"""High-accuracy multi-model menu ingestion shared by web and LINE."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
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

from observability import emit
from ollama_fuc import vision_chat


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
TILE_THRESHOLD = 1600
# 所有圖片一律正規化到這個長邊：小的放大、大的縮小。
#
# 放大不會增加資訊——910px 插值到 3600px，新像素全是編出來的。有用的原因是
# 視覺模型把圖切成固定大小的 patch、每個 patch 一個 token，所以關鍵不是「圖
# 有幾個像素」而是「一個字佔到幾個 patch」。字被撐開到更多 patch 上，模型才
# 有東西可看。切塊當初有效也是同一個道理（每塊只涵蓋四分之一畫面），只是放
# 大用一次呼叫就達成，而且不會把直式版面切斷。
#
# 掃過三個點，四個 eval 案例的價格正確率：
#
#     2600 → 96.9%   64.9s
#     3600 → 99.2%   77.4s   ← 平台從這裡開始
#     4600 → 99.2%  106.2s   準確度一樣，只是更慢
#
# 2600 → 3600 修好了 budaoweng 手寫紅字的 160（原本讀成 150）與 xianghong 兩
# 項「（限內用）套餐」的小字價格（原本整個讀不到）。再往上沒有東西可以贏了。
TARGET_LONG_SIDE = 3600
# 低於這個尺寸的圖放大只會放大雜訊，不碰。
MIN_UPSCALE_LONG_SIDE = 400
# 總覽只判斷版面與店名，prompt 明寫「不要逐項 OCR」，不需要讀清楚每個價格。
OVERVIEW_LONG_SIDE = 1400
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


def _extract_json_document(text: str) -> Tuple[Any, bool]:
    """回傳 (解析結果, 是否真的解析到 JSON)。

    第二個值不能用「結果是不是空的」來推——模型回一個字面上的 ``{}`` 是解析
    成功的空物件，跟「整段回應裡找不到 JSON」是兩件事，修法也完全不同：前者
    要改 prompt，後者要看模型到底吐了什麼。
    """
    try:
        return json.loads(text), True
    except (TypeError, json.JSONDecodeError):
        pass
    fenced = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text or "", flags=re.I | re.S)
    decoder = json.JSONDecoder()
    for index, char in enumerate(fenced):
        if char not in "[{":
            continue
        try:
            return decoder.raw_decode(fenced[index:])[0], True
        except json.JSONDecodeError:
            continue
    # 這個專案在「解析把讀好的東西扔掉、看起來卻像模型爛」上吃過一次大虧
    # （normalize_vision_result 的靜默 continue），所以這裡一定要留下痕跡。
    emit(
        "menu_vision.json_unparsed",
        textLength=len(text or ""),
        preview=(text or "")[:200],
    )
    return {}, False


def _extract_json_value(text: str) -> Any:
    return _extract_json_document(text)[0]


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
        # `title` 是 prompt 現在要求的 key（`name` 會被閘道轉成 tool_calls），
        # `name` 保留是為了舊資料與 LINE 那條路傳進來的結構。
        category_name = str(
            raw_category.get("name") or raw_category.get("title") or "其他"
        ).strip()[:80] or "其他"
        raw_items = raw_category.get("items") or raw_category.get("menu_items") or raw_category.get("dishes") or []
        items = []
        for raw_item in raw_items if isinstance(raw_items, (list, tuple)) else []:
            raw_item = _coerce_item(raw_item)
            if raw_item is None:
                continue
            name = str(raw_item.get("name") or raw_item.get("dish") or raw_item.get("item_name") or raw_item.get("title") or "").strip()
            key = re.sub(r"\s+", "", name).casefold()
            price = raw_item.get("price")
            if price is None:
                price = raw_item.get("amount") if raw_item.get("amount") is not None else raw_item.get("cost")
            clean_price = _clean_price(price)
            # 單字品名在加料吊牌與小攤菜單上很常見——不倒翁的木板就是「麵 20」
            # 「蛋 20」。舊的 len(name) < 2 一律丟掉，而且不留 warning，所以那兩
            # 塊牌子不管模型有沒有讀到都不會出現在結果裡。但一個字又沒有價格的
            # 多半是 OCR 撿到的雜訊，所以只在有價格時放行。
            if not name or key in seen:
                continue
            if len(name) < 2 and clean_price is None:
                continue
            seen.add(key)
            item: Dict[str, Any] = {"name": name[:160], "price": clean_price}
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
    """EXIF 轉正、把長邊正規化到 TARGET_LONG_SIDE，回傳單一整圖區塊。

    通訊軟體與剪貼簿常把一張看得清楚的 2500px 菜單壓成 840px，所以小圖要放
    大；手機原檔又常是 4032px，送過去只是讓每次呼叫多扛 1MB。兩邊都收斂到
    同一個長邊最單純。
    """
    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    target = _int_env("VISION_LONG_SIDE", TARGET_LONG_SIDE)
    long_side = max(image.size)
    if long_side >= MIN_UPSCALE_LONG_SIDE and long_side != target:
        scale = target / long_side
        image = image.resize(
            (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
            Image.Resampling.LANCZOS,
        )
        if scale > 1:
            # 放大會糊，銳化一下讓筆畫邊緣回來。縮小不需要。
            image = image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))

    width, height = image.size
    full = _encode_jpeg(image)
    # VISION_TILES=0 完全關掉切塊。--debug 記錄顯示切塊在直式菜單上是主動有害
    # 的：budaoweng 的木板品名在上、價格在下，2×2 的水平切線攔腰砍過，四塊
    # 分別吐出 null 價格、空陣列、以及「北」「噌」「青」這種被切一半的字，
    # 最後全靠校對看完整張圖救回來。四個 eval 案例比對下來，切塊多花的 4 次
    # 呼叫沒有多讀到任何一項。
    if not _tiling_enabled() or max(width, height) <= TILE_THRESHOLD:
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


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


def _tiling_enabled() -> bool:
    """切塊預設關閉，見 analyze_menu_image 的說明。VISION_TILES=1 可以開回來。"""
    return os.getenv("VISION_TILES", "0").strip().lower() in {"1", "true", "yes", "on"}


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
    # schema 刻意不用 `name` 當 key——閘道會把帶 name 的輸出 JSON 轉成 tool_calls，
    # content 變成空的，整個切塊白跑。詳見 _to_wire_categories 的說明。
    schema = (
        '{{"restaurant_name":"","categories":[{{"title":"分類","items":[{{"dish":"完整品名","price":230}}]}}]}}'
        if ask_identity
        else '{{"categories":[{{"title":"分類","items":[{{"dish":"完整品名","price":230}}]}}]}}'
    )
    where = (
        "這是整張菜單的完整照片。"
        if tile_id == "full"
        else f"這是原圖的高解析區塊 {tile_id}，原圖座標 {list(box)}。"
    )
    return f"""你是繁體中文菜單 OCR 專家。{where}
全圖初步資訊：{json.dumps(overview, ensure_ascii=False)}
逐字抄錄所有主要餐點品名與印刷價格。保留印刷的繁體字，不要憑圖片猜食材，不要加入產地、克數、電話或說明文字。
被手寫線劃過的印刷品項仍要保留。看不清價格用 null；不要猜數字。
共用價格框：價格若寫在括號或圓角框裡，標著大／小（或大碗小碗、L／S），而且那個框在版面上涵蓋整欄或一整組品項，代表那一組的每一項都同時有這兩種價格，而不是上面幾項算一種、下面幾項算另一種。遇到這種情形要為每個尺寸各輸出一項，品名後面加上（大）或（小）——例如同一欄的「乾麵」要輸出「乾麵（大）」55 與「乾麵（小）」45 兩項。
配料與加點若自己掛一塊牌子、或自己佔一行並帶著自己的價格（例如吊牌上的「麵 20元」「蛋 20元」），那就是品項，品名只有一兩個字也一樣要列。但附在分類標題或某一項旁邊的加價說明（例如「（加蛋10元）」「加大+10」）是註解不是品項，不要列。{identity_rule}
只回傳 JSON：{schema}"""


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
            # 用 dish 不用 name：閘道看到 name 會把輸出轉成 tool_calls。
            '讀取圖片中可見的菜名與價格，只回傳 JSON：'
            '{"menu_items":[{"dish":"品名","price":0}]}',
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
    """一次呼叫讀完整張菜單。

    這裡曾經是「總覽 → 2×2 切塊並行 OCR → 最終校對」的六次呼叫流程。四個
    eval 案例、131 項對照答案的實測顯示，那套架構是在用呼叫次數補償解析度
    不足——把輸入放大到 4000px 再單次呼叫，每個維度都不輸：

        指標          六次呼叫   單次 @4000
        召回率          82.4%      82.4%    漏掉的是同一批品項，一項不差
        精確率          99.1%      99.1%
        價格正確率      86.1%      96.3%    +10.2pt
        店名            3/3        3/3
        總耗時         367.8s      74.7s

    切塊贏的地方不是「分開讀」，而是每塊只涵蓋四分之一畫面，文字在模型固定
    的圖片 token 預算裡佔得比較多。放大輸入就有同樣效果，而且不會像切塊那樣
    弄壞直式版面——budaoweng 的木板品名在上價格在下，2x2 的水平切線攔腰砍
    過，四塊分別吐出 null 價格、空陣列、和「北」「噌」「青」這種半個字。

    要回到舊架構：git checkout snapshot/6call-baseline
    """
    if not image_bytes:
        raise ValueError("圖片內容不可為空")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("圖片不可超過 10 MB")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_IMAGE_TYPES:
        raise ValueError("僅支援 JPEG、PNG、WebP 或 HEIC 圖片")

    started = time.perf_counter()
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

    requested_ocr_model = os.getenv("VISION_MODEL", DEFAULT_OCR_MODEL)
    verify_model = os.getenv("VISION_VERIFY_MODEL", DEFAULT_VERIFY_MODEL)
    active_ocr_model = requested_ocr_model
    ocr_fallback_warning = ""

    prompt = _tile_prompt("full", [0, 0, *image_size], {}, ask_identity=True)
    prepare_ms = round((time.perf_counter() - started) * 1000, 1)
    image_url = _data_url(full_bytes, "image/jpeg")
    ocr_started = time.perf_counter()
    try:
        response = _call_vision(
            vision_func,
            prompt,
            image_url,
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
        response = _call_vision(vision_func, prompt, image_url, model=verify_model, temperature=0.0)

    ocr_ms = round((time.perf_counter() - ocr_started) * 1000, 1)
    parsed, parsed_ok = _extract_json_document(response)
    result = normalize_vision_result(parsed, restaurant_hint)
    categories = result["categories"]
    if not categories:
        # 解析失敗與「照片裡真的沒有菜單」原本共用同一句話，但一個要查模型輸出、
        # 一個要換張照片，對使用者和對開發者的意義都不同。
        if not parsed_ok:
            raise ValueError("模型回應無法解析成菜單資料，請再試一次")
        raise ValueError("照片中沒有辨識到可用的菜單或菜色")

    detected = str(result.get("detected_restaurant_name") or "").strip()
    item_count = sum(len(category["items"]) for category in categories)
    priced = sum(item.get("price") is not None for category in categories for item in category["items"])
    coverage = round(priced / item_count, 3) if item_count else 0.0
    quality_score = round(min(1.0, coverage * 0.55 + min(item_count / 20, 1) * 0.25 + 0.2), 3)
    if ocr_fallback_warning:
        # 降級可以讓流程走完，但不能假裝成高信心的主要辨識結果。
        quality_score = min(quality_score, 0.7)

    identity_conflict = _names_conflict(restaurant_hint, detected)
    warnings = [str(v)[:200] for v in (result.get("warnings") or []) if str(v).strip()]
    if identity_conflict:
        warnings.append(f"使用者店名「{restaurant_hint}」與圖片候選「{detected}」不同，確認前不會存檔")
    if ocr_fallback_warning:
        warnings.append(ocr_fallback_warning)

    emit(
        "menu_vision.analyzed",
        model=active_ocr_model,
        modelFallback=bool(ocr_fallback_warning),
        # prepareMs 是本機縮放，ocrMs 才是模型呼叫——兩者分開才看得出慢在哪。
        prepareMs=prepare_ms,
        ocrMs=ocr_ms,
        totalMs=round((time.perf_counter() - started) * 1000, 1),
        imageSize=list(image_size),
        itemCount=item_count,
        priceCoverage=coverage,
        qualityScore=quality_score,
    )

    return {
        "restaurant_name": (restaurant_hint.strip() or detected)[:120],
        "detected_restaurant_name": detected[:120],
        "source_type": str(result.get("source_type") or "menu"),
        "confidence": quality_score,
        "categories": categories,
        "warnings": warnings[:8],
        "quality": {
            "score": quality_score,
            "priceCoverage": coverage,
            "itemCount": item_count,
            # 沒有切塊就沒有重疊區，這兩個計數永遠是 0。欄位保留是因為前端與
            # 待確認流程的回應 schema 還在讀。
            "conflictCount": 0,
            "modelFallback": bool(ocr_fallback_warning),
            "verifyDroppedCount": 0,
            "verifyInventedCount": 0,
        },
        "conflicts": [],
        "identity": {
            "userHint": restaurant_hint.strip(),
            "detectedName": detected[:120],
            "candidates": list(dict.fromkeys(v for v in (detected[:120], restaurant_hint.strip()) if v)),
            "candidateInferred": False,
            "menuType": "",
        },
        "identityConflict": identity_conflict,
        "models": {"ocrRequested": requested_ocr_model, "ocr": active_ocr_model},
        "sourceBlocks": [{"id": r["id"], "box": r["box"]} for r in regions],
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
