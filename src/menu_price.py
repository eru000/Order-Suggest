"""菜單價格的單一解析規則（期末修正規劃 契約 C1）。

以前有兩套解析：``menu_vision._clean_price`` 與
``MenuIngestionService.normalize_price``。兩者都「抓字串裡第一段數字」，
於是：

    "1?0"       -> 1.0     看不清的價格變成 1 元，還因為最便宜被優先推薦
    "100～190"  -> 100.0   範圍被截成單價
    "大60/小45" -> 60.0    多種價格只留第一個
    "約150"     -> 150.0   約略價當成明確價

預算判斷吃的是數字，任何「猜出來的單價」都會一路流到推薦結果。這裡的原則是：
**只有能確定是單一明確價格時才給數字**，其餘一律保留狀態與原文，交給人工確認
或推薦器的「價格未知」路徑。

``item["price"]`` 仍然只放 ``value``（fixed 才有數字），所以推薦器、評測器
不用改就相容；``item["priceInfo"]`` 保留完整判斷。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, TypedDict


PRICE_STATUSES = ("fixed", "market", "range", "multi", "unreadable", "missing")


class PriceVariant(TypedDict):
    label: str
    value: float


class PriceInfo(TypedDict):
    raw: str | None
    value: float | None
    status: str
    variants: list[PriceVariant]
    reason: str


_MARKET_WORDS = ("時價", "市價", "季節價", "marketprice")
_UNREADABLE_WORDS = ("看不清", "不清楚", "模糊", "無法辨識", "illegible", "unreadable")
_MISSING_WORDS = ("未知", "unknown", "未提供", "未標示", "未標價", "不明")
_MISSING_EXACT = {"", "null", "none", "n/a", "na", "-", "--", "—", "無", "空"}
# 模型被要求「看不清的數字用 ? 代替」；□ 與 � 是 OCR 與編碼失敗常見的替代字元。
_UNCERTAIN_CHARS = "?？□■�"
# 「起」要排除「起司」；英文只認獨立的字，避免 cup 之類誤中。
_APPROX = re.compile(r"約|起(?!司)|以上|左右|\bup\b|\bfrom\b", re.IGNORECASE)
# 數字後面接這些單位時是份量、人數、熱量，不是價格：「2-3人份 680」「1/2隻 300」。
_QUANTITY = re.compile(
    r"\d+(?:\.\d+)?(?:\s*[-~～〜至到/]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:人份|人|份|入|顆|片|粒|支|隻|尾|兩|斤|公克|克|kg|g|ml|cc|公升|大卡|kcal|cal|吋|寸|oz|%|折|分鐘|min)",
    re.IGNORECASE,
)
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_RANGE_GAP = re.compile(r"^\s*(?:元|塊)?\s*[~～〜\-–—至到]\s*(?:NT\$?|\$)?\s*$", re.IGNORECASE)
_LABEL_JUNK = re.compile(r"[\s/、,，;；|:：()（）\[\]【】$元+]|NT|NTD", re.IGNORECASE)


def _info(raw: str | None, status: str, reason: str, value: float | None = None,
          variants: list[PriceVariant] | None = None) -> PriceInfo:
    return {"raw": raw, "value": value, "status": status, "variants": variants or [], "reason": reason}


def _number_info(value: float, raw: str | None) -> PriceInfo:
    if math.isnan(value) or math.isinf(value) or value < 0:
        return _info(raw, "missing", "不是有效的非負數字")
    # 數字 0 照舊保留：推薦器另有「price == 0 視為時價」的既有慣例會接手。
    return _info(raw, "fixed", "明確單價", float(value))


def _clean_label(text: str) -> str:
    return _LABEL_JUNK.sub("", text).strip()[:12]


def _parse_text(raw: str) -> PriceInfo:
    text = unicodedata.normalize("NFKC", raw).strip()
    folded = re.sub(r"\s+", "", text).casefold()

    if folded in _MISSING_EXACT:
        return _info(raw, "missing", "沒有價格")
    if any(word in folded for word in _MARKET_WORDS):
        return _info(raw, "market", "時價或市價")
    if any(char in text for char in _UNCERTAIN_CHARS) or any(w in folded for w in _UNREADABLE_WORDS):
        return _info(raw, "unreadable", "價格含無法辨識的字元")

    scrubbed = _QUANTITY.sub(" ", _THOUSANDS.sub("", text))
    matches = list(_NUMBER.finditer(scrubbed))
    if not matches:
        if any(word in folded for word in _MISSING_WORDS):
            return _info(raw, "missing", "標示為未知或未提供")
        return _info(raw, "missing", "沒有可辨識的數字")

    if len(matches) == 1:
        value = float(matches[0].group())
        if _APPROX.search(text):
            return _info(raw, "range", "約略價或起價，不是確定單價")
        return _info(raw, "fixed", "明確單價", value)

    if len(matches) == 2 and _RANGE_GAP.match(scrubbed[matches[0].end():matches[1].start()]):
        return _info(raw, "range", "價格範圍，不是確定單價")

    starts = [0] + [m.end() for m in matches[:-1]]
    ends = [m.start() for m in matches[1:]] + [len(scrubbed)]
    before = [_clean_label(scrubbed[s:m.start()]) for s, m in zip(starts, matches)]
    after = [_clean_label(scrubbed[m.end():e]) for m, e in zip(matches, ends)]
    # 「大60/小45」標籤在數字前；「60(大)/45(小)」標籤在數字後。看第一個數字前面
    # 有沒有字決定整串用哪一種，不能逐項混用，否則第二項會撿到第一項的標籤。
    labels = before if before[0] else after
    variants: list[PriceVariant] = [
        {"label": label, "value": float(match.group())} for label, match in zip(labels, matches)
    ]
    return _info(raw, "multi", "同一品項有多個價格，需拆成規格", variants=variants)


def parse_price(value: Any) -> PriceInfo:
    """把模型、爬蟲或人工輸入的價格轉成 PriceInfo。

    只有 status == "fixed" 時 value 才是數字；其餘狀態 value 一律為 None。
    """
    if value is None:
        return _info(None, "missing", "沒有價格")
    if isinstance(value, bool):
        return _info(str(value), "missing", "布林值不是價格")
    if isinstance(value, (int, float)):
        return _number_info(float(value), None)
    if isinstance(value, (list, tuple)):
        parsed = [parse_price(part) for part in value]
        fixed = [p for p in parsed if p["status"] == "fixed" and p["value"] is not None]
        if len(parsed) == 1:
            return parsed[0]
        if fixed and len(fixed) == len(parsed):
            return _info(str(list(value)), "multi", "同一品項有多個價格，需拆成規格",
                         variants=[{"label": "", "value": float(p["value"] or 0)} for p in fixed])
        return _info(str(list(value)), "unreadable", "價格清單中有無法解析的值")
    if isinstance(value, dict):
        for key in ("price", "value", "amount"):
            if key in value:
                return parse_price(value[key])
        variants: list[PriceVariant] = []
        for label, part in value.items():
            parsed_part = parse_price(part)
            if parsed_part["status"] != "fixed" or parsed_part["value"] is None:
                return _info(str(value), "unreadable", "規格價格中有無法解析的值")
            variants.append({"label": str(label)[:12], "value": parsed_part["value"]})
        if len(variants) == 1:
            return _info(str(value), "fixed", "明確單價", variants[0]["value"])
        if variants:
            return _info(str(value), "multi", "同一品項有多個價格，需拆成規格", variants=variants)
        return _info(str(value), "missing", "沒有價格")
    return _parse_text(str(value))


def price_value(value: Any) -> float | None:
    """只要數字的呼叫端用這個；非 fixed 一律回 None。"""
    return parse_price(value)["value"]
