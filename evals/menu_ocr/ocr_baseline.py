"""A 組基線：OCR＋版面配對（期末規劃節點 5，回應委員3-2、3-4、5-2）。

委員問的是「為什麼用 VLM 而不用成熟的 OCR」。公平的比較必須讓 OCR 也完成同一個
任務——輸出「菜名＋價格」的結構化品項——再用同一套真值與計分器評分，而不是只比
「字認得準不準」。

管線
1. RapidOCR：PaddleOCR 的 PP-OCR 偵測＋辨識模型，以 ONNX Runtime 執行（CPU）。
   專案的 Windows 環境是 Python 3.14，PaddlePaddle 本體不一定能裝；ONNX 版本的模型
   與 PaddleOCR 相同，且能在任何 Python 環境重現。
2. OpenCC s2tw：PP-OCR 中文模型常輸出簡體字形（魯→鲁），先轉回繁體，避免把字形差
   異算成 OCR 的辨識錯誤。
3. 版面配對：把每一行文字分成「價格」「品名」「分類標題」「註記」；品名與同一列、
   位於其右方最近的價格配對；只有括號的小字註記（如「（外帶）」）接到正下方的品名前。

配對規則只用開發資料設計與檢查（期末規劃 全域規則 5）。
"""

from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

_ENGINE: Any = None
_CONVERTER: Any = None

# OCR 常把「NT$」讀成「NTs」。
_CURRENCY = r"(?:NT\s*[$sS＄]?|\$|＄)"
_PRICE_ONLY = re.compile(r"^" + _CURRENCY + r"?\s*(\d{1,5})(?:\.\d+)?\s*(?:元|/份)?$", re.I)
_MARKET_ONLY = re.compile(r"^(?:時價|時價供應|季節價)$")
_INLINE_PRICE = re.compile(
    r"^(.*?[^\d\s$＄.])\s*" + _CURRENCY + r"?\s*(\d{2,5})(?:\.\d+)?\s*元?$", re.I
)
_CJK = re.compile(r"[\u3400-\u9fff]")
# 規格與價格寫在同一行：「小90　中180」「大270」「L 60 M 50」
_SIZE_LABEL = r"(特大|[大中小](?:碗|份|杯)?|單人|雙人|[LMS])"
_SIZE_PAIR = re.compile(_SIZE_LABEL + r"\s*" + r"(?:NT\s*[$sS＄]?|\$|＄)?\s*(\d{2,5})", re.I)
_SIZE_LINE = re.compile(r"^(?:\s*" + _SIZE_LABEL + r"\s*(?:NT\s*[$sS＄]?|\$|＄)?\s*\d{2,5}\s*元?)+$", re.I)
# 中英對照菜單：中文品名後面接的英文翻譯不是品名的一部分。
_TRAILING_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9 ,.'&/\-]*$")
_HEADER_NOISE = re.compile(r"單價|單価|數量|数量|價格|價目|^價$|^售價$|price|qty", re.I)
_CATEGORY = re.compile(r"^.{1,6}(?:類|类|區|区|系列|專區)$")
_NOTE = re.compile(r"^[（(【\[].{1,6}[)）】\]]$")
_HAS_WORD = re.compile(r"[㐀-鿿A-Za-z]")


def engine_info() -> dict[str, str]:
    from importlib import metadata

    info = {"ocrEngine": "rapidocr-onnxruntime (PP-OCR models)", "ocrPostprocess": "opencc s2tw"}
    for package in ("rapidocr-onnxruntime", "onnxruntime", "opencc-python-reimplemented"):
        try:
            info[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            info[package] = "missing"
    return info


def _engine() -> Any:
    global _ENGINE, _CONVERTER
    if _ENGINE is None:
        from opencc import OpenCC
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
        _CONVERTER = OpenCC("s2tw")
    return _ENGINE


def ocr_lines(image_bytes: bytes) -> list[dict[str, Any]]:
    """回傳每一行：text（已轉繁體）、raw（OCR 原文）、box=(x0, y0, x1, y1)、conf。"""
    import numpy as np
    from PIL import Image, ImageOps

    engine = _engine()
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    result, _ = engine(np.array(image))
    lines = []
    for box, text, conf in result or []:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        raw = str(text).strip()
        lines.append({
            "text": _CONVERTER.convert(raw).replace(" ", ""),
            "raw": raw,
            "box": (min(xs), min(ys), max(xs), max(ys)),
            "conf": round(float(conf), 4),
        })
    return lines


def _kind(text: str) -> str:
    if _PRICE_ONLY.match(text) or _MARKET_ONLY.match(text):
        return "price"
    if _SIZE_LINE.match(text):
        return "sizeprice"
    if not _HAS_WORD.search(text) or _HEADER_NOISE.search(text):
        return "noise"
    if _NOTE.match(text):
        return "note"
    if _CATEGORY.match(text):
        return "category"
    return "name"


def _sub_box(entry: dict[str, Any], start: int, end: int) -> tuple[float, float, float, float]:
    """一行文字中第 start～end 個字的大致位置（依字數平均切分）。"""
    x0, y0, x1, y1 = entry["box"]
    width = (x1 - x0) / max(1, len(entry["text"]))
    return (x0 + start * width, y0, x0 + end * width, y1)


def _digits_box(entry: dict[str, Any]) -> tuple[float, float, float, float]:
    """價格行裡數字本身的位置（去掉 NT$、$ 等前綴與「元」）。"""
    match = re.search(r"\d+", entry["text"])
    if not match:
        return entry["box"]
    return _sub_box(entry, match.start(), match.end())


def _center_y(box: tuple[float, float, float, float]) -> float:
    return (box[1] + box[3]) / 2


def _height(box: tuple[float, float, float, float]) -> float:
    return max(1.0, box[3] - box[1])


def pair_items(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 OCR 行配成 {"dish", "price", "category"}。"""
    cjk_lines = sum(1 for line in lines if _CJK.search(line["text"]))
    mostly_chinese = cjk_lines >= 0.5 * max(1, len(lines))
    entries = []
    for line in lines:
        text = re.sub(r"^[*＊·•．.]+", "", line["text"])
        if _CJK.search(text):
            text = _TRAILING_LATIN.sub("", text).strip() or text
        kind = _kind(text)
        if kind == "name" and mostly_chinese and not _CJK.search(text):
            kind = "noise"
        inline_price = None
        if kind == "name":
            inline = _INLINE_PRICE.match(text)
            if inline:
                text, inline_price = inline.group(1), inline.group(2)
        entries.append({**line, "text": text, "kind": kind, "inlinePrice": inline_price})

    names = [entry for entry in entries if entry["kind"] == "name"]
    size_lines = [entry for entry in entries if entry["kind"] == "sizeprice"]
    prices = [entry for entry in entries if entry["kind"] == "price"]
    notes = [entry for entry in entries if entry["kind"] == "note"]
    categories = sorted(
        (entry for entry in entries if entry["kind"] == "category"), key=lambda e: e["box"][1]
    )

    def same_row(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
        overlap = min(a[3], b[3]) - max(a[1], b[1])
        return overlap >= 0.4 * min(_height(a), _height(b))  # type: ignore[arg-type]

    def blocked(name: dict[str, Any], x_from: float, x_to: float) -> bool:
        """同一列中間夾著另一個品名，代表價格屬於隔壁那一欄。"""
        return any(
            other is not name and same_row(name["box"], other["box"])
            and x_from <= other["box"][0] < x_to
            for other in names
        )

    # 同一列右方的「規格＋價格」行（可能被 OCR 切成好幾段）全部接到品名後面。
    size_variants: dict[int, list[tuple[str, str, Any]]] = {}
    for name_index, name in enumerate(names):
        nx0, _, nx1, _ = name["box"]
        row = sorted(
            (line for line in size_lines
             if same_row(name["box"], line["box"]) and line["box"][0] >= nx1 - 0.3 * (nx1 - nx0)
             and not blocked(name, nx1 - 0.3 * (nx1 - nx0), line["box"][0])),
            key=lambda line: line["box"][0],
        )
        pairs = [
            (match.group(1), match.group(2), _sub_box(line, match.start(2), match.end(2)))
            for line in row for match in _SIZE_PAIR.finditer(line["text"])
        ]
        if pairs:
            size_variants[name_index] = pairs

    # 同一列、在右方、中間沒有隔著別的品名的價格；每個價格只配一次。
    row_prices: dict[int, list[int]] = {}
    candidates = []
    for name_index, name in enumerate(names):
        if name["inlinePrice"] is not None or name_index in size_variants:
            continue
        nx0, ny0, nx1, ny1 = name["box"]
        for price_index, price in enumerate(prices):
            px0 = price["box"][0]
            if not same_row(name["box"], price["box"]) or px0 < nx1 - 0.3 * (nx1 - nx0):
                continue
            if blocked(name, nx1 - 0.3 * (nx1 - nx0), px0):
                continue
            row_prices.setdefault(name_index, []).append(price_index)
            candidates.append((px0 - nx1, name_index, price_index))
    candidates.sort()
    price_of: dict[int, str] = {}
    price_box_of: dict[int, Any] = {}
    used: set[int] = set()
    def price_value(entry: dict[str, Any]) -> str | None:
        match = _PRICE_ONLY.match(entry["text"])
        return match.group(1) if match else None  # 時價：配對但價格未知

    paired: set[int] = set()
    for _, name_index, price_index in candidates:
        if name_index in paired or price_index in used:
            continue
        price_of[name_index] = price_value(prices[price_index])  # type: ignore[assignment]
        price_box_of[name_index] = _digits_box(prices[price_index])
        paired.add(name_index)
        used.add(price_index)

    # 價格印在品名下方（套餐卡片版型）：只給字級明顯較大的標題行配對，避免說明文字
    # 搶走價格。字級較大的才是品項標題，這是排版慣例，不是針對特定菜單。
    heights = sorted(_height(name["box"]) for name in names)
    median_height = heights[len(heights) // 2] if heights else 0.0
    below = []
    for name_index, name in enumerate(names):
        if name_index in paired or name["inlinePrice"] is not None:
            continue
        if _height(name["box"]) < 1.2 * median_height:
            continue
        nx0, _, nx1, ny1 = name["box"]
        for price_index, price in enumerate(prices):
            if price_index in used:
                continue
            px0, py0, _, _ = price["box"]
            if 0 <= py0 - ny1 <= 10 * _height(name["box"]) and nx0 - (nx1 - nx0) <= px0:
                below.append((((px0 - nx0) ** 2 + (py0 - ny1) ** 2) ** 0.5, name_index, price_index))
    for _, name_index, price_index in sorted(below):
        if name_index in paired or price_index in used:
            continue
        price_of[name_index] = price_value(prices[price_index])  # type: ignore[assignment]
        price_box_of[name_index] = _digits_box(prices[price_index])
        paired.add(name_index)
        used.add(price_index)

    # 括號小字註記接到正下方、水平重疊的品名前面：「（外帶）」＋「大魯肉飯」
    prefix_of: dict[int, str] = {}
    for note in notes:
        bx0, _, bx1, by1 = note["box"]
        best = None
        for name_index, name in enumerate(names):
            nx0, ny0, nx1, _ = name["box"]
            gap = ny0 - by1
            if -0.3 * _height(note["box"]) <= gap <= 1.5 * _height(note["box"]) and min(bx1, nx1) > max(bx0, nx0):
                if best is None or gap < best[0]:
                    best = (gap, name_index)
        if best is not None and best[1] not in prefix_of:
            prefix_of[best[1]] = note["text"]

    # 一列有多個價格（小／中／大欄）：用價格欄正上方最近的短標題當規格名稱。
    variants_of: dict[int, list[tuple[str, str | None, Any]]] = {}
    for name_index, chain in row_prices.items():
        if len(chain) < 2:
            continue
        labelled = []
        for price_index in sorted(chain, key=lambda i: prices[i]["box"][0]):
            px0, py0, px1, _ = prices[price_index]["box"]
            heads = [
                entry for entry in entries
                if entry["kind"] in {"name", "note", "noise"} and len(entry["text"]) <= 3
                and entry["box"][3] <= py0 and min(entry["box"][2], px1) > max(entry["box"][0], px0)
            ]
            if not heads:
                labelled = []
                break
            head = max(heads, key=lambda entry: entry["box"][3])
            labelled.append(
                (head["text"].strip("()（）"), price_value(prices[price_index]), _digits_box(prices[price_index]))
            )
        if labelled and len({label for label, _, _ in labelled}) == len(labelled):
            variants_of[name_index] = labelled
            used.update(chain)

    # 大部分品名都配到價格時，沒配到的多半是說明文字、地址或標語，不列為品項；
    # 配到價格的品名不多時（套餐卡片），改用字級判斷：比已配對品名小的行是說明文字。
    for name_index, pairs in size_variants.items():
        variants_of[name_index] = list(pairs)
    priced_names = [
        names[i] for i in range(len(names))
        if i in paired or i in variants_of or names[i]["inlinePrice"] is not None
    ]
    priced_share = len(priced_names) / max(1, len(names))
    priced_heights = sorted(_height(name["box"]) for name in priced_names)
    title_height = priced_heights[len(priced_heights) // 2] if priced_heights else 0.0
    items = []
    for name_index, name in enumerate(names):
        has_price = name_index in paired or name_index in variants_of or name["inlinePrice"] is not None
        if not has_price and (
            priced_share >= 0.5
            or (len(priced_names) >= 3 and _height(name["box"]) < 0.85 * title_height)
        ):
            continue
        if name_index in variants_of:
            for label, value, box in variants_of[name_index]:
                items.append({
                    "dish": f"{name['text']}（{label}）", "price": value, "category": "", "priceBox": box,
                })
            continue
        category = ""
        for header in categories:
            if header["box"][1] <= name["box"][1]:
                category = header["text"]
        inline = name["inlinePrice"]
        if inline is not None:
            # 品名與價格同一行：價格是行尾那幾個字
            raw = line_text = name["text"] + inline
            box = _sub_box({"text": raw, "box": name["box"]}, len(line_text) - len(inline), len(line_text))
        else:
            box = price_box_of.get(name_index)
        items.append({
            "dish": prefix_of.get(name_index, "") + name["text"],
            "price": inline if inline is not None else price_of.get(name_index),
            "category": category,
            "priceBox": box,
        })
    return items


def analyze(image_bytes: bytes, content_type: str = "", restaurant_hint: str = "", **_: Any) -> dict[str, Any]:
    """與 menu_vision.analyze_menu_image 相同的輸出格式，交給同一個計分器。"""
    from menu_vision import normalize_vision_result, review_counts

    started = time.perf_counter()
    lines = ocr_lines(image_bytes)
    items = pair_items(lines)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["category"] or "菜單", []).append(
            {"dish": item["dish"], "price": item["price"]}
        )
    result = normalize_vision_result(
        {"categories": [{"title": title, "items": rows} for title, rows in grouped.items()]},
        restaurant_hint,
    )
    if not result["categories"]:
        raise ValueError("照片中沒有辨識到可用的菜單或菜色")
    count = sum(len(category["items"]) for category in result["categories"])
    priced = sum(
        item.get("price") is not None for category in result["categories"] for item in category["items"]
    )
    result.update({
        "quality": {
            "itemCount": count,
            "priceCoverage": round(priced / count, 3) if count else 0.0,
            **review_counts(result),
        },
        "models": {"ocr": "rapidocr-onnxruntime"},
        "ocrLines": lines,
        "ocrSeconds": round(time.perf_counter() - started, 3),
    })
    return result
