from __future__ import annotations

import copy
import re
from typing import Any

# 3：烈酒改判為 drink。annotate_item 會沿用已存的標註，所以改了規則就得升版，
# 否則資料庫裡既有菜單（每道菜都存著當初算好的 semantic）永遠套不到新規則。
SEMANTIC_SCHEMA_VERSION = 3

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
        # 烈酒的品名多半只有品牌年份（蘇格登15年），靠品名抓不到；_role 比對的是
        # 「分類 品名」，所以認分類名（烈酒區、調酒區）才擋得住整櫃酒。
        # 不能只寫「酒」：酒蒸海鮮石鍋燒、全酒麻油雞鍋、紹興醉雞都是菜。
        "烈酒",
        "威士忌",
        "白蘭地",
        "伏特加",
        "琴酒",
        "清酒",
        "梅酒",
        "高粱",
        "調酒",
        "雞尾酒",
        "沙瓦",
        "香檳",
        "beer",
        "wine",
        "whisky",
        "whiskey",
        "vodka",
        "cocktail",  # sake 不能列：日文的鮭魚也是 sake，酒蒸料理的英文譯名也有
        "champagne",
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
        # 只寫「排餐」的話，牛排館整份菜單一道主餐都認不出來。
        "牛排",
        "豬排",
        "雞排",
        "魚排",
        "牛小排",
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


VEGETARIAN_MARKERS = ("素", "蔬食")
VEGETARIAN_INGREDIENTS = (
    "青菜",
    "蔬菜",
    "豆腐",
    "豆干",
    "豆皮",
    "菇",
    "海帶",
    "紫菜",
    "地瓜",
    "玉米",
    "筍",
)
MEAT_TERMS = (
    "雞", "豬", "牛", "鴨", "鵝", "羊", "肉", "培根", "火腿", "香腸", "貢丸", "排骨",
    "魚", "蝦", "蟹", "海鮮", "花枝", "小卷", "魷魚", "蛤", "蚵", "干貝",
)
ANIMAL_PRODUCT_TERMS = ("蛋", "奶", "起司", "乳酪", "優格", "蜂蜜", "美乃滋")


def _dietary(name: str, explicit: Any) -> tuple[list[str], list[str]]:
    """從品名推斷素食相容性，回傳 (相容, 不相容)。

    兩個都空代表「從品名判斷不出來」。這個三態很重要：舊版只有
    ``dietaryFlags`` 一個清單，而 deterministic 路徑永遠回空，推薦器的
    ``dietary.issubset(flags)`` 就必定失敗——使用者說一句「我吃素」整份菜單
    會被清空。分出 conflicts 之後，推薦器才能只排除確定衝突的品項。
    """
    if isinstance(explicit, list) and explicit:
        return [str(value) for value in explicit], []
    has_marker = any(term in name for term in VEGETARIAN_MARKERS)
    has_meat = any(term in name for term in MEAT_TERMS)
    has_animal = any(term in name for term in ANIMAL_PRODUCT_TERMS)
    flags: list[str] = []
    conflicts: list[str] = []
    # 「素肉燥飯」的素字優先於肉字——店家自己標素就是素。
    if has_marker or (not has_meat and any(term in name for term in VEGETARIAN_INGREDIENTS)):
        flags.append("vegetarian")
        (conflicts if has_animal else flags).append("vegan")
    elif has_meat:
        conflicts.extend(("vegetarian", "vegan"))
    elif has_animal:
        conflicts.append("vegan")
    return flags, conflicts


ALCOHOL_TERMS = (
    "啤酒", "紅酒", "白酒", "烈酒", "威士忌", "白蘭地", "伏特加", "琴酒",
    "清酒", "梅酒", "高粱", "調酒", "雞尾酒", "沙瓦", "香檳",
    "beer", "wine", "whisky", "whiskey", "vodka", "cocktail", "champagne",
)


def is_alcohol(name: str, category: str = "") -> bool:
    """只看菜單寫了什麼，不做推斷。沒要酒卻配酒是使用者一眼看得出來的錯。"""
    value = f"{category} {name}".casefold()
    return any(_term_hit(term.casefold(), value) for term in ALCOHOL_TERMS)


def _term_hit(term: str, value: str) -> bool:
    # 英文關鍵字要整個字比對：「酒蒸海鮮石鍋燒 (Sake-Steamed…)」是鍋物，
    # 不是清酒，但 "sake" 出現在英文譯名裡，用子字串比對就會判成飲料。
    if term.isascii():
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", value) is not None
    return term in value


def _role(name: str, category: str) -> tuple[str, float]:
    value = f"{category} {name}".casefold()
    for role, terms in ROLE_TERMS.items():
        if any(_term_hit(term.casefold(), value) for term in terms):
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
    dietary_flags, dietary_conflicts = _dietary(name, value.get("dietaryFlags"))
    value["semantic"] = {
        "schemaVersion": SEMANTIC_SCHEMA_VERSION,
        "role": str(value.get("role") or role),
        "spice": _spice(name, tags),
        "allergens": _allergens(name, value.get("allergens")),
        "dietaryFlags": dietary_flags,
        "dietaryConflicts": dietary_conflicts,
        "ingredients": list(value.get("ingredients") or []),
        "tasteTags": list(value.get("tasteTags") or []),
        "confidence": confidence,
        "source": "explicit" if value.get("role") or value.get("allergens") else "deterministic_v1",
    }
    if isinstance(existing, dict):
        # 升版是為了套用新的規則，不是為了把已經確認過的事實扔掉。規則推不出
        # 「這道確定不含花生」，重算會讓確認過的成分退回「不明」。
        old_allergens = existing.get("allergens")
        if isinstance(old_allergens, dict) and old_allergens.get("known"):
            value["semantic"]["allergens"] = copy.deepcopy(old_allergens)
        if existing.get("source") == "explicit" and existing.get("role"):
            value["semantic"]["role"] = existing["role"]
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
