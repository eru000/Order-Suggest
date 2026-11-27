"""合菜／熱炒／燒肉店的「一桌菜」組合。

這種店不是一個人點一道主餐，是一桌人點一桌菜。原本的推薦把它當成單點，
結果是「六個人 預算5000」推出三個火鍋加兩份壽司、沒有青菜也沒有主食，
而且只用掉 $630。

一桌菜的組成規則（期末規劃節點 C，回應委員5-4「規則缺乏依據」）原本是作者
自訂的「人數+1 道、必有青菜湯鍋主食、同類最多兩道」。現在每一項都改成有出處：

- 道數：三家連鎖餐廳官網 18 組多人合菜套餐（evals/table_rules/set_menus.json）
  的「主菜＋湯」道數對人數做線性迴歸，R²=0.975；每組套餐另附每人一份白飯，
  所以再加一道主食。
- 必備類別：衛福部國民健康署《每日飲食指南》與「我的餐盤」要求每餐都有蔬菜、
  全穀雜糧、豆魚蛋肉；18 組套餐也全部都有青菜、蛋白質與白飯。湯不是食物類別，
  改用套餐資料：3 人以上的 15 組全部附湯，2 人的 3 組都沒有（列為加價購）。
- 類別比例：用套餐的平均類別比例當權重，以 Sainte-Laguë 最高商數法依序分配
  道數（比例代表制的席次分配方法），取代原本寫死的輪流順序。
- 同類上限：任一類的道數不超過「套餐中該類出現過的最大比例 × 道數」。

數值都由 evals/table_rules/derive_policy.py 算出，
tests/test_table_policy_evidence.py 檢查這裡的常數與推導結果一致。
"""

from __future__ import annotations

import math
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

# 合菜分類補充的食材字（只用在 table_menu，不動 menu_vocabulary，所以不影響 menu_semantics
# 的標註與 SEMANTIC_SCHEMA_VERSION）。來源：derive_policy.py 以 106 道公開套餐菜名檢查分類器，
# 字表沒收的常見食材（四季豆、野蓮、茄、扁鱈、霸王骨、肺片等）。同一批菜名也拿來計算
# 修改後的正確率，所以那個數字不是獨立驗證。
TABLE_VEGETABLE_TERMS = (
    "四季豆", "野蓮", "茄", "白菜", "美人腿", "茭白筍", "秋葵", "山蘇", "龍鬚菜",
    "娃娃菜", "青江菜", "菠菜",
)
TABLE_SEAFOOD_TERMS = ("鱈", "鯛", "鱸", "鮭", "鯖", "鰻", "透抽")
TABLE_MEAT_TERMS = ("骨", "肋排", "肺片", "牛肚", "豬肚")

# 桶的順序就是判斷順序：先判鍋湯，否則「酸白菜肉鍋」會被青菜的「白菜」搶走；
# 主食要排在肉類前面，否則「鴨肉飯」會被判成肉類。
BUCKET_RULES: tuple[tuple[str, str], ...] = (
    # 「鍋」要在字尾才算鍋物：石鍋飯、鍋燒意麵是主食。
    ("湯鍋", r"鍋$|煲$|火鍋|湯$|羹$|鍋物"),
    ("主食", r"飯|麵|粥|米粉|冬粉|粄條|炒飯|炒麵"),
    ("青菜", r""),  # 見 _is_vegetable：葷素同名的字要另外判斷
    # 三家套餐都把蛋與豆腐獨立成一欄（蛋豆腐／蛋尬豆腐／蛋/豆腐），含蝦的
    # 「蝦扯蛋」「滑蛋蝦仁」也放在這欄，所以排在海鮮、肉類前面。鹹蛋是調味，
    # 蛋糕、蛋塔、杏仁豆腐是甜點。
    ("蛋豆腐", r"(?<!杏仁)豆腐|(?<!鹹)蛋(?!糕|塔|撻|捲|餅|黃酥)"),
    ("海鮮", pattern_of((*SEAFOOD_TERMS, *TABLE_SEAFOOD_TERMS))),
    ("肉類", pattern_of((*MEAT_TERMS, *TABLE_MEAT_TERMS)) + r"|燒臘"),
    ("點心", pattern_of(DESSERT_TERMS) + r"|水果|果盤|西瓜|鳳梨"),
)

# 品名後面的份量標註：「淮山蛤蜊雞湯(大)」「秘醬炙烤肋排（10塊）」。不去掉的話
# 「湯$」這類字尾規則永遠對不到，雞湯會被判成海鮮（2026-11-27 以套餐菜名實測）。
_SIZE_SUFFIX = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")


def _is_vegetable(value: str) -> bool:
    if any_term(VEGETABLE_TERMS, value):
        return True
    # 補充的蔬菜字也當成葷素同名處理：「絲瓜蛤蠣」「蒼蠅頭」有葷料就不算青菜
    return any_term((*AMBIGUOUS_VEGETABLE_TERMS, *TABLE_VEGETABLE_TERMS), value) and not any_term(
        (*MEAT_TERMS, *SEAFOOD_TERMS, *TABLE_MEAT_TERMS, *TABLE_SEAFOOD_TERMS), value
    )


# 國健署「豆魚蛋肉類」對應到三個桶；其餘必備類別各自一個桶。
FOOD_GROUPS: dict[str, tuple[str, ...]] = {
    "青菜": ("青菜",),
    "主食": ("主食",),
    "湯鍋": ("湯鍋",),
    "豆魚蛋肉": ("肉類", "海鮮", "蛋豆腐"),
}

# ── 由 evals/table_rules/derive_policy.py 推導（evals/results/table_rules/derived.json）
# 道數（主菜＋湯，不含小菜與白飯）= 截距 + 斜率 × 人數，18 組套餐，R²=0.975，RMSE=0.44
DISH_INTERCEPT = 1.972
DISH_SLOPE = 1.093
# 套餐資料涵蓋 2–10 人；1 人外插一格（約 3 道），超過 10 人不外插。
PEOPLE_RANGE = (1, 10)
# 每類佔道數的平均比例（Sainte-Laguë 權重）與最大比例（同類上限）
SHARE_MEAN: tuple[tuple[str, float], ...] = (
    ("肉類", 0.407), ("青菜", 0.178), ("海鮮", 0.147),
    ("蛋豆腐", 0.144), ("湯鍋", 0.116), ("主食", 0.007),
)
SHARE_MAX: tuple[tuple[str, float], ...] = (
    ("肉類", 0.571), ("青菜", 0.25), ("海鮮", 0.37),
    ("蛋豆腐", 0.25), ("湯鍋", 0.2), ("主食", 0.125),
)
# 一桌一定要有的。湯只在 3 人以上必備（套餐 15/15 有湯，2 人 0/3）。
ALWAYS_REQUIRED = ("青菜", "主食", "豆魚蛋肉")
SOUP_MIN_PEOPLE = 3
# 套餐沒有的類別：分類器認不出來的「其他」與點心，只在其他類都排不進時才補。
FALLBACK_BUCKETS = ("其他", "點心")

# 舊名稱保留給既有呼叫端與測試；內容改為依人數決定，見 TablePolicy.required_groups。
REQUIRED_BUCKETS = ("青菜", "湯鍋", "主食")


def required_groups_for(people: int) -> tuple[str, ...]:
    return DEFAULT_TABLE_POLICY.required_groups(people)


def absent_required_buckets(
    rows: list[dict[str, Any]], people: int = SOUP_MIN_PEOPLE, policy: TablePolicy | None = None,
) -> list[str]:
    """必備類別裡，候選中完全沒有品項的那幾類。

    「這一桌沒有湯」有兩種原因：菜單上根本沒有湯品（燒肉店就是這樣），或是
    有湯但預算排不進去。使用者該看到的說明完全不同，而原本的程式兩種都只是
    安靜跳過，結果看起來一樣——口試委員就是把前者讀成「規則的例外處理」。

    這裡只回報前者：候選中一項都沒有。實測四份菜單，森森燒肉的湯鍋是 0 項、
    大肥鵝的主食是 0 項，兩者都屬於這一類。
    """
    policy = policy or DEFAULT_TABLE_POLICY
    present = {
        bucket_of(str(row.get("name") or ""), str(row.get("category") or ""))
        for row in rows
    }
    return [
        group for group in policy.required_groups(people)
        if not present & set(FOOD_GROUPS.get(group, (group,)))
    ]


def bucket_of(name: str, category: str = "") -> str:
    # 品名與分類分開比對：「鍋$」這種字尾規則接上分類字串就永遠對不到，
    # 「東北酸白菜肉鍋」會因為後面多一個空白掉進肉類。
    name = name.strip()
    while _SIZE_SUFFIX.search(name):
        stripped = _SIZE_SUFFIX.sub("", name)
        if not stripped:
            break
        name = stripped
    values = [value for value in (name, category.strip()) if value]
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


_SET_MENUS = "evals/table_rules/set_menus.json（饗泰多、真珠、開飯川食堂官網套餐，2026-11-27 擷取）"
_GUIDE = (
    "衛生福利部國民健康署（2018）。每日飲食指南手冊；「我的餐盤」：每餐蔬菜、"
    "全穀雜糧、豆魚蛋肉都要有"
)


@dataclass(frozen=True)
class TablePolicy:
    """一桌菜的組成政策（期末規劃 4.3／節點 C，回應委員5-4「規則缺乏依據」）。

    每個預設值都在 sources 註明依據。換一組數值（例如另一批套餐資料推導的結果）
    就是換一個 TablePolicy，不必改程式。
    """

    dish_intercept: float = DISH_INTERCEPT
    dish_slope: float = DISH_SLOPE
    people_range: tuple[int, int] = PEOPLE_RANGE
    staple_dishes: int = 1
    always_required: tuple[str, ...] = ALWAYS_REQUIRED
    soup_min_people: int = SOUP_MIN_PEOPLE
    share_mean: tuple[tuple[str, float], ...] = SHARE_MEAN
    share_max: tuple[tuple[str, float], ...] = SHARE_MAX
    fallback_buckets: tuple[str, ...] = FALLBACK_BUCKETS
    sources: tuple[tuple[str, str], ...] = (
        ("dish_intercept/dish_slope",
         f"{_SET_MENUS}：18 組套餐「主菜＋湯」道數對人數的最小平方法，R²=0.975"),
        ("staple_dishes", f"{_SET_MENUS}：18/18 組套餐都附每人一份白飯，不算在菜數內"),
        ("people_range", f"{_SET_MENUS}：資料只涵蓋 2–10 人"),
        ("always_required", f"{_GUIDE}；{_SET_MENUS}：18/18 組都有青菜、蛋白質與白飯"),
        ("soup_min_people", f"{_SET_MENUS}：3 人以上 15/15 組附湯，2 人 0/3 組（加價購）"),
        ("share_mean",
         f"{_SET_MENUS}：各類平均比例；分配方法為 Sainte-Laguë 最高商數法"
         "（Balinski & Young, 2001）"),
        ("share_max", f"{_SET_MENUS}：各類在任一組套餐中的最大比例"),
        ("fallback_buckets", "套餐資料沒有這兩類（甜點另計），只在其他類都排不進時補位"),
    )

    def main_dishes(self, people: int) -> int:
        """主菜＋湯的道數，不含主食。"""
        low, high = self.people_range
        p = min(high, max(low, int(people)))
        return max(1, math.floor(self.dish_intercept + self.dish_slope * p + 0.5))

    def table_size(self, people: int) -> int:
        return self.main_dishes(people) + self.staple_dishes

    def required_groups(self, people: int) -> tuple[str, ...]:
        groups = list(self.always_required)
        if int(people) >= self.soup_min_people:
            groups.insert(groups.index("主食") + 1 if "主食" in groups else len(groups), "湯鍋")
        return tuple(groups)

    def caps_for_dishes(self, dishes: int) -> dict[str, int]:
        caps = {
            bucket: max(1, math.floor(share * dishes + 1e-9)) for bucket, share in self.share_max
        }
        # 分類器認不出來的菜沒有資料，套用所有類別中最寬的那個上限。
        widest = max(share for _, share in self.share_max)
        for bucket in self.fallback_buckets:
            caps.setdefault(bucket, max(1, math.floor(widest * dishes + 1e-9)))
        return caps

    def caps(self, people: int) -> dict[str, int]:
        return self.caps_for_dishes(self.main_dishes(people))


DEFAULT_TABLE_POLICY = TablePolicy()


def table_size(people: int, policy: TablePolicy | None = None) -> int:
    """建議道數：套餐迴歸的主菜＋湯，再加一道主食。"""
    return (policy or DEFAULT_TABLE_POLICY).table_size(people)


def _cap(caps: dict[str, int], bucket: str) -> int:
    return caps.get(bucket, 1)


def _cheapest(rows: list[dict[str, Any]], group: str) -> float | None:
    members = set(FOOD_GROUPS.get(group, (group,)))
    prices = [float(row["price"]) for row in rows
              if row.get("price") is not None and _bucket(row) in members]
    return min(prices) if prices else None


def cover_cost(rows: list[dict[str, Any]], groups: tuple[str, ...]) -> float | None:
    """同時放進這幾類各一道的最低總價。各類別的桶互不重疊，所以就是各類最便宜一道相加。"""
    total = 0.0
    for group in groups:
        price = _cheapest(rows, group)
        if price is None:
            return None
        total += price
    return total


def coverable_group_sets(
    rows: list[dict[str, Any]], groups: tuple[str, ...], budget: float | None,
) -> list[tuple[str, ...]]:
    """預算內能同時湊齊、而且類別數最多的那些必備類別組合（可能不只一組）。

    必備類別最多 4 類，16 種子集合直接窮舉就是精確解。以前依序貪婪：一個人預算
    $300 先放了青菜和 $149 的石鍋飯，剩下的錢買不起任何蛋白質，但其實
    「青菜＋肉」放得進預算（2026-11-27 recommendation eval）。
    沒有標價品項的類別（全是時價或菜單沒有）不參與取捨，留給 diagnose_table 說明。
    """
    if budget is None:
        return [groups]
    available = [group for group in groups if _cheapest(rows, group) is not None]
    feasible: list[tuple[str, ...]] = []
    for mask in range(1 << len(available)):
        subset = tuple(group for i, group in enumerate(available) if mask >> i & 1)
        cost = cover_cost(rows, subset)
        if cost is not None and cost <= budget + 1e-9:
            feasible.append(subset)
    most = max(len(subset) for subset in feasible)
    return [subset for subset in feasible if len(subset) == most]


def compose_table(
    rows: list[dict[str, Any]],
    people: int,
    budget: float | None = None,
    policy: TablePolicy | None = None,
) -> list[dict[str, Any]]:
    """湊一桌。rows 必須是已經套過忌口、過敏等條件的候選。

    每個品項會加上 "bucket"。價格未知（時價）的品項在有預算時不列入，
    因為算不進總額，等於讓使用者看不出這桌要多少錢。

    先依 required_groups 各放一道，剩下的道數用 Sainte-Laguë 最高商數法分配：
    每次挑「權重 ÷ (2 × 已有道數 + 1)」最大的類別，結果會逼近套餐的平均比例。
    """
    policy = policy or DEFAULT_TABLE_POLICY
    pool = [dict(row, bucket=bucket_of(str(row["name"]), str(row.get("category") or "")))
            for row in rows]
    if budget is not None:
        pool = [row for row in pool if row.get("price") is not None]
    if not pool:
        return []

    count = min(policy.table_size(people), len(pool))
    caps = policy.caps(people)
    weights = dict(policy.share_mean)
    rank = {bucket: index for index, (bucket, _) in enumerate(policy.share_mean)}
    per_dish = (budget / count) if budget else None
    weighted = tuple(bucket for bucket, weight in policy.share_mean if weight > 0)

    def build(targets: tuple[str, ...]) -> tuple[list[dict[str, Any]], float]:
        picked: list[dict[str, Any]] = []
        spent = 0.0
        reserve = 0.0

        def affordable(row: dict[str, Any]) -> bool:
            if budget is None:
                return True
            price = row.get("price")
            return price is not None and spent + price + reserve <= budget + 1e-9

        def taken(bucket: str) -> int:
            return sum(1 for row in picked if row["bucket"] == bucket)

        def quotient(bucket: str) -> float:
            return weights.get(bucket, 0.0) / (2 * taken(bucket) + 1)

        def take_from(bucket: str) -> bool:
            nonlocal spent
            if taken(bucket) >= _cap(caps, bucket):
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
                    row.get("spiceUncertain", False), abs((row["price"] or 0) - per_dish),
                    row["name"],
                ))
            else:
                options.sort(key=lambda row: (
                    row.get("spiceUncertain", False), row.get("price") is None,
                    row.get("price") or 0,
                ))
            picked.append(options[0])
            spent += options[0].get("price") or 0.0
            return True

        def by_quotient(buckets: tuple[str, ...]) -> list[str]:
            return sorted(buckets, key=lambda b: (-quotient(b), rank.get(b, len(rank))))

        # 必備類別各放一道；放每一類時替後面的類別保留最便宜一道的錢。
        for index, group in enumerate(targets):
            if len(picked) >= count:
                break
            later = cover_cost(pool, targets[index + 1:])
            reserve = later or 0.0
            for bucket in by_quotient(FOOD_GROUPS.get(group, (group,))):
                if take_from(bucket):
                    break
        reserve = 0.0
        while len(picked) < count:
            if any(take_from(bucket) for bucket in by_quotient(weighted)):
                continue
            if any(take_from(bucket) for bucket in policy.fallback_buckets):
                continue
            break
        return picked, spent

    # 目標依序是：必備類別湊得最齊（國健署每餐三類）、道數最接近建議（套餐迴歸）、
    # 預算用得最足。預算不夠湊齊全部必備類別時，每一種「最多類」的組合都試一次。
    groups = policy.required_groups(people)
    best: tuple[list[dict[str, Any]], float] | None = None
    for subset in coverable_group_sets(pool, groups, budget):
        targets = tuple(group for group in groups if group in subset)
        result = build(targets)
        if best is None or (len(result[0]), result[1]) > (len(best[0]), best[1]):
            best = result
    picked = best[0] if best else []
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
    people: int | None = None,
) -> float | None:
    """size 道、守住同類上限（且含 must_have 類）的一桌，最低可能總價；湊不出來回 None。

    同類上限構成分割擬陣（partition matroid，各類容量可以不同），在擬陣上依價格
    由低到高貪婪挑選就是最低總價。強制包含某個桶時先放該桶最便宜的一道再貪婪，
    同樣是最佳解（同桶內交換不影響可行性）。must_have 是食物類別（例如豆魚蛋肉，
    對應多個桶）時，對每個桶各算一次取最小值：最佳解一定含其中某個桶，而含該桶的
    最佳解就是上述強制版本。tests/test_table_menu.py 用小菜單窮舉驗證（規劃 4.5）。

    同類上限依 people 的建議道數計算；沒給 people 時以 size 當道數。只看有標價的品項。
    """
    policy = policy or DEFAULT_TABLE_POLICY
    caps = policy.caps(people) if people is not None else policy.caps_for_dishes(size)
    priced = sorted(
        (row for row in rows if row.get("price") is not None),
        key=lambda row: float(row["price"]),
    )

    def solve(forced: str | None) -> float | None:
        chosen: list[dict[str, Any]] = []
        counts: dict[str, int] = {}

        def add(row: dict[str, Any]) -> bool:
            bucket = _bucket(row)
            if counts.get(bucket, 0) >= _cap(caps, bucket) or any(row is other for other in chosen):
                return False
            chosen.append(row)
            counts[bucket] = counts.get(bucket, 0) + 1
            return True

        if forced is not None:
            first = next((row for row in priced if _bucket(row) == forced), None)
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

    if must_have is None:
        return solve(None)
    costs = [cost for bucket in FOOD_GROUPS.get(must_have, (must_have,))
             if (cost := solve(bucket)) is not None]
    return min(costs) if costs else None


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

    covered = [
        group for group in policy.required_groups(people)
        if picked_buckets & set(FOOD_GROUPS.get(group, (group,)))
    ]
    for group in policy.required_groups(people):
        members = set(FOOD_GROUPS.get(group, (group,)))
        if picked_buckets & members:
            continue
        in_menu = any(_bucket(row) in members for row in menu_rows)
        in_candidates = [row for row in candidate_rows if _bucket(row) in members]
        if not in_menu:
            cause = "absent_in_menu"
        elif not in_candidates:
            cause = "excluded_by_user"
        elif budget is not None and all(row.get("price") is None for row in in_candidates):
            cause = "price_unknown"
        elif budget is not None and (
            not fits(min_table_cost(candidate_rows, max(1, len(picked)), policy, group, people))
            or not fits(cover_cost(candidate_rows, (*covered, group)))
        ):
            # 同樣道數、又要有這一類，預算內不存在這樣的一桌；或是要放這一類，
            # 就得拿掉另一個已經放進來的必備類別
            cause = "over_budget"
        else:
            cause = "search_not_found"
        unmet.append({"constraint": group, "cause": cause})

    target = policy.table_size(people)
    if len(picked) < target:
        chosen = {row.get("name") for row in picked}
        remaining = [row for row in candidate_rows if row.get("name") not in chosen]
        if not remaining:
            cause = "excluded_by_user" if len(menu_rows) >= target else "absent_in_menu"
        elif budget is not None and all(row.get("price") is None for row in remaining):
            cause = "price_unknown"
        elif budget is not None and not fits(
            min_table_cost(candidate_rows, len(picked) + 1, policy, people=people)
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
