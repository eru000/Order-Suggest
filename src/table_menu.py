"""合菜／熱炒／燒肉店的「一桌菜」組合。

這種店不是一個人點一道主餐，是一桌人點一桌菜。原本的推薦把它當成單點，
結果是「六個人 預算5000」推出三個火鍋加兩份壽司、沒有青菜也沒有主食，
而且只用掉 $630。

這裡只做兩件事：把菜分成幾個桶，再照台灣合菜的點法湊一桌——道數大約是
人數 + 1，其中至少一道青菜、一道湯或鍋，剩下的肉類與海鮮輪流。
"""

from __future__ import annotations

import re
from typing import Any

from menu_vocabulary import (
    AMBIGUOUS_VEGETABLE_TERMS,
    DESSERT_TERMS,
    MEAT_TERMS,
    SEAFOOD_TERMS,
    VEGETABLE_TERMS,
    any_term,
    pattern_of,
)

# 桶的順序就是判斷順序：先判鍋湯，否則「酸白菜肉鍋」會被青菜的「白菜」搶走；
# 主食要排在肉類前面，否則「鴨肉飯」會被判成肉類。
BUCKET_RULES: tuple[tuple[str, str], ...] = (
    # 「鍋」要在字尾才算鍋物：石鍋飯、鍋燒意麵是主食。
    ("湯鍋", r"鍋$|煲$|火鍋|湯$|羹$|鍋物"),
    ("主食", r"飯|麵|粥|米粉|冬粉|粄條|炒飯|炒麵"),
    ("青菜", r""),  # 見 _is_vegetable：葷素同名的字要另外判斷
    ("海鮮", pattern_of(SEAFOOD_TERMS)),
    ("肉類", pattern_of(MEAT_TERMS) + r"|燒臘"),
    ("點心", pattern_of(DESSERT_TERMS) + r"|水果|果盤|西瓜|鳳梨"),
)


def _is_vegetable(value: str) -> bool:
    if any_term(VEGETABLE_TERMS, value):
        return True
    return any_term(AMBIGUOUS_VEGETABLE_TERMS, value) and not any_term(
        (*MEAT_TERMS, *SEAFOOD_TERMS), value
    )

# 一桌至少要有的東西。青菜排第一是因為最常被漏掉。
REQUIRED_BUCKETS = ("青菜", "湯鍋", "主食")
# 湊滿道數時輪流取的桶。
FILL_BUCKETS = ("肉類", "海鮮", "湯鍋", "青菜", "主食", "點心", "其他")


def bucket_of(name: str, category: str = "") -> str:
    # 品名與分類分開比對：「鍋$」這種字尾規則接上分類字串就永遠對不到，
    # 「東北酸白菜肉鍋」會因為後面多一個空白掉進肉類。
    values = [value for value in (name.strip(), category.strip()) if value]
    for bucket, pattern in BUCKET_RULES:
        if bucket == "青菜":
            if any(_is_vegetable(value) for value in values):
                return bucket
            continue
        if any(re.search(pattern, value) for value in values):
            return bucket
    return "其他"


def is_shared_table(item_count: int, main_count: int) -> bool:
    """菜單長得像合菜店嗎？只看菜單本身，不另外設定。

    實測四份真實菜單：大肥鵝 135 項主餐 1%、森森燒肉 81 項主餐 11% 會進合菜
    模式；斗六當歸鴨 28 項主餐 32%、奔頂牛排 8 項主餐 88% 維持單點。
    """
    if item_count < 30:
        return False
    return main_count / item_count < 0.15


def table_size(people: int) -> int:
    """台灣合菜的慣例：道數大約是人數加一。"""
    return max(3, min(10, int(people) + 1))


def compose_table(
    rows: list[dict[str, Any]],
    people: int,
    budget: float | None = None,
) -> list[dict[str, Any]]:
    """湊一桌。rows 必須是已經套過忌口、過敏等條件的候選。

    每個品項會加上 "bucket"。價格未知（時價）的品項在有預算時不列入，
    因為算不進總額，等於讓使用者看不出這桌要多少錢。
    """
    pool = [dict(row, bucket=bucket_of(str(row["name"]), str(row.get("category") or "")))
            for row in rows]
    if budget is not None:
        pool = [row for row in pool if row.get("price") is not None]
    if not pool:
        return []

    count = min(table_size(people), len(pool))
    per_dish = (budget / count) if budget else None
    picked: list[dict[str, Any]] = []
    spent = 0.0

    def affordable(row: dict[str, Any]) -> bool:
        if budget is None:
            return True
        price = row.get("price")
        return price is not None and spent + price <= budget + 1e-9

    def take_from(bucket: str) -> bool:
        nonlocal spent
        # 同一類最多兩道，免得六個人拿到一桌三個鍋。
        if sum(1 for row in picked if row["bucket"] == bucket) >= 2:
            return False
        names = {row["name"] for row in picked}
        families = {row.get("family") for row in picked if row.get("family")}
        options = [
            row for row in pool
            if row["bucket"] == bucket
            and row["name"] not in names
            and row.get("family") not in families
            and affordable(row)
        ]
        if not options:
            return False
        if per_dish is not None:
            # 挑最接近「每道平均預算」的，才不會湊出一桌小菜或一桌大菜。
            options.sort(key=lambda row: (abs((row["price"] or 0) - per_dish), row["name"]))
        else:
            options.sort(key=lambda row: (row.get("price") is None, row.get("price") or 0))
        picked.append(options[0])
        spent += options[0].get("price") or 0.0
        return True

    for bucket in REQUIRED_BUCKETS:
        if len(picked) < count:
            take_from(bucket)

    index = 0
    while len(picked) < count:
        progressed = False
        for bucket in FILL_BUCKETS[index:] + FILL_BUCKETS[:index]:
            if len(picked) >= count:
                break
            if take_from(bucket):
                progressed = True
                index = (FILL_BUCKETS.index(bucket) + 1) % len(FILL_BUCKETS)
                break
        if not progressed:
            break
    return picked
