"""Meal-local wishes, explicit menu evidence, and conservative text intent routing."""

from __future__ import annotations

import re
from typing import Any

PERSISTENT_FIELDS = ("allergens", "dietaryRestrictions", "excludes")
SOFT_LABELS = {
    "taste": {"light": "想吃清爽、少油的", "rich": "想吃重口味的"},
    "portion": {"large": "想吃大份一點", "small": "想吃小份一點"},
}
SOFT_PATTERNS = (
    (r"太清淡|太清爽|沒味道", "taste", "rich"),
    (r"太濃郁|太重口味", "taste", "light"),
    (r"份量太多|太大份|吃不完", "portion", "small"),
    (r"份量太少|份量太小|太小份|吃不飽", "portion", "large"),
    (r"(?:不想|不要|別)(?:吃)?(?:太)?(?:清淡|清爽)", "taste", "rich"),
    (r"(?:不想|不要|別)(?:吃)?(?:大份|大碗)", "portion", "small"),
    (r"(?:不想|不要|別)(?:吃)?(?:小份|小碗)", "portion", "large"),
    (r"(?:不想|不要|別)(?:吃)?(?:太)?(?:油膩|油|重口味)|清爽(?:一點)?|清淡(?:一點)?|少油",
     "taste", "light"),
    (r"重口味(?:一點)?|濃郁(?:一點)?", "taste", "rich"),
    (r"不太餓|不餓|吃不多|小份(?:一點)?|少量|份量少(?:一點)?", "portion", "small"),
    (r"很餓|好餓|吃飽(?:一點)?|大份(?:一點)?|份量多(?:一點)?", "portion", "large"),
)
_WISH_PATTERN = re.compile("|".join(
    f"(?P<wish_{index}>{pattern})" for index, (pattern, _, _) in enumerate(SOFT_PATTERNS)
))


def meal_wishes(text: str) -> tuple[dict[str, str], str]:
    """Remove matched phrases so they cannot become literal likes/dislikes."""
    result = {}
    # Read in sentence order: a later correction wins; consume negation with its phrase.
    for match in _WISH_PATTERN.finditer(text):
        index = int(str(match.lastgroup).removeprefix("wish_"))
        _, field, value = SOFT_PATTERNS[index]
        result[field] = value
    return result, _WISH_PATTERN.sub("", text)


def menu_signals(item: dict[str, Any]) -> dict[str, str | None]:
    tags = [str(item.get("name") or "")]
    for values in (item.get("tags"), item.get("tasteTags"),
                   item.get("semantic", {}).get("tasteTags")):
        if isinstance(values, list):
            tags.extend(str(v) for v in values)
    evidence = " ".join(tags)
    # Only explicit menu wording: soup is not assumed light, rice is not assumed large.
    light = bool(re.search(r"清爽|清淡|少油|低油", evidence))
    rich = bool(re.search(r"重口味|濃郁|油炸|酥炸|炸雞|炸豬排|炸排骨", evidence))
    # 括號註記（鴨肉飯(大)）是店家明寫的份量，跟「大份」同級；(大辣) 這種不算。
    large = bool(re.search(r"大份|大碗|加大|份量多|[（(]\s*大\s*[）)]", evidence))
    small = bool(re.search(r"小份|小碗|迷你|少量|[（(]\s*小\s*[）)]", evidence))
    return {
        "taste": ("light" if light else "rich") if light != rich else None,
        "portion": ("large" if large else "small") if large != small else None,
    }


def is_information_question(text: str) -> bool:
    if re.search(
        r"(?:不要|不想)(?:再)?(?:幫我|替我)?(?:決定|選|挑)"
        r"|(?:不要|不想)(?:就)?吃(?:第|這)", text
    ):
        return True
    if re.search(r"為什麼|為何|怎麼|如何|如果|假如|是否有|會不會|是不是", text):
        return True
    if re.search(r"不知道.*吃|不知.*吃|吃什麼好|吃甚麼好", text):
        return False
    # A polite request to modify a meal is still an action, even with a question mark.
    if re.search(
        r"(?:幫我|替我)?(?:換成|改成|改吃|換吃|改為|換一批|換一道|換一個|調整|設定)"
        r"|(?:幫我|替我)(?:決定|選一道|挑一道)", text
    ) and not re.search(r"不要(?:幫我)?(?:換|改)|不想(?:換|改)", text):
        return False
    return bool(re.search(
        r"[?？]|嗎|呢|有沒有|是否|能不能|可不可以|多少|哪|什麼|甚麼", text
    ))


def split_message(text: str) -> tuple[str, str]:
    commands, questions = [], []
    for part in re.split(r"[，,。；;！？!?\n]+", text):
        part = part.strip()
        if not part:
            continue
        (questions if is_information_question(part) else commands).append(part)
    # Punctuation-only question cues must still be respected for a single clause.
    if len(commands) == 1 and not questions and is_information_question(text):
        return "", text
    return "，".join(commands), "，".join(questions)
