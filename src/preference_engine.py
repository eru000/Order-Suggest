from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class SpicePreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: int | None = Field(default=None, ge=0, le=5)
    maximum: int | None = Field(default=None, ge=0, le=5)
    target: int | None = Field(default=None, ge=0, le=5)
    strict: bool = False
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_text: str = ""

    @field_validator("maximum")
    @classmethod
    def validate_range(cls, value: int | None, info: Any) -> int | None:
        minimum = info.data.get("minimum")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("maximum must be greater than or equal to minimum")
        return value


class PreferenceOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    field: str
    value: Any = None

    @field_validator("action")
    @classmethod
    def action_is_supported(cls, value: str) -> str:
        if value not in {"set", "add", "remove", "clear"}:
            raise ValueError("unsupported preference operation")
        return value


class ParsedPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spice: SpicePreference | None = None
    allergens: list[str] = Field(default_factory=list)
    dietary_restrictions: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    likes: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    dish_types: list[str] = Field(default_factory=list)
    budget_amount: float | None = Field(default=None, ge=0)
    budget_basis: str = "total"
    people: int | None = Field(default=None, ge=1, le=50)
    need_drink: bool | None = None
    operations: list[PreferenceOperation] = Field(default_factory=list)
    unparsed_text: str = ""


SPICE_RULES: tuple[tuple[re.Pattern[str], dict[str, Any]], ...] = (
    (
        re.compile(r"不吃大辣|不要大辣|不要太辣|不會太辣|不能太辣|別太辣"),
        {"maximum": 2, "target": 1, "strict": False},
    ),
    (
        re.compile(
            r"不吃辣|不能吃辣|不敢吃辣|不會吃辣|不想吃辣|不愛吃辣|不喜歡辣|受不了辣|怕辣"
            r"|不要辣|不加辣|免辣|去辣|無辣|不辣"
        ),
        {"maximum": 0, "target": 0, "strict": True},
    ),
    (re.compile(r"越辣越好|爆辣|特辣|超辣"), {"minimum": 4, "target": 5, "strict": False}),
    (re.compile(r"大辣|很辣|重辣"), {"minimum": 3, "target": 4, "strict": False}),
    # 這條放在否定規則之後，「不愛吃辣」才不會被「愛吃辣」搶去判成中辣。
    (re.compile(r"中辣|要辣|愛吃辣|想吃辣|嗜辣|辣一點|重口味"), {"target": 3, "strict": False}),
    (
        re.compile(r"微辣|小辣|一點點辣|一點辣|可以吃一點辣|辣度普通"),
        {"maximum": 2, "target": 1, "strict": False},
    ),
)

CUISINES = ("中式", "日式", "泰式", "美式", "韓式", "義式", "越式", "印度", "墨西哥")
DIETARY_TERMS = {
    "vegan": ("純素", "全素"),
    "vegetarian": ("素食", "蛋奶素", "奶蛋素"),
    "halal": ("清真",),
    "gluten_free": ("無麩質", "不含麩質"),
}
DISH_ALIASES = {
    "漢堡": ("漢堡", "burger", "芝加哥堡"),
    "吐司": ("吐司", "toast"),
    "貝果": ("貝果", "bagel"),
    "套餐": ("套餐", "combo"),
}
ALLERGEN_ALIASES = {
    "peanut": ("花生",),
    "tree_nut": ("堅果", "腰果", "杏仁", "核桃"),
    "milk": ("牛奶", "乳製品", "奶類"),
    "egg": ("蛋", "雞蛋"),
    "shellfish": ("甲殼類", "蝦", "蟹"),
    "fish": ("魚類", "魚"),
    "soy": ("黃豆", "大豆"),
    "gluten": ("麩質",),
}

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(value: str) -> int | None:
    total = current = 0
    last_unit = 1
    for char in value:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            total += (current or 1) * unit
            current = 0
            last_unit = unit
        else:
            return None
    if current:
        total += current * (last_unit // 10) if last_unit >= 10 and total else current
    return total or None


def _budget(text: str) -> tuple[float | None, str]:
    patterns = (
        re.compile(
            r"(?:預算|不超過|最多|上限)?\s*(\d{2,6})\s*(?:元|塊|圓|NT\$?|NTD)?\s*(?:以內|以下|左右|上下)?",
            re.I,
        ),
        re.compile(
            r"(?:預算|不超過|最多|上限)?\s*([一二兩三四五六七八九十百千]+)\s*(?:元|塊|圓)?\s*(?:以內|以下|左右|上下)?"
        ),
    )
    if not re.search(r"預算|不超過|最多|上限|元|塊|圓|NT|以內|以下", text, re.I):
        return None, "total"
    match = patterns[0].search(text)
    amount = float(match.group(1)) if match else None
    if amount is None:
        match = patterns[1].search(text)
        parsed = _cn_to_int(match.group(1)) if match else None
        amount = float(parsed) if parsed else None
    return amount, "per_person" if re.search(r"每人|一人|人均", text) else "total"


def _split_terms(value: str) -> list[str]:
    stopped = re.split(r"[。；;！？!?，,\n\t　]|\s+(?=要|想|改|預算|\d+\s*人)", value, maxsplit=1)[
        0
    ]
    return [
        part.strip()
        for part in re.split(r"[、\s]+|跟|和|與|及|還有|以及|加上", stopped)
        if part.strip()
    ]


# 「不要」含「要」、「不吃」含「吃」，所以正面觸發詞一律用多字詞，且把否定
# 形式排在同一條 alternation 之外——單字「要」會把「我不要辣」讀成喜歡辣。
# 前面那個字是否定詞就不算數：「我不想吃麵」的「想吃」、「我不喜歡蓮子」的
# 「喜歡」都會命中，少了這個 lookbehind 會把討厭讀成喜歡。
LIKE_CUES = re.compile(r"(?<![不別沒])(?:想吃|愛吃|要吃|想喝|想點|我要|喜歡|來個|來份|來碗|來一份)")

# 只削掉動詞開頭與語尾助詞。不碰數量詞是刻意的：「三杯雞」削掉「三杯」會變成
# 「雞」，比漏抓還糟——漏抓只是少加分，錯抓會推薦到完全不相干的品項。
_DISLIKE_LEADING = re.compile(r"^(?:不要|不吃|不敢吃|不想吃|不愛吃|不喜歡|忌口|別吃|別)+")
_LIKE_LEADING = re.compile(r"^[吃喝點來]+")
_LIKE_TRAILING = re.compile(r"[的嗎呢啊喔吧了]+$")


def _clean_like(term: str) -> str:
    return _LIKE_TRAILING.sub("", _LIKE_LEADING.sub("", term.strip())).strip()


def _canonical_allergen(term: str) -> str | None:
    for canonical, aliases in ALLERGEN_ALIASES.items():
        if any(alias in term for alias in aliases):
            return canonical
    return None


def parse_preferences(
    text: str,
    *,
    llm_extractor: Callable[[str], str | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = str(text or "").strip()
    parsed = ParsedPreferences()

    for pattern, values in SPICE_RULES:
        match = pattern.search(source)
        if match:
            parsed.spice = SpicePreference(**values, confidence=1.0, source_text=match.group(0))
            parsed.operations.append(PreferenceOperation(action="set", field="spice", value=values))
            break

    budget, basis = _budget(source)
    if budget is not None:
        parsed.budget_amount = budget
        parsed.budget_basis = basis
        parsed.operations.append(
            PreferenceOperation(
                action="set", field="budget", value={"amount": budget, "basis": basis}
            )
        )

    people_match = re.search(r"(\d{1,2})\s*(?:個|位)?\s*人", source)
    if people_match:
        parsed.people = max(1, min(50, int(people_match.group(1))))
    else:
        people_match = re.search(r"([一二兩三四五六七八九十]+)\s*(?:個|位)?\s*人", source)
        if people_match:
            parsed.people = _cn_to_int(people_match.group(1))
    if parsed.people:
        parsed.operations.append(
            PreferenceOperation(action="set", field="people", value=parsed.people)
        )

    if re.search(r"(?:不要|不含|無|不需要|別加)\s*飲料", source):
        parsed.need_drink = False
    elif re.search(r"要飲料|加飲料|來杯|想喝|要喝", source):
        parsed.need_drink = True
    if parsed.need_drink is not None:
        parsed.operations.append(
            PreferenceOperation(action="set", field="needDrink", value=parsed.need_drink)
        )

    for cuisine in CUISINES:
        if cuisine in source:
            parsed.cuisines.append(cuisine)
            parsed.operations.append(
                PreferenceOperation(action="set", field="cuisines", value=[cuisine])
            )
            break
    for canonical, aliases in DISH_ALIASES.items():
        if any(alias.casefold() in source.casefold() for alias in aliases):
            parsed.dish_types.append(canonical)
            parsed.operations.append(
                PreferenceOperation(action="set", field="dishTypes", value=[canonical])
            )
            break
    for canonical, aliases in DIETARY_TERMS.items():
        if any(alias in source for alias in aliases):
            parsed.dietary_restrictions.append(canonical)
            parsed.operations.append(
                PreferenceOperation(action="add", field="dietaryRestrictions", value=canonical)
            )

    allergy_patterns = (
        re.compile(r"對([^，。；;！!？?]+?)過敏"),
        re.compile(r"([^，。；;！!？?\s]+)過敏"),
    )
    for pattern in allergy_patterns:
        for match in pattern.finditer(source):
            for term in _split_terms(match.group(1)):
                canonical = _canonical_allergen(term) or term
                if canonical not in parsed.allergens:
                    parsed.allergens.append(canonical)
                    parsed.operations.append(
                        PreferenceOperation(action="add", field="allergens", value=canonical)
                    )

    spice_noise = {"辣", "太辣", "很辣", "超辣", "大辣", "中辣", "小辣", "微辣", "重辣", "飲料"}
    for cue in ("不要", "不吃", "不敢吃", "不想吃", "不愛吃", "不喜歡", "忌口"):
        for match in re.finditer(cue, source):
            segment = source[match.end() :]
            for raw in _split_terms(segment):
                # 「我不吃麵、不吃粥」切出來的第二段是「不吃粥」——觸發詞跟著
                # 被切進來了。留著的話 AI 會把「不吃粥」當成一道菜名唸出來。
                term = _DISLIKE_LEADING.sub("", raw).strip()
                if not term or term in spice_noise or "過敏" in term:
                    continue
                if term in parsed.dislikes:
                    continue
                parsed.dislikes.append(term)
                parsed.operations.append(
                    PreferenceOperation(action="add", field="dislikes", value=term)
                )

    # recommendation.py 一直有讀 prefs["likes"] 並加分，但兩邊都沒有人產生它，
    # 所以「我要當歸的」跟「有什麼推薦？」以前會給出一模一樣的結果。
    for match in LIKE_CUES.finditer(source):
        for raw in _split_terms(source[match.end() :]):
            term = _clean_like(raw)
            if not term or term in spice_noise or "過敏" in term:
                continue
            if term in parsed.dislikes or term in parsed.likes:
                continue
            parsed.likes.append(term)
            parsed.operations.append(
                PreferenceOperation(action="add", field="likes", value=term)
            )

    # 同一句裡又喜歡又不吃，以不吃為準——把牴觸的正面偏好收回去。
    for term in parsed.dislikes:
        if term in parsed.likes:
            parsed.likes.remove(term)
        parsed.operations.append(
            PreferenceOperation(action="remove", field="likes", value=term)
        )

    removal_patterns = (
        re.compile(r"(?:可以吃|可以接受)([^，。；;！!？?\s]+)了?"),
        re.compile(r"([^，。；;！!？?\s]+)(?:可以吃|可以了)"),
        re.compile(r"(?:取消|不用)(?:避開|排除|忌口)?([^，。；;！!？?\s]+)"),
    )
    for pattern in removal_patterns:
        for match in pattern.finditer(source):
            term = match.group(1).strip()
            if term:
                parsed.operations.append(
                    PreferenceOperation(action="remove", field="dislikes", value=term)
                )
                allergen = _canonical_allergen(term)
                if allergen:
                    parsed.operations.append(
                        PreferenceOperation(action="remove", field="allergens", value=allergen)
                    )

    if re.search(r"清除偏好|全部重設|全部清除|重新開始", source):
        parsed.operations = [PreferenceOperation(action="clear", field="all")]

    # LLM is a bounded ambiguity fallback, never a per-item classifier.
    if llm_extractor and not parsed.operations and len(source) >= 4:
        try:
            raw = llm_extractor(source)
            candidate = json.loads(raw) if isinstance(raw, str) else raw
            llm_value = ParsedPreferences.model_validate(candidate)
            parsed = llm_value
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            parsed.unparsed_text = source

    return _to_legacy_dict(parsed)


def _to_legacy_dict(parsed: ParsedPreferences) -> dict[str, Any]:
    value: dict[str, Any] = {
        "_operations": [operation.model_dump() for operation in parsed.operations],
    }
    if parsed.spice:
        value["spiceProfile"] = parsed.spice.model_dump()
        target = parsed.spice.target
        if parsed.spice.strict and parsed.spice.maximum == 0:
            value["spiceLevel"] = "不辣"
        elif target is not None:
            value["spiceLevel"] = ("不辣", "小辣", "小辣", "中辣", "大辣", "大辣")[target]
    if parsed.dislikes:
        value["excludes"] = list(dict.fromkeys(parsed.dislikes))
    if parsed.likes:
        value["likes"] = list(dict.fromkeys(parsed.likes))
    if parsed.allergens:
        value["allergens"] = list(dict.fromkeys(parsed.allergens))
    if parsed.dietary_restrictions:
        value["dietaryRestrictions"] = list(dict.fromkeys(parsed.dietary_restrictions))
    if parsed.budget_amount is not None:
        value["budget"] = parsed.budget_amount
        value["budgetBasis"] = parsed.budget_basis
    if parsed.people is not None:
        value["people"] = parsed.people
    if parsed.need_drink is not None:
        value["needDrink"] = parsed.need_drink
    if parsed.cuisines:
        value["cuisine"] = parsed.cuisines[0]
    if parsed.dish_types:
        value["preferredDish"] = parsed.dish_types[0]
    value["weights"] = {
        "price": 0.8 if parsed.budget_amount is not None else 0.3,
        "main": 0.5,
        "variety": 0.4,
        "drink": 0.6 if parsed.need_drink else -0.8,
        "spice": 0.7 if parsed.spice else 0.2,
        "category": 0.5,
        "cuisine": 0.6 if parsed.cuisines else 0.0,
    }
    return value


def merge_preference_delta(base: dict[str, Any], delta: dict[str, Any]) -> None:
    operations = delta.get("_operations")
    if not isinstance(operations, list):
        operations = []
    if any(
        op.get("action") == "clear" and op.get("field") == "all"
        for op in operations
        if isinstance(op, dict)
    ):
        base.clear()
        return

    list_fields = {
        "dislikes": "excludes",
        "likes": "likes",
        "allergens": "allergens",
        "dietaryRestrictions": "dietaryRestrictions",
    }
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        action = operation.get("action")
        field = str(operation.get("field") or "")
        target = list_fields.get(field, field)
        incoming = operation.get("value")
        if action == "remove" and target in list_fields.values():
            base[target] = [value for value in base.get(target, []) if value != incoming]
        elif action == "clear":
            base.pop(target, None)

    for key, value in delta.items():
        if key == "_operations":
            continue
        if key in {"excludes", "likes", "allergens", "dietaryRestrictions"}:
            removed = {
                op.get("value")
                for op in operations
                if isinstance(op, dict)
                and op.get("action") == "remove"
                and list_fields.get(str(op.get("field") or ""), op.get("field")) == key
            }
            merged = [*base.get(key, []), *value]
            base[key] = list(dict.fromkeys(item for item in merged if item not in removed))
        elif value is not None:
            base[key] = value
