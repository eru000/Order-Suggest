"""合菜／熱炒／燒肉店的「一桌菜」組合。

這種店不是一個人點一道主餐，是一桌人點一桌菜。原本的推薦把它當成單點，
結果是「六個人 預算5000」推出三個火鍋加兩份壽司、沒有青菜也沒有主食，
而且只用掉 $630。

這裡只做兩件事：把菜分成幾個桶，再照台灣合菜的點法湊一桌——道數大約是
人數 + 1，其中至少一道青菜、一道湯或鍋，剩下的肉類與海鮮輪流。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


def absent_required_buckets(rows: list[dict[str, Any]]) -> list[str]:
    """必備類別裡，候選中完全沒有品項的那幾類。

    「這一桌沒有湯」有兩種原因：菜單上根本沒有湯品（燒肉店就是這樣），或是
    有湯但預算排不進去。使用者該看到的說明完全不同，而原本的程式兩種都只是
    安靜跳過，結果看起來一樣——口試委員就是把前者讀成「規則的例外處理」。

    這裡只回報前者：候選中一項都沒有。實測四份菜單，森森燒肉的湯鍋是 0 項、
    大肥鵝的主食是 0 項，兩者都屬於這一類。
    """
    present = {
        bucket_of(str(row.get("name") or ""), str(row.get("category") or ""))
        for row in rows
    }
    return [bucket for bucket in REQUIRED_BUCKETS if bucket not in present]


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


@dataclass(frozen=True)
class TablePolicy:
    """一桌菜的組成政策（期末規劃 4.3，回應委員5-4「規則缺乏依據」）。

    把散在程式裡的常數集中成可設定的參數，每個預設值都在 sources 註明依據。
    目前全部是作者預設（author_default），待文獻或使用者問卷補上依據後更新
    sources，或依使用者偏好換一組參數。
    """

    required_buckets: tuple[str, ...] = REQUIRED_BUCKETS
    fill_buckets: tuple[str, ...] = FILL_BUCKETS
    extra_dishes: int = 1
    min_dishes: int = 3
    max_dishes: int = 10
    bucket_cap: int = 2
    sources: tuple[tuple[str, str], ...] = (
        ("extra_dishes", "author_default：台灣合菜常見的「人數加一」點法，待文獻或問卷佐證"),
        ("required_buckets", "author_default：青菜、湯鍋、主食，待問卷佐證"),
        ("bucket_cap", "author_default：同一類最多兩道，避免一桌三個鍋"),
        ("min_dishes", "author_default"),
        ("max_dishes", "author_default"),
    )

    def table_size(self, people: int) -> int:
        return max(self.min_dishes, min(self.max_dishes, int(people) + self.extra_dishes))


DEFAULT_TABLE_POLICY = TablePolicy()


def table_size(people: int, policy: TablePolicy | None = None) -> int:
    """台灣合菜的慣例：道數大約是人數加一。"""
    return (policy or DEFAULT_TABLE_POLICY).table_size(people)


def compose_table(
    rows: list[dict[str, Any]],
    people: int,
    budget: float | None = None,
    policy: TablePolicy | None = None,
) -> list[dict[str, Any]]:
    """湊一桌。rows 必須是已經套過忌口、過敏等條件的候選。

    每個品項會加上 "bucket"。價格未知（時價）的品項在有預算時不列入，
    因為算不進總額，等於讓使用者看不出這桌要多少錢。
    """
    policy = policy or DEFAULT_TABLE_POLICY
    pool = [dict(row, bucket=bucket_of(str(row["name"]), str(row.get("category") or "")))
            for row in rows]
    if budget is not None:
        pool = [row for row in pool if row.get("price") is not None]
    if not pool:
        return []

    count = min(policy.table_size(people), len(pool))
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
        if sum(1 for row in picked if row["bucket"] == bucket) >= policy.bucket_cap:
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
            options.sort(key=lambda row: (
                row.get("spiceUncertain", False), abs((row["price"] or 0) - per_dish), row["name"],
            ))
        else:
            options.sort(key=lambda row: (
                row.get("spiceUncertain", False), row.get("price") is None, row.get("price") or 0,
            ))
        picked.append(options[0])
        spent += options[0].get("price") or 0.0
        return True

    for bucket in policy.required_buckets:
        if len(picked) < count:
            take_from(bucket)

    fill = policy.fill_buckets
    index = 0
    while len(picked) < count:
        progressed = False
        for bucket in fill[index:] + fill[:index]:
            if len(picked) >= count:
                break
            if take_from(bucket):
                progressed = True
                index = (fill.index(bucket) + 1) % len(fill)
                break
        if not progressed:
            break
    return picked


# ── 未達原因（期末規劃 4.2，回應委員5-7）───────────────────────────────
# 「這一桌沒有湯」至少有五種原因，給使用者的說明完全不同。以前只分得出第一種，
# 而且是用「篩選後的候選」判斷，所以使用者說「不吃鍋」時會被說成「這家店的
# 菜單上沒有湯鍋」（2026-11-26 實際執行確認）。
UNMET_CAUSES = (
    "absent_in_menu",    # 原菜單就沒有這類
    "excluded_by_user",  # 菜單有，但被使用者的忌口、過敏、辣度等條件排除
    "price_unknown",     # 有預算時，這類只剩時價或沒標價的品項，算不進總額
    "over_budget",       # 這類品項放不進預算
    "search_not_found",  # 預算內排得進，但目前的貪婪組合沒有挑到
)
DISH_COUNT = "道數"


def _bucket(row: dict[str, Any]) -> str:
    if row.get("bucket"):
        return str(row["bucket"])
    return bucket_of(str(row.get("name") or ""), str(row.get("category") or ""))


def min_table_cost(
    rows: list[dict[str, Any]],
    size: int,
    policy: TablePolicy | None = None,
    must_have: str | None = None,
) -> float | None:
    """size 道、守住同類上限（且含 must_have 類）的一桌，最低可能總價；湊不出來回 None。

    同類上限構成分割擬陣（partition matroid），在擬陣上依價格由低到高貪婪挑選
    就是最低總價；強制包含某類時先放該類最便宜的一道再貪婪，同樣是最佳解。
    tests/test_table_menu.py 用小菜單窮舉驗證這個性質（規劃 4.5）。只看有標價的品項。
    """
    policy = policy or DEFAULT_TABLE_POLICY
    priced = sorted(
        (row for row in rows if row.get("price") is not None),
        key=lambda row: float(row["price"]),
    )
    chosen: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def add(row: dict[str, Any]) -> bool:
        bucket = _bucket(row)
        if counts.get(bucket, 0) >= policy.bucket_cap or any(row is other for other in chosen):
            return False
        chosen.append(row)
        counts[bucket] = counts.get(bucket, 0) + 1
        return True

    if must_have is not None:
        first = next((row for row in priced if _bucket(row) == must_have), None)
        if first is None or size < 1:
            return None
        add(first)
    for row in priced:
        if len(chosen) >= size:
            break
        add(row)
    if len(chosen) < size:
        return None
    return sum(float(row["price"]) for row in chosen)


def diagnose_table(
    menu_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    picked: list[dict[str, Any]],
    people: int,
    budget: float | None = None,
    policy: TablePolicy | None = None,
) -> list[dict[str, Any]]:
    """回報這一桌沒達成的條件與原因。兩條推薦路徑共用（規劃 4.1、4.2）。

    menu_rows 是套用使用者條件「之前」的同一批菜，candidate_rows 是套用之後、
    實際交給 compose_table 的候選。預算與排除條件是硬性條件，這裡只解釋、
    永遠不放寬；必備類別與道數是軟性目標，沒達成就列出原因。

    over_budget 與 search_not_found 用 min_table_cost 精確區分：預算內根本不存在
    符合條件的一桌才算 over_budget；存在但貪婪組合沒挑到，就是 search_not_found。
    """
    policy = policy or DEFAULT_TABLE_POLICY
    unmet: list[dict[str, Any]] = []
    picked_buckets = {_bucket(row) for row in picked}

    def fits(cost: float | None) -> bool:
        return cost is not None and (budget is None or cost <= budget + 1e-9)

    for bucket in policy.required_buckets:
        if bucket in picked_buckets:
            continue
        in_menu = any(_bucket(row) == bucket for row in menu_rows)
        in_candidates = [row for row in candidate_rows if _bucket(row) == bucket]
        if not in_menu:
            cause = "absent_in_menu"
        elif not in_candidates:
            cause = "excluded_by_user"
        elif budget is not None and all(row.get("price") is None for row in in_candidates):
            cause = "price_unknown"
        elif budget is not None and not fits(
            min_table_cost(candidate_rows, max(1, len(picked)), policy, bucket)
        ):
            # 同樣道數、又要有這一類，預算內不存在這樣的一桌
            cause = "over_budget"
        else:
            cause = "search_not_found"
        unmet.append({"constraint": bucket, "cause": cause})

    target = policy.table_size(people)
    if len(picked) < target:
        chosen = {row.get("name") for row in picked}
        remaining = [row for row in candidate_rows if row.get("name") not in chosen]
        if not remaining:
            cause = "excluded_by_user" if len(menu_rows) >= target else "absent_in_menu"
        elif budget is not None and all(row.get("price") is None for row in remaining):
            cause = "price_unknown"
        elif budget is not None and not fits(
            min_table_cost(candidate_rows, len(picked) + 1, policy)
        ):
            cause = "over_budget"
        else:
            cause = "search_not_found"
        unmet.append(
            {"constraint": DISH_COUNT, "cause": cause, "target": target, "picked": len(picked)}
        )
    return unmet


def describe_unmet(unmet: list[dict[str, Any]]) -> str:
    """把未達原因轉成給使用者看的一句話。沒有未達就回空字串。"""
    sentences: list[str] = []
    by_cause: dict[str, list[str]] = {}
    for entry in unmet:
        if entry["constraint"] != DISH_COUNT:
            by_cause.setdefault(entry["cause"], []).append(entry["constraint"])
    templates = {
        "absent_in_menu": "這家店的菜單上沒有{label}類的品項，這一桌因此沒有{label}",
        "excluded_by_user": "{label}類的品項都被你的條件排除了，這一桌因此沒有{label}",
        "price_unknown": (
            "{label}類只剩時價或沒標價的品項，有預算時算不進總額，這一桌因此沒有{label}"
        ),
        "over_budget": "{label}類放不進這次的預算",
        "search_not_found": "{label}類有預算內的品項，但這次的組合沒有排進去，可以換掉其他菜再試",
    }
    for cause in UNMET_CAUSES:
        if cause in by_cause:
            sentences.append(templates[cause].format(label="、".join(by_cause[cause])))
    for entry in unmet:
        if entry["constraint"] == DISH_COUNT:
            picked, target = entry.get("picked", 0), entry.get("target", 0)
            sentences.append({
                "over_budget": f"預算內排得下 {picked} 道，少於建議的 {target} 道",
                "price_unknown": (
                    f"其餘品項是時價或沒標價，有預算時只排得出 {picked} 道（建議 {target} 道）"
                ),
                "excluded_by_user": f"符合你條件的品項只湊得出 {picked} 道（建議 {target} 道）",
                "absent_in_menu": f"菜單上的品項只湊得出 {picked} 道（建議 {target} 道）",
                "search_not_found": (
                    f"這次排出 {picked} 道（建議 {target} 道）；預算其實夠多點幾道，"
                    "較貴的必備菜佔掉了預算，可以換掉較貴的菜再試"
                ),
            }[entry["cause"]])
    return "；".join(sentences)
