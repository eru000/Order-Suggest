from __future__ import annotations

import copy
import re
from typing import Any

SEMANTIC_SCHEMA_VERSION = 1

ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "drink": (
        "茶",
        "飲料",
        "飲品",
        "果汁",
        "咖啡",
        "奶茶",
        "可樂",
        "汽水",
        "豆漿",
        "拿鐵",
        "氣泡",
        "啤酒",
        "紅酒",
        "白酒",
        "beer",
        "wine",
    ),
    "side": ("薯條", "雞塊", "沙拉", "蔬菜棒", "小菜", "加料"),
    "dessert": ("冰淇淋", "蛋糕", "甜點", "派", "可頌", "甜甜圈", "蛋撻", "大福", "布丁"),
    "main": (
        "堡",
        "burger",
        "吐司",
        "貝果",
        "三明治",
        "套餐",
        "義大利麵",
        "燉飯",
        "麵",
        "飯",
        "排餐",
        "主餐",
        "獨享餐",
    ),
}

SPICE_TERMS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (5, ("特辣", "超辣", "爆辣", "魔鬼辣")),
    (4, ("大辣", "麻辣", "重辣")),
    (3, ("中辣", "辣子", "辣雞", "香辣")),
    (2, ("小辣", "微辣", "辣椒", "辣醬")),
)

ALLERGEN_TERMS: dict[str, tuple[str, ...]] = {
    "peanut": ("花生", "花生醬"),
    "tree_nut": ("堅果", "腰果", "杏仁", "核桃"),
    "milk": ("牛奶", "奶油", "起司", "乳酪", "鮮奶"),
    "egg": ("雞蛋", "蛋黃", "蛋白", "荷包蛋"),
    "shellfish": ("蝦", "蟹", "龍蝦"),
    "fish": ("魚", "鮭魚", "鯖魚", "鱈魚"),
    "soy": ("黃豆", "豆漿", "豆腐"),
    "gluten": ("麵包", "吐司", "麵", "可頌", "蛋糕"),
}


def _role(name: str, category: str) -> tuple[str, float]:
    value = f"{category} {name}".casefold()
    for role, terms in ROLE_TERMS.items():
        if any(term.casefold() in value for term in terms):
            return role, 0.8
    return "other", 0.25


def _spice(name: str, tags: list[str]) -> dict[str, Any]:
    value = f"{name} {' '.join(tags)}"
    explicit = re.search(r"辣度\s*([0-5])", value)
    if explicit:
        level = int(explicit.group(1))
        return {"min": level, "max": level, "adjustable": False, "known": True}
    if "不辣" in value or "無辣" in value:
        return {"min": 0, "max": 0, "adjustable": False, "known": True}
    for level, terms in SPICE_TERMS:
        if any(term in value for term in terms):
            adjustable = any(term in value for term in ("辣度可調", "可調辣", "辣度選擇"))
            return {
                "min": 0 if adjustable else level,
                "max": level,
                "adjustable": adjustable,
                "known": True,
            }
    # 沒有辣味線索不是安全保證，保留 unknown；推薦器會依限制強度處理。
    return {"min": None, "max": None, "adjustable": False, "known": False}


def _allergens(name: str, explicit: Any) -> dict[str, Any]:
    if isinstance(explicit, list):
        return {"known": True, "values": sorted({str(value) for value in explicit})}
    found = {
        allergen
        for allergen, terms in ALLERGEN_TERMS.items()
        if any(term in name for term in terms)
    }
    return {"known": False, "values": sorted(found)}


def annotate_item(item: dict[str, Any], category: str = "") -> dict[str, Any]:
    value = copy.deepcopy(item)
    existing = value.get("semantic")
    if (
        isinstance(existing, dict)
        and int(existing.get("schemaVersion") or 0) >= SEMANTIC_SCHEMA_VERSION
    ):
        return value
    name = str(value.get("name") or "").strip()
    tags = (
        [str(tag) for tag in value.get("tags", [])] if isinstance(value.get("tags"), list) else []
    )
    role, confidence = _role(name, category)
    value["semantic"] = {
        "schemaVersion": SEMANTIC_SCHEMA_VERSION,
        "role": str(value.get("role") or role),
        "spice": _spice(name, tags),
        "allergens": _allergens(name, value.get("allergens")),
        "dietaryFlags": list(value.get("dietaryFlags") or []),
        "ingredients": list(value.get("ingredients") or []),
        "tasteTags": list(value.get("tasteTags") or []),
        "confidence": confidence,
        "source": "explicit" if value.get("role") or value.get("allergens") else "deterministic_v1",
    }
    return value


def annotate_menu(menu: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(menu)
    restaurants = value.get("restaurants")
    if isinstance(restaurants, dict):
        for restaurant in restaurants.values():
            categories = restaurant.get("categories", {}) if isinstance(restaurant, dict) else {}
            if not isinstance(categories, dict):
                continue
            for category_name, category in categories.items():
                if isinstance(category, dict):
                    category["items"] = [
                        annotate_item(item, str(category_name))
                        for item in category.get("items", [])
                        if isinstance(item, dict)
                    ]
        return value
    categories = value.get("categories")
    if isinstance(categories, list):
        for category in categories:
            if isinstance(category, dict):
                category["items"] = [
                    annotate_item(item, str(category.get("name") or ""))
                    for item in category.get("items", [])
                    if isinstance(item, dict)
                ]
    return value
