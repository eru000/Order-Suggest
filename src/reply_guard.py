"""LLM 推薦說明的輸出防護（期末規劃第六項後續，回應委員5-9）。

第六項的 LLM 消融（evals/results/step6/llm-ablation-scored.json）顯示：同一份
結構化推薦交給 LLM 改寫成口語，10 則只有 4 則忠實呈現；其餘 6 則省略推薦品項、
換成別的菜、或寫錯數量與總額。模板 10/10 忠實。推薦本身完全由規則決定，所以
LLM 只該負責「怎麼說」，不能改「推薦什麼」。

這裡把消融用的自動檢查搬進產品，作為送出前的防護：

1. 每一道推薦品項都要出現在回覆裡（容許省略品名後面的括號，例如英文名）。
2. 回覆中的金額都要對得上推薦資料：單價、小計、合計、預算、服務費、剩餘預算、
   每人金額，容許四捨五入 1 元。
3. 不能提到「菜單上有、但這次沒有推薦」的菜（LLM 換菜的情形）。

任何一項不通過就改用模板回覆，並記錄原因。這只用在推薦說明；開放問答
（decision_context）本來就可能談到推薦以外的菜，不套用。

規則是依消融觀察到的三種失敗類型定的，用同一批 10 則驗證，不是獨立驗證；
見 log.txt 2026-11-27「LLM 輸出防護」。
"""

from __future__ import annotations

import re
from typing import Any

_MONEY = re.compile(
    r"(?:NT\$|\$|＄)\s*([\d,]+(?:\.\d+)?)"
    r"|([\d,]+(?:\.\d+)?)(?!\s*(?:%|％|人|位|道|份|杯|瓶|盤|碗|分鐘|個))"
)
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "萬": 10000}
_CN_RUN = re.compile(r"[零〇一二兩三四五六七八九十百千萬]+")
_NAME_SUFFIX = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")
# 個位數多半是人數、杯數、道數，不當成金額
MIN_MONEY = 10
TOLERANCE = 1.0
# 短於這個長度的菜名（「飲料」「白飯」）同時是通稱，不能拿來判斷換菜
MIN_INVENTED_NAME = 3


def _cn_number(run: str) -> int | None:
    """「八百一十」→810、「一千」→1000、「六九三」→693（逐位念法）。"""
    if all(ch in _CN_DIGITS for ch in run):
        return int("".join(str(_CN_DIGITS[ch]) for ch in run)) if len(run) >= 2 else None
    total = section = digit = 0
    last_unit = 1
    for ch in run:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
        else:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + digit) * unit
                section = digit = 0
            else:
                section += (digit or 1) * unit
                digit = 0
            last_unit = unit
    # 「八百一」＝810、「一千二」＝1200：尾數接在最後一個單位的下一位
    if digit:
        section += digit * (last_unit // 10 if last_unit >= 100 else 1)
    return total + section or None


def to_arabic(text: str) -> str:
    """把中文數字金額轉成阿拉伯數字，LLM 常寫「三百」「八百一十」。"""
    def repl(match: re.Match[str]) -> str:
        value = _cn_number(match.group(0))
        return str(value) if value is not None else match.group(0)
    return _CN_RUN.sub(repl, text)


_CJK = re.compile(r"[\u4e00-\u9fff]")
_INNER = re.compile(r"[（(]([^（）()]*)[）)]")
# 推薦品名沒有逐字出現時，品名的開頭或結尾至少一半（且至少兩個字）出現在回覆裡
# 就算有提到：「七喜汽水」寫成「七喜」、「香辣牛肚絲」寫成「牛肚絲」。只看開頭與
# 結尾，是因為中間一段常是別道菜：「當歸松阪豬肉湯」的「松阪豬肉」也是
# 「松阪豬肉飯」的一部分（2026-11-27 以第六項的 10 則 LLM 回覆實測）。
MIN_PARTIAL_RATIO = 0.5
MIN_PARTIAL_CHARS = 2


def _partial_hit(alias: str, text: str) -> bool:
    shortest = max(MIN_PARTIAL_CHARS, -(-len(alias) * MIN_PARTIAL_RATIO // 1))
    for size in range(len(alias) - 1, int(shortest) - 1, -1):
        if alias[:size] in text or alias[-size:] in text:
            return True
    return False


def _aliases(name: str) -> list[str]:
    """品名的幾種寫法：全名、去掉括號、括號裡的中文（「Salmon Bibimbap (玉子鮭魚石鍋飯)」）。"""
    out = [name, _core_name(name)]
    out += [inner.strip() for inner in _INNER.findall(name) if _CJK.search(inner)]
    return [alias for alias in dict.fromkeys(out) if alias]


def is_mentioned(name: str, text: str) -> bool:
    lowered = text.lower()
    for alias in _aliases(name):
        if alias.lower() in lowered:
            return True
        if _CJK.search(alias) and _partial_hit(alias, text):
            return True
    return False


def _core_name(name: str) -> str:
    """「綜合野菜 (Vegetables Combo)」→「綜合野菜」。"""
    while _NAME_SUFFIX.search(name):
        stripped = _NAME_SUFFIX.sub("", name)
        if not stripped:
            break
        name = stripped
    return name.strip()


def menu_item_prices(menu: dict[str, Any] | None) -> dict[str, float | None]:
    """菜單上的品名與標價，排除與分類名稱相同的（「飲料」「季節時蔬」同時是分類標籤）。"""
    if not isinstance(menu, dict):
        return {}
    from recommendation import _flatten_menu

    items = _flatten_menu(menu)
    categories = {str(item.get("category") or "") for item in items}
    prices: dict[str, float | None] = {}
    for item in items:
        name = str(item["name"])
        if name in categories:
            continue
        price = item.get("price")
        prices[name] = float(price) if isinstance(price, (int, float)) else None
    return prices


def menu_item_names(menu: dict[str, Any] | None) -> list[str]:
    return sorted(menu_item_prices(menu), key=len, reverse=True)


def fact_check(text: str, rec: dict[str, Any], menu_names: list[str]) -> dict[str, Any]:
    """回覆與推薦資料的對照。llm_ablation.py 與產品防護共用這一支。"""
    items = [item for item in rec.get("items") or [] if isinstance(item, dict)]
    names = [str(item.get("name") or "") for item in items]
    mentioned = [name for name in names if name and is_mentioned(name, text)]
    missing = [name for name in names if name and name not in mentioned]
    meta = rec.get("meta") or {}
    meta = meta if isinstance(meta, dict) else {}
    allowed: set[float] = set()
    for item in items:
        for key in ("price", "total", "lineTotal"):
            value = item.get(key)
            if isinstance(value, (int, float)):
                allowed.add(round(float(value), 2))

    def number(key: str) -> float | None:
        value = meta.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    budget = number("budget")
    subtotal, total = number("estimatedSubtotal"), number("estimatedTotal")
    rate, people = number("serviceRate"), number("people")
    derived = [budget, subtotal, total]
    if subtotal is not None and rate is not None:
        derived.append(subtotal * rate)  # 服務費
    if budget is not None:
        derived += [budget - v for v in (subtotal, total) if v is not None]  # 剩餘預算
    if people:
        derived += [v / people for v in (budget, subtotal, total) if v is not None]  # 每人
    allowed.update(round(v, 2) for v in derived if v is not None)
    numbers = []
    for match in _MONEY.finditer(to_arabic(text)):
        raw = (match.group(1) or match.group(2)).replace(",", "")
        try:
            value = round(float(raw), 2)
        except ValueError:
            continue
        if value >= MIN_MONEY:
            numbers.append(value)
    # 未推薦的菜名若只是推薦品名（或分類）的一部分就不算，避免推薦「鴨肉飯」時
    # 「鴨肉」被誤判；但「松阪豬肉飯」不是推薦品「松阪豬肉(盤)」的一部分，照算。
    covering = [n for n in names if n] + [_core_name(n) for n in names if n] + [
        str(i.get("category") or "") for i in items if i.get("category")
    ]
    invented = [
        name for name in menu_names
        if len(name) >= MIN_INVENTED_NAME and name not in names and name in text
        and not any(name in other for other in covering)
    ]
    unsupported = [n for n in numbers if not any(abs(n - a) <= TOLERANCE for a in allowed)]
    return {
        "itemCount": len(names),
        "itemsMentioned": len(mentioned),
        "missingItems": missing,
        "moneyMentions": len(numbers),
        "unsupportedNumbers": unsupported,
        "inventedDishes": invented,
        "chars": len(text),
    }


def check_reply(
    text: str, rec: dict[str, Any], menu: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """推薦說明能不能直接送出。ok=False 時 reasons 列出不通過的項目。

    - missing_items：有推薦品項沒提到（省略）
    - unsupported_numbers：金額對不上推薦資料
    - unrecommended_dishes：提到菜單上有、但這次沒推薦的菜（換菜）

    三項任一不通過就改用模板。誤擋只是改回模板（模板 10/10 忠實），漏擋卻會讓
    使用者看到與推薦卡片矛盾的說明，所以寧可嚴格：「也可以加點凍檸茶」這種
    正確的補充也會被擋下。
    """
    result = fact_check(text, rec, menu_item_names(menu))
    reasons = []
    if result["missingItems"]:
        reasons.append("missing_items")
    if result["unsupportedNumbers"]:
        reasons.append("unsupported_numbers")
    if result["inventedDishes"]:
        reasons.append("unrecommended_dishes")
    return {**result, "ok": not reasons, "reasons": reasons}
