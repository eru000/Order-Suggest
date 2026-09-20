"""菜單字彙的唯一來源。

為什麼要有這個檔案：同一組關鍵字原本散在三個地方各寫一份，內容還會各自長歪。
實例——「這是肉嗎」的字表有兩份：

    menu_semantics.MEAT_TERMS  有 培根 火腿 貢丸 海鮮 蛤 蚵，沒有 叉燒 松阪
    table_menu.MEAT_TERMS      有 叉燒 松阪，沒有 培根 火腿 貢丸

於是「松阪豬」在分桶時算肉、在素食判斷時不算肉。同一類 bug 我們已經修過兩次：
時價與分類規則各修了一邊，另一邊照樣錯。

規則本身仍然留在各自的模組（怎麼用字是各模組的事），這裡只放字彙。
"""

from __future__ import annotations

import re

# ── 食材 ────────────────────────────────────────────────
MEAT_TERMS: tuple[str, ...] = (
    "雞", "豬", "牛", "鴨", "鵝", "羊", "肉", "培根", "火腿", "香腸", "貢丸",
    "排骨", "叉燒", "松阪", "腸",
)
SEAFOOD_TERMS: tuple[str, ...] = (
    "魚", "蝦", "蟹", "海鮮", "花枝", "小卷", "中卷", "魷魚", "蛤", "蚵",
    "干貝", "鮑魚", "軟絲", "刺身", "壽司", "生魚",
)
# 寫了這些字就是青菜，不管旁邊有沒有肉（鵝油高麗菜是青菜）。
VEGETABLE_TERMS: tuple[str, ...] = (
    "時蔬", "青菜", "野菜", "蔬菜", "沙拉", "高麗菜", "花椰", "地瓜葉",
    "水蓮", "空心菜", "豆苗", "玉米筍",
)
# 葷素都有：清炒蘆筍是青菜，蘆筍牛肉、控肉桂竹筍是肉。
AMBIGUOUS_VEGETABLE_TERMS: tuple[str, ...] = ("蘆筍", "竹筍", "桂竹", "菇", "筍")
ANIMAL_PRODUCT_TERMS: tuple[str, ...] = ("蛋", "奶", "起司", "乳酪", "優格", "蜂蜜", "美乃滋")
VEGETARIAN_MARKERS: tuple[str, ...] = ("素", "蔬食")
VEGETARIAN_INGREDIENTS: tuple[str, ...] = (
    "青菜", "蔬菜", "豆腐", "豆干", "豆皮", "菇", "海帶", "紫菜", "地瓜", "玉米", "筍",
)

# ── 品項角色 ────────────────────────────────────────────
# 烈酒的品名多半只有品牌年份（蘇格登15年），靠品名抓不到；比對時會連分類名
# 一起看，所以認得「烈酒區」就擋得住整櫃酒。不能只寫「酒」：酒蒸海鮮石鍋燒、
# 全酒麻油雞鍋、紹興醉雞都是菜。sake 也不能列——日文的鮭魚也是 sake。
ALCOHOL_TERMS: tuple[str, ...] = (
    "啤酒", "紅酒", "白酒", "烈酒", "威士忌", "白蘭地", "伏特加", "琴酒",
    "清酒", "梅酒", "高粱", "調酒", "雞尾酒", "沙瓦", "香檳",
    "beer", "wine", "whisky", "whiskey", "vodka", "cocktail", "champagne",
)
SOFT_DRINK_TERMS: tuple[str, ...] = (
    "茶", "飲料", "飲品", "果汁", "咖啡", "奶茶", "可樂", "汽水", "豆漿",
    "拿鐵", "摩卡", "雪碧", "芬達", "氣泡",
)
DRINK_TERMS: tuple[str, ...] = (*SOFT_DRINK_TERMS, *ALCOHOL_TERMS)
SIDE_TERMS: tuple[str, ...] = ("薯條", "雞塊", "魚圈", "蝦塊", "沙拉", "蔬菜棒", "小菜", "加料")
DESSERT_TERMS: tuple[str, ...] = (
    "冰淇淋", "蛋糕", "甜點", "派", "可頌", "甜甜圈", "蛋撻", "大福", "布丁",
)
MAIN_TERMS: tuple[str, ...] = (
    "堡", "burger", "吐司", "貝果", "三明治", "套餐", "義大利麵", "燉飯",
    "麵", "飯",
    # 只寫「排餐」的話，牛排館整份菜單一道主餐都認不出來。
    "牛排", "豬排", "雞排", "魚排", "牛小排", "排餐", "主餐", "獨享餐",
)


def term_hit(term: str, value: str) -> bool:
    """英文關鍵字要整個字比對：「酒蒸海鮮石鍋燒 (Sake-Steamed…)」是鍋物，
    不是清酒，但 "sake" 出現在英文譯名裡，用子字串比對就會判成飲料。"""
    if term.isascii():
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", value) is not None
    return term in value


def any_term(terms: tuple[str, ...], value: str) -> bool:
    lowered = value.casefold()
    return any(term_hit(term.casefold(), lowered) for term in terms)


def pattern_of(*term_groups: tuple[str, ...]) -> str:
    """給需要用 re 的模組：把字表接成一個 pattern，字表本身仍只有一份。"""
    return "|".join(re.escape(term) for group in term_groups for term in group)
