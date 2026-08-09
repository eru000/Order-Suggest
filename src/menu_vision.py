"""High-accuracy multi-model menu ingestion shared by web and LINE."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageFilter, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    # JPEG/PNG/WebP continue to work; requirements.txt installs HEIF support.
    pass

from ollama_fuc import vision_chat


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
TILE_THRESHOLD = 1600
LOW_RES_RECOVERY_LONG_SIDE = 2000
TILE_OVERLAP = 0.12
MERGE_SIMILARITY = 0.92
# .env 沒設定時的後備模型。原本這裡寫死 "gemma-4-31b"，但那個模型在學校的
# API 上已經回 HTTP 500——組員少填一行 .env 就會每個切塊都失敗，而且錯誤
# 訊息看起來像網路問題。同一份預設值散在四個地方也是它會飄掉的原因。
#
# ornith-35b 是 Ornith-1.0-35B（35B MoE，3B 活躍）。同一張菜單它與 397B 的
# vibe 端點同樣拿到 100%，但單次切塊延遲中位數 3.4s vs 5.3s。菜單 OCR 是
# 「照著抄」，不太需要 thinking model 的推理預算。
#
# 校對那關同時負責店名辨識，原本用 mistral-small-4 四次全錯（把「向宏魯肉飯」
# 讀成「台客魚肉粒」，純紅色的圖也說是棕色）。改成同一個 ornith-35b 之後店名
# 正確。OCR 與校對同模型不會讓校對變成橡皮圖章——它的價值來自視角不同：
# 切塊各自只看四分之一，校對一次看完四塊，實測仍補回 7 個切塊漏掉的品項。
DEFAULT_OCR_MODEL = "ornith-35b"
DEFAULT_VERIFY_MODEL = "ornith-35b"
# 最終校對的輸出與切塊結果配對時的門檻。低於這個值就視為「不是同一項」，
# 也就是被刪掉或被憑空新增。
VERIFY_MATCH_THRESHOLD = 0.72
# 三個字的品名改一個字，相似度就只剩 0.667——「肉蓗飯」被校對修成「肉羹飯」
# 會低於上面的門檻，於是同一道菜的錯字版與更正版被雙雙保留。價格一致時放寬
# 到這個值，讓「修錯字」不會被誤判成「刪一項又加一項」。
VERIFY_SAME_PRICE_THRESHOLD = 0.5


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _extract_json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        pass
    fenced = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text or "", flags=re.I | re.S)
    decoder = json.JSONDecoder()
    for index, char in enumerate(fenced):
        if char not in "[{":
            continue
        try:
            return decoder.raw_decode(fenced[index:])[0]
        except json.JSONDecodeError:
            continue
    return {}


def _clean_price(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = str(value).strip().replace(",", "")
    if not text or any(word in text.casefold() for word in ("時價", "未知", "unknown", "null", "看不清")):
        return None
    match = re.search(r"(?:NT\$?|\$)?\s*(\d{1,6}(?:\.\d+)?)", text, flags=re.I)
    return float(match.group(1)) if match else None


def _coerce_category(raw_category: Any) -> Optional[Dict[str, Any]]:
    """把模型常見的替代格式收斂成 {"name", "items"}。

    llama4scout 很常無視 schema，回傳巢狀陣列而不是物件：
        {"categories": [["麵類", [["乾麵", 45], ["大乾麵", 55]]]]}

    這不是壞資料——菜名與價格都是對的。但舊版直接 `isinstance(dict)` 過濾，
    整個分類會被 continue 掉且不留痕跡。實測一張雙欄菜單就是這樣整個右半邊
    消失（24 項只剩 14 項），而且 warnings 一個字都沒有。
    """
    if isinstance(raw_category, dict):
        return raw_category
    if isinstance(raw_category, (list, tuple)) and len(raw_category) == 2:
        name, items = raw_category
        if isinstance(name, str) and isinstance(items, (list, tuple)):
            return {"name": name, "items": list(items)}
    return None


def _coerce_item(raw_item: Any) -> Optional[Dict[str, Any]]:
    """同上，但針對品項。模型會回 ["乾麵", 45] 而不是 {"name":…, "price":…}。"""
    if isinstance(raw_item, dict):
        return raw_item
    if isinstance(raw_item, str):
        match = re.search(r"(?:NT\$?|\$)\s*(\d{1,6}(?:\.\d+)?)\s*$", raw_item, flags=re.I)
        return {
            "name": raw_item[: match.start()].strip(" -—:") if match else raw_item.strip(),
            "price": match.group(1) if match else None,
        }
    if isinstance(raw_item, (list, tuple)) and raw_item and isinstance(raw_item[0], str):
        return {"name": raw_item[0], "price": raw_item[1] if len(raw_item) > 1 else None}
    return None


def normalize_vision_result(raw: Any, restaurant_hint: str = "") -> Dict[str, Any]:
    """Normalize untrusted model JSON without treating model confidence as quality."""
    if isinstance(raw, list):
        raw = {"menu_items": raw}
    if not isinstance(raw, dict):
        raw = {}
    detected_name = str(raw.get("restaurant_name") or raw.get("restaurantName") or "").strip()
    if _name_key(detected_name) in {
        _name_key("圖片可見店名"),
        _name_key("圖片實際可見店名"),
        _name_key("餐廳名稱"),
        _name_key("店名"),
    }:
        detected_name = ""
    raw_categories = raw.get("categories")
    if isinstance(raw_categories, dict):
        raw_categories = [
            {"name": category_name, "items": items if isinstance(items, list) else []}
            for category_name, items in raw_categories.items()
        ]
    elif not isinstance(raw_categories, list):
        fallback = raw.get("menu_items") or raw.get("items") or raw.get("dishes") or raw.get("foods") or []
        raw_categories = [{"name": "菜單", "items": fallback}]

    categories: List[Dict[str, Any]] = []
    seen = set()
    for raw_category in raw_categories:
        raw_category = _coerce_category(raw_category)
        if raw_category is None:
            continue
        if not any(key in raw_category for key in ("items", "menu_items", "dishes")) and any(
            key in raw_category for key in ("price", "amount", "cost", "dish", "item_name")
        ):
            raw_category = {"name": "菜單", "items": [raw_category]}
        category_name = str(raw_category.get("name") or "其他").strip()[:80] or "其他"
        raw_items = raw_category.get("items") or raw_category.get("menu_items") or raw_category.get("dishes") or []
        items = []
        for raw_item in raw_items if isinstance(raw_items, (list, tuple)) else []:
            raw_item = _coerce_item(raw_item)
            if raw_item is None:
                continue
            name = str(raw_item.get("name") or raw_item.get("dish") or raw_item.get("item_name") or raw_item.get("title") or "").strip()
            key = re.sub(r"\s+", "", name).casefold()
            if len(name) < 2 or key in seen:
                continue
            seen.add(key)
            price = raw_item.get("price")
            if price is None:
                price = raw_item.get("amount") if raw_item.get("amount") is not None else raw_item.get("cost")
            item: Dict[str, Any] = {"name": name[:160], "price": _clean_price(price)}
            description = str(raw_item.get("description") or "").strip()
            if description:
                item["description"] = description[:300]
            items.append(item)
        if items:
            categories.append({"name": category_name, "items": items})

    source_type = str(raw.get("source_type") or "menu").lower()
    if source_type not in {"menu", "dish_display", "food_photo", "unknown"}:
        source_type = "unknown"
    try:
        legacy_confidence = min(1.0, max(0.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        legacy_confidence = 0.0
    return {
        "restaurant_name": (restaurant_hint.strip() or detected_name)[:120],
        "detected_restaurant_name": detected_name[:120],
        "source_type": source_type,
        # Kept only for response-schema compatibility. analyze_menu_image
        # replaces this with the programmatically derived quality score.
        "confidence": legacy_confidence,
        "categories": categories,
        "warnings": [str(value)[:200] for value in raw.get("warnings", []) if str(value).strip()][:8]
        if isinstance(raw.get("warnings"), list)
        else [],
    }


def _data_url(image_bytes: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _encode_jpeg(image: Image.Image, quality: int = 94) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def prepare_image_regions(image_bytes: bytes) -> Tuple[bytes, List[Dict[str, Any]], Tuple[int, int]]:
    """Apply EXIF orientation and return either one region or overlapping 2x2 regions."""
    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    # Messaging clients and clipboard uploads sometimes downscale a readable
    # 2500px menu to ~840px. Upscale moderately before tiling so the vision API
    # does not downsample already tiny Chinese glyphs a second time.
    original_width, original_height = image.size
    if 400 <= max(original_width, original_height) < TILE_THRESHOLD:
        scale = LOW_RES_RECOVERY_LONG_SIDE / max(original_width, original_height)
        recovered_size = (
            max(1, int(round(original_width * scale))),
            max(1, int(round(original_height * scale))),
        )
        image = image.resize(recovered_size, Image.Resampling.LANCZOS).filter(
            ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3)
        )
    width, height = image.size
    full = _encode_jpeg(image)
    if max(width, height) <= TILE_THRESHOLD:
        return full, [{"id": "full", "box": [0, 0, width, height], "bytes": full}], (width, height)

    overlap_x = int(round(width * TILE_OVERLAP / 2))
    overlap_y = int(round(height * TILE_OVERLAP / 2))
    middle_x, middle_y = width // 2, height // 2
    boxes = [
        (0, 0, min(width, middle_x + overlap_x), min(height, middle_y + overlap_y)),
        (max(0, middle_x - overlap_x), 0, width, min(height, middle_y + overlap_y)),
        (0, max(0, middle_y - overlap_y), min(width, middle_x + overlap_x), height),
        (max(0, middle_x - overlap_x), max(0, middle_y - overlap_y), width, height),
    ]
    regions = []
    for index, box in enumerate(boxes, 1):
        regions.append({"id": f"tile-{index}", "box": list(box), "bytes": _encode_jpeg(image.crop(box))})
    return full, regions, (width, height)


def _call_vision(
    vision_func: Any,
    prompt: str,
    image_url: str | Sequence[str],
    *,
    model: str,
    timeout: float = 180.0,
    temperature: float = 0.0,
) -> str:
    try:
        return vision_func(prompt, image_url=image_url, model=model, timeout=timeout, temperature=temperature)
    except TypeError as exc:
        # Existing injected test doubles and third-party adapters may expose the
        # older (prompt, image_url, timeout) signature.
        if "unexpected keyword" not in str(exc):
            raise
        return vision_func(prompt, image_url=image_url, timeout=timeout)


def _name_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", value.casefold())


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _name_key(left), _name_key(right)).ratio()


def _names_conflict(hint: str, detected: str) -> bool:
    if not hint.strip() or not detected.strip():
        return False
    left, right = _name_key(hint), _name_key(detected)
    return left not in right and right not in left and SequenceMatcher(None, left, right).ratio() < 0.72


def _tile_prompt(
    tile_id: str,
    box: Sequence[int],
    overview: Dict[str, Any],
    *,
    ask_identity: bool = False,
) -> str:
    """ask_identity 給快速模式用：沒跑總覽時，店名得由切塊自己順便認。"""
    identity_rule = (
        "\nrestaurant_name 只填這個區塊裡實際印出的店名（通常在菜單最上方）；"
        "這一塊看不到店名就填空字串，不要猜、也不要抄欄位說明。"
        if ask_identity
        else ""
    )
    schema = (
        '{{"restaurant_name":"","categories":[{{"name":"分類","items":[{{"name":"完整品名","price":230}}]}}]}}'
        if ask_identity
        else '{{"categories":[{{"name":"分類","items":[{{"name":"完整品名","price":230}}]}}]}}'
    )
    return f"""你是繁體中文菜單 OCR 專家。這是原圖的高解析區塊 {tile_id}，原圖座標 {list(box)}。
全圖初步資訊：{json.dumps(overview, ensure_ascii=False)}
逐字抄錄此區塊內所有主要餐點品名與印刷價格。保留印刷的繁體字，不要憑圖片猜食材，不要加入產地、克數、電話或說明文字。
被手寫線劃過的印刷品項仍要保留。看不清價格用 null；不要猜數字。{identity_rule}
只回傳 JSON：{schema}"""


def _flatten_categories(categories: Sequence[Dict[str, Any]], source: Dict[str, Any]) -> List[Dict[str, Any]]:
    flattened = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        category_name = str(category.get("name") or "其他")
        for item in category.get("items", []):
            if isinstance(item, dict):
                flattened.append({**item, "category": category_name, "sources": [source]})
    return flattened


def merge_region_items(region_results: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Merge duplicates only at >=0.92 similarity and with compatible prices."""
    merged: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for region in region_results:
        source = {"block": region["id"], "box": list(region["box"])}
        for candidate in _flatten_categories(region.get("categories", []), source):
            best = None
            best_score = 0.0
            for existing in merged:
                score = _similarity(candidate["name"], existing["name"])
                if score > best_score:
                    best, best_score = existing, score
            if best is not None and best_score >= MERGE_SIMILARITY:
                left_price, right_price = best.get("price"), candidate.get("price")
                if left_price is not None and right_price is not None and left_price != right_price:
                    conflicts.append({
                        "type": "price",
                        "candidates": [best["name"], candidate["name"]],
                        "prices": [left_price, right_price],
                        "blocks": [best["sources"][0]["block"], source["block"]],
                        "resolved": False,
                    })
                    merged.append(candidate)
                    continue
                best["sources"].append(source)
                if best.get("price") is None and candidate.get("price") is not None:
                    best["price"] = candidate["price"]
                continue
            if best is not None and best_score >= 0.84:
                conflicts.append({
                    "type": "name",
                    "candidates": [best["name"], candidate["name"]],
                    "prices": [best.get("price"), candidate.get("price")],
                    "blocks": [best["sources"][0]["block"], source["block"]],
                    "resolved": False,
                })
            merged.append(candidate)
    return merged, conflicts


def _items_to_categories(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    category_map: Dict[str, List[Dict[str, Any]]] = {}
    for index, item in enumerate(items, 1):
        public = {"id": f"item-{index:03d}", "name": item["name"], "price": item.get("price")}
        if item.get("description"):
            public["description"] = item["description"]
        if item.get("sources"):
            public["sources"] = item["sources"]
        category_map.setdefault(str(item.get("category") or "其他"), []).append(public)
    return [{"name": name, "items": values} for name, values in category_map.items()]


def _menu_item_refs(result: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    refs: List[Tuple[str, Dict[str, Any]]] = []
    for category in result.get("categories", []):
        if not isinstance(category, dict):
            continue
        category_name = str(category.get("name") or "其他")
        for item in category.get("items", []):
            if isinstance(item, dict) and item.get("name"):
                refs.append((category_name, item))
    return refs


def format_menu_preview(result: Dict[str, Any], max_items: int = 40) -> str:
    """Create a compact, numbered menu preview suitable for LINE."""
    refs = _menu_item_refs(result)
    lines = []
    for index, (category, item) in enumerate(refs[:max_items], 1):
        price = item.get("price")
        price_text = f"${float(price):.0f}" if isinstance(price, (int, float)) else "價格不明"
        lines.append(f"{index}. [{category}] {item['name']} — {price_text}")
    if len(refs) > max_items:
        lines.append(f"…另有 {len(refs) - max_items} 項")
    return "\n".join(lines) or "（沒有可顯示品項）"


def looks_like_manual_correction(text: str) -> bool:
    compact = (text or "").strip()
    return bool(
        re.search(r"(?:修改|更正|修正|改名|改價|價格|改成|改為|應該是|=>|→|改)", compact)
        or re.match(r"^第?\s*\d+\s*項?\s*[:：]", compact)
    )


def apply_manual_correction(result: Dict[str, Any], instruction: str) -> Dict[str, Any]:
    """Apply one deterministic correction to a pending menu analysis.

    Primary syntax: ``改 3 菜名 豬腳飯`` or ``改 3 價格 90``. Older
    natural-language forms remain compatible. No model mutates persisted data.
    """
    text = (instruction or "").strip()
    if not text:
        raise ValueError("修正指令不可為空")
    refs = _menu_item_refs(result)
    if not refs:
        raise ValueError("目前沒有可修正的菜單項目")
    numbered_match = re.match(
        r"^\s*(?:改|修改|更正|修正)?\s*第?\s*(\d+)\s*項?\s*(菜名|名稱|品名|價格|價錢)\s*(?:改成|改為|為|成|[:：=])?\s*(.+?)\s*$",
        text,
    )
    if numbered_match:
        selector = numbered_match.group(1)
        field = numbered_match.group(2)
        raw_value = numbered_match.group(3).strip()
        operation = "price" if field in {"價格", "價錢"} else "rename"
        if operation == "price":
            price_value = re.fullmatch(r"\$?\s*(\d{1,6}(?:\.\d+)?)\s*(?:元)?", raw_value)
            if not price_value:
                raise ValueError("價格必須是數字，例如：改 3 價格 90")
            new_value = float(price_value.group(1))
        else:
            new_value = raw_value
    else:
        text = re.sub(r"^\s*(?:修改|更正|修正|改名|改價)\s*[：:]?\s*", "", text)
        price_match = re.match(
            r"^\s*(?:把|將)?\s*(.+?)\s*(?:的)?\s*價格\s*(?:改成|改為|改|=>|→|[:：])\s*\$?\s*(\d{1,6}(?:\.\d+)?)\s*(?:元)?\s*$",
            text,
        )
        operation = "price" if price_match else "rename"
        if price_match:
            selector, new_value = price_match.group(1), float(price_match.group(2))
        else:
            rename_match = re.match(
                r"^\s*(?:把|將)?\s*(.+?)\s*(?:改成|改為|應該是|=>|→|改)\s*(.+?)\s*(?:才對)?\s*$",
                text,
            )
            if not rename_match:
                raise ValueError("看不懂修正指令。請使用：改 3 菜名 豬腳飯")
            selector, new_value = rename_match.group(1), rename_match.group(2)

    selector = re.sub(r"^[「『'\"\s]+|[」』'\"\s]+$", "", str(selector))
    selector = re.sub(r"^第\s*(\d+)\s*項?$", r"\1", selector)
    if selector.isdigit():
        index = int(selector)
        if not 1 <= index <= len(refs):
            raise ValueError(f"找不到第 {index} 項，這份菜單目前有 {len(refs)} 項")
        target = refs[index - 1][1]
        matched_fuzzily = False
    else:
        matches = [item for _, item in refs if _name_key(str(item.get("name") or "")) == _name_key(selector)]
        if not matches:
            ranked = sorted(
                [(_similarity(selector, str(item.get("name") or "")), index, item) for index, (_, item) in enumerate(refs, 1)],
                key=lambda row: row[0],
                reverse=True,
            )
            best_score, best_index, best_item = ranked[0]
            second_score = ranked[1][0] if len(ranked) > 1 else 0.0
            # Accept a single clear OCR-near-match. Ambiguous fuzzy matches are
            # never mutated automatically; the numbered preview remains the
            # unambiguous fallback.
            if best_score >= 0.72 and (best_score >= 0.9 or best_score - second_score >= 0.08):
                matches = [best_item]
                matched_fuzzily = True
            else:
                suggestions = "、".join(
                    f"第{index}項「{item.get('name')}」" for score, index, item in ranked[:3] if score >= 0.45
                )
                hint = f"；最接近的是 {suggestions}" if suggestions else ""
                raise ValueError(f"找不到品項「{selector}」的唯一匹配{hint}，請使用預覽編號修改")
        else:
            matched_fuzzily = False
        if len(matches) > 1:
            raise ValueError(f"品項「{selector}」出現多次，請使用預覽編號修改")
        target = matches[0]

    old_name = str(target.get("name") or "")
    if operation == "price":
        old_value = target.get("price")
        target["price"] = new_value
        change = {"type": "price", "item": old_name, "from": old_value, "to": new_value}
        message = f"已把「{old_name}」價格改為 ${new_value:.0f}"
    else:
        new_name = re.sub(r"^[「『'\"\s]+|[」』'\"\s。！]+$", "", str(new_value)).strip()
        if len(new_name) < 2 or len(new_name) > 160:
            raise ValueError("新菜名長度不合理")
        if any(item is not target and _name_key(str(item.get("name") or "")) == _name_key(new_name) for _, item in refs):
            raise ValueError(f"菜單中已經有「{new_name}」，請避免建立重複品項")
        target["name"] = new_name
        change = {"type": "rename", "from": old_name, "to": new_name}
        fuzzy_note = "（依近似品名配對）" if matched_fuzzily else ""
        message = f"已把「{old_name}」改成「{new_name}」{fuzzy_note}"

    corrections = result.setdefault("manualCorrections", [])
    if isinstance(corrections, list):
        corrections.append({**change, "correctedAt": datetime.now(timezone.utc).isoformat()})
    quality = result.setdefault("quality", {})
    if isinstance(quality, dict):
        priced = sum(item.get("price") is not None for _, item in refs)
        quality["itemCount"] = len(refs)
        quality["priceCoverage"] = round(priced / len(refs), 3) if refs else 0.0
        quality["humanReviewed"] = True
        quality["manualCorrectionCount"] = len(corrections) if isinstance(corrections, list) else 1
    for conflict in result.get("conflicts", []):
        if isinstance(conflict, dict) and old_name in conflict.get("candidates", []):
            conflict["resolved"] = True
            conflict["resolvedBy"] = "human"
    return {"message": message, "change": change, "result": result}


def _attach_sources(verified: List[Dict[str, Any]], original: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for item in verified:
        matches = sorted(original, key=lambda source: _similarity(item["name"], source["name"]), reverse=True)
        if matches and _similarity(item["name"], matches[0]["name"]) >= VERIFY_MATCH_THRESHOLD:
            item["sources"] = matches[0].get("sources", [])
    return verified


def _same_item(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """這兩筆是不是同一道菜（可能只是其中一邊有錯字）。"""
    score = _similarity(str(left.get("name") or ""), str(right.get("name") or ""))
    if score >= VERIFY_MATCH_THRESHOLD:
        return True
    left_price, right_price = left.get("price"), right.get("price")
    if score < VERIFY_SAME_PRICE_THRESHOLD or left_price is None or right_price is None:
        return False
    try:
        return abs(float(left_price) - float(right_price)) < 0.01
    except (TypeError, ValueError):
        return False


def _has_counterpart(item: Dict[str, Any], candidates: Sequence[Dict[str, Any]]) -> bool:
    return any(_same_item(item, other) for other in candidates)


def _reconcile_verified(
    verified_items: List[Dict[str, Any]],
    tile_items: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """對帳最終校對的輸出與切塊實際讀到的內容。

    校對那一關原本握有絕對的刪除權：只要它沒寫進回傳，該品項就消失，
    而且不留痕跡。實測 tile-4 把「大肉羹麵 65」讀得清清楚楚，最終輸出
    卻沒有它；同一次校對一項也沒補回來。

    這裡不讓它靜默刪除。取捨的理由是兩種錯的可見度差很多：漏掉的品項是
    看不見的失敗——使用者不會知道菜單少了什麼，推薦引擎也永遠不會提到它；
    多出來的品項則會出現在確認畫面上，人看得到、也能用 apply_manual_correction
    改掉。所以補回被刪的品項並標記出來，把裁決權交還給人。

    校正仍然照收：品名或價格被改過的品項，相似度夠高就配得上，不會變成
    重複項（例如「魯肉湯飯」被更正成「魯肉湯麵」）。

    回傳 (最終品項, 被刪而補回的品名, 校對憑空新增的品名)。
    """
    restored = [
        dict(tile_item)
        for tile_item in tile_items
        if str(tile_item.get("name") or "") and not _has_counterpart(tile_item, verified_items)
    ]
    # 校對憑空生出來、四個切塊都沒讀到的品項最可疑：它沒有任何影像佐證。
    invented = [
        str(item["name"])
        for item in verified_items
        if str(item.get("name") or "") and not _has_counterpart(item, tile_items)
    ]
    return verified_items + restored, [str(item["name"]) for item in restored], invented


def _legacy_analyze(
    image_bytes: bytes,
    mime: str,
    restaurant_hint: str,
    vision_func: Any,
) -> Dict[str, Any]:
    """Compatibility path for adapters/tests that pass opaque non-image bytes."""
    url = _data_url(image_bytes, mime)
    prompt = _tile_prompt("full", [0, 0, 0, 0], {"restaurant_hint": restaurant_hint})
    first = normalize_vision_result(_extract_json_value(_call_vision(
        vision_func, prompt, url, model=os.getenv("VISION_MODEL", DEFAULT_OCR_MODEL)
    )), restaurant_hint)
    if not first["categories"]:
        retry = normalize_vision_result(_extract_json_value(_call_vision(
            vision_func,
            "讀取圖片中可見的菜名與價格，只回傳 JSON menu_items 陣列。",
            url,
            model=os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL),
        )), restaurant_hint)
        return retry
    draft = json.dumps({"restaurant_name": first["detected_restaurant_name"], "categories": first["categories"]}, ensure_ascii=False)
    verified = normalize_vision_result(_extract_json_value(_call_vision(
        vision_func,
        f"對照同一張圖片校正下列菜單，只回傳完整 JSON，不得新增看不見的品項：{draft}",
        url,
        model=os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL),
    )), restaurant_hint)
    return verified if verified["categories"] else first


def analyze_menu_image(
    image_bytes: bytes,
    content_type: str,
    restaurant_hint: str = "",
    *,
    vision_func: Any = vision_chat,
) -> Dict[str, Any]:
    if not image_bytes:
        raise ValueError("圖片內容不可為空")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("圖片不可超過 10 MB")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_IMAGE_TYPES:
        raise ValueError("僅支援 JPEG、PNG、WebP 或 HEIC 圖片")

    try:
        full_bytes, regions, image_size = prepare_image_regions(image_bytes)
    except Exception:
        result = _legacy_analyze(image_bytes, mime, restaurant_hint, vision_func)
        if not result["categories"]:
            raise ValueError("照片中沒有辨識到可用的菜單或菜色")
        item_count = sum(len(cat["items"]) for cat in result["categories"])
        priced = sum(item.get("price") is not None for cat in result["categories"] for item in cat["items"])
        coverage = round(priced / item_count, 3) if item_count else 0.0
        detected = result.get("detected_restaurant_name", "")
        result.update({
            "quality": {"score": round(coverage * 0.7 + 0.2, 3), "priceCoverage": coverage, "itemCount": item_count},
            "conflicts": [],
            "identity": {"userHint": restaurant_hint, "detectedName": detected, "candidates": [detected] if detected else []},
            "identityConflict": _names_conflict(restaurant_hint, detected),
            "models": {"ocr": os.getenv("VISION_MODEL", DEFAULT_OCR_MODEL), "verify": os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL)},
            "sourceBlocks": [{"id": "full", "box": [0, 0, 0, 0]}],
        })
        result["confidence"] = result["quality"]["score"]
        return result

    # 快速模式：跳過總覽（只判版面與店名），切塊改為並行。呼叫次數 6 → 5，
    # 但等待從三輪縮成兩輪。
    #
    # 最終校對那一關**不能省**。它的 prompt 負責「補上清楚可見但遺漏的主要排
    # 餐」，實測拿掉之後同一張照片從 31 項掉到 16 項——整個左半邊的飯類、麵類、
    # 盤類、燙青菜全部消失。省下的那一輪等待不值這個代價。
    fast_mode = os.getenv("VISION_FAST", "").strip().lower() in {"1", "true", "yes", "on"}

    full_url = _data_url(full_bytes, "image/jpeg")
    overview_prompt = """閱讀整張餐廳菜單，只做版面與身分辨識，不要逐項 OCR。
請完全根據圖片判斷，不要參考或猜測使用者先前輸入的名稱。
restaurant_name 只能填圖片實際印出的店名；看不清就填空字串，禁止抄寫欄位說明。
若沒有獨立招牌，但某個特色餐點名稱清楚像品牌，可放入 brand_candidates，這只是候選而不是確認店名。
回傳 JSON：{"restaurant_name":"","brand_candidates":[],"source_type":"menu","menu_type":"","layout":"","warnings":[]}"""
    requested_ocr_model = os.getenv("VISION_MODEL", DEFAULT_OCR_MODEL)
    verify_model = os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL)
    active_ocr_model = requested_ocr_model
    ocr_fallback_warning = ""

    if fast_mode:
        overview = {}
    else:
        overview_raw = _extract_json_value(_call_vision(
            vision_func,
            overview_prompt,
            full_url,
            model=os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL),
            temperature=0.0,
        ))
        overview = overview_raw if isinstance(overview_raw, dict) else {}

    tile_urls = [_data_url(region["bytes"], "image/jpeg") for region in regions]

    def _ocr_region(index: int) -> Dict[str, Any]:
        """單一切塊的 OCR。降級是各切塊獨立判斷，才能安全地並行。"""
        nonlocal active_ocr_model, ocr_fallback_warning
        region = regions[index]
        tile_prompt = _tile_prompt(region["id"], region["box"], overview, ask_identity=fast_mode)
        try:
            response = _call_vision(
                vision_func,
                tile_prompt,
                tile_urls[index],
                model=requested_ocr_model,
                temperature=_float_env("VISION_TEMPERATURE", 0.0),
            )
        except RuntimeError as exc:
            if requested_ocr_model == verify_model:
                raise
            active_ocr_model = verify_model
            ocr_fallback_warning = (
                f"主要 OCR 模型 {requested_ocr_model} 無法使用，本張圖片已改由 {verify_model} 辨識"
            )
            print(f"[Vision] {ocr_fallback_warning}: {exc}")
            response = _call_vision(
                vision_func,
                tile_prompt,
                tile_urls[index],
                model=verify_model,
                temperature=0.0,
            )
        normalized = normalize_vision_result(_extract_json_value(response))
        return {
            "id": region["id"],
            "box": region["box"],
            "categories": normalized["categories"],
            "detected_restaurant_name": normalized.get("detected_restaurant_name", ""),
        }

    # 切塊彼此無關，串列跑等於把等待時間乘四。
    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        region_results = list(pool.map(_ocr_region, range(len(regions))))

    if fast_mode:
        # 沒跑總覽，店名只能從切塊來。招牌常橫跨中線被切成兩半，所以挑最長的
        # ——殘缺的一定比完整的短。
        #
        # 試過另外裁一條全寬橫幅專門認店名，實測反而更糟：那是一張又寬又扁的
        # 低資訊圖，模型會整個幻覺（把「斗六門當歸鴨」讀成「萊菔非六門餐鴨」），
        # 而且和切塊結果的相似度剛好落在門檻邊緣擋不掉。多花一次呼叫換更差的
        # 結果，所以拿掉了。
        tile_names = [
            str(region.get("detected_restaurant_name") or "").strip()
            for region in region_results
            if str(region.get("detected_restaurant_name") or "").strip()
        ]
        overview["restaurant_name"] = max(tile_names, key=len) if tile_names else ""

    merged, conflicts = merge_region_items(region_results)
    if not merged:
        raise ValueError("照片中沒有辨識到可用的菜單或菜色")

    # The final pass sees the high-resolution regions, not a downscaled full image.
    draft_categories = _items_to_categories(merged)
    verify_prompt = f"""你是菜單 OCR 最終校對員。接下來圖片依序是 {', '.join(region['id'] for region in regions)}。
請逐項對照圖片校正草稿：修正錯字與價格、合併重疊區重複項、刪除虛構項目、補上清楚可見但遺漏的主要排餐。
不能把 230 看成 330，也不能把 360 看成 560。繁體中文店名與菜名照印刷文字。
草稿：{json.dumps(draft_categories, ensure_ascii=False)}
待裁決衝突：{json.dumps(conflicts, ensure_ascii=False)}
restaurant_name 只能填圖片實際印出的店名，看不清就填空字串。禁止把「店名、分類、菜名」等欄位說明當作內容。
只回傳 JSON：{{"restaurant_name":"","categories":[{{"name":"","items":[{{"name":"","price":null}}]}}]}}"""
    verified_raw = _extract_json_value(_call_vision(
        vision_func,
        verify_prompt,
        tile_urls,
        model=os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL),
        temperature=0.0,
    ))
    verified = normalize_vision_result(verified_raw)
    restored_by_verify: List[str] = []
    invented_by_verify: List[str] = []
    if verified["categories"]:
        verified_flat = _attach_sources(_flatten_categories(verified["categories"], {}), merged)
        final_items, restored_by_verify, invented_by_verify = _reconcile_verified(verified_flat, merged)
        categories = _items_to_categories(final_items)
    else:
        categories = draft_categories

    brand_candidates = [
        str(value).strip()[:120] for value in overview.get("brand_candidates", [])
        if isinstance(value, str) and value.strip()
    ] if isinstance(overview.get("brand_candidates"), list) else []
    printed_name = str(verified.get("detected_restaurant_name") or overview.get("restaurant_name") or "").strip()
    detected = printed_name or (brand_candidates[0] if brand_candidates else "")
    item_count = sum(len(category["items"]) for category in categories)
    priced = sum(item.get("price") is not None for category in categories for item in category["items"])
    coverage = round(priced / item_count, 3) if item_count else 0.0
    unresolved = sum(not conflict.get("resolved") for conflict in conflicts)
    quality_score = round(min(1.0, coverage * 0.55 + min(item_count / 20, 1) * 0.25 + (0.2 if unresolved == 0 else 0.1)), 3)
    if ocr_fallback_warning:
        # A fallback can keep the flow usable but must never masquerade as a
        # high-confidence primary OCR result.
        quality_score = min(quality_score, 0.7)
    identity_conflict = _names_conflict(restaurant_hint, detected)
    warnings = [str(value)[:200] for value in overview.get("warnings", []) if str(value).strip()] if isinstance(overview.get("warnings"), list) else []
    if identity_conflict:
        warnings.append(f"使用者店名「{restaurant_hint}」與圖片候選「{detected}」不同，確認前不會存檔")
    if not printed_name and detected:
        warnings.append(f"圖片未見獨立店名，候選「{detected}」是由特色餐點文字推測，仍需人工確認")
    if conflicts:
        warnings.append(f"有 {len(conflicts)} 組 OCR 候選曾發生衝突，請確認摘要")
    if restored_by_verify:
        preview = "、".join(restored_by_verify[:5]) + ("…" if len(restored_by_verify) > 5 else "")
        warnings.append(f"最終校對漏掉 {len(restored_by_verify)} 項切塊已讀出的品項，已補回請確認：{preview}")
    if invented_by_verify:
        preview = "、".join(invented_by_verify[:5]) + ("…" if len(invented_by_verify) > 5 else "")
        warnings.append(f"有 {len(invented_by_verify)} 項只出現在最終校對、切塊都沒讀到，請特別確認：{preview}")
    if ocr_fallback_warning:
        warnings.append(ocr_fallback_warning)
    return {
        "restaurant_name": (restaurant_hint.strip() or detected)[:120],
        "detected_restaurant_name": detected[:120],
        "source_type": str(overview.get("source_type") or "menu"),
        "confidence": quality_score,
        "categories": categories,
        "warnings": warnings[:8],
        "quality": {
            "score": quality_score,
            "priceCoverage": coverage,
            "itemCount": item_count,
            "conflictCount": len(conflicts),
            "modelFallback": bool(ocr_fallback_warning),
            "verifyDroppedCount": len(restored_by_verify),
            "verifyInventedCount": len(invented_by_verify),
        },
        "conflicts": conflicts,
        "identity": {
            "userHint": restaurant_hint.strip(),
            "detectedName": detected[:120],
            "candidates": list(dict.fromkeys(value for value in (detected[:120], *brand_candidates, restaurant_hint.strip()) if value)),
            "candidateInferred": bool(detected and not printed_name),
            "menuType": str(overview.get("menu_type") or ""),
        },
        "identityConflict": identity_conflict,
        "models": {
            "overview": os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL),
            "ocrRequested": requested_ocr_model,
            "ocr": active_ocr_model,
            "verify": verify_model,
        },
        "sourceBlocks": [{"id": region["id"], "box": region["box"]} for region in regions],
        "imageSize": {"width": image_size[0], "height": image_size[1]},
    }


def to_persisted_document(result: Dict[str, Any], confirmed_at: Optional[str] = None) -> Dict[str, Any]:
    menu_items = []
    for category in result.get("categories", []):
        for item in category.get("items", []):
            menu_items.append({**item, "category": category.get("name") or "其他"})
    return {
        "schemaVersion": 2,
        "name": result.get("restaurant_name", ""),
        "source": "user_photo_vlm",
        "source_type": result.get("source_type", "unknown"),
        "confirmedAt": confirmed_at or datetime.now(timezone.utc).isoformat(),
        "quality": result.get("quality", {}),
        "identity": result.get("identity", {}),
        "identityConflict": bool(result.get("identityConflict")),
        "models": result.get("models", {}),
        "sourceBlocks": result.get("sourceBlocks", []),
        "manualCorrections": result.get("manualCorrections", []),
        "warnings": result.get("warnings", []),
        "menu_items": menu_items,
    }
