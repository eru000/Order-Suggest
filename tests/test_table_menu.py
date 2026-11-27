import itertools
import random
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from table_menu import (  # noqa: E402
    TablePolicy,
    absent_required_buckets,
    bucket_of,
    compose_table,
    describe_unmet,
    diagnose_table,
    is_shared_table,
    min_table_cost,
    table_size,
)


def rows(*specs):
    return [
        {"name": name, "price": price, "category": category, "family": name}
        for name, price, category in specs
    ]


class BucketTest(unittest.TestCase):
    def test_hot_pot_by_name_suffix_even_without_category(self):
        # 「鍋$」曾經因為品名後面被接上空白的分類而永遠對不到。
        self.assertEqual(bucket_of("東北酸白菜肉鍋"), "湯鍋")
        self.assertEqual(bucket_of("蟹黃豆腐煲"), "湯鍋")
        self.assertEqual(bucket_of("招牌雞", "經典鍋物"), "湯鍋")

    def test_rice_and_noodles_are_staples_not_hot_pot(self):
        self.assertEqual(bucket_of("玉子鮭魚石鍋飯"), "主食")
        self.assertEqual(bucket_of("鍋燒意麵"), "主食")

    def test_vegetable_terms_that_come_with_meat(self):
        # 明確的蔬菜名一律算青菜，葷素同名的（筍、菇）有肉就算肉。
        self.assertEqual(bucket_of("鵝油高麗菜"), "青菜")
        self.assertEqual(bucket_of("清炒蘆筍"), "青菜")
        self.assertEqual(bucket_of("蘆筍牛肉"), "肉類")
        self.assertEqual(bucket_of("控肉桂竹筍"), "肉類")


class AbsentRequiredBucketTest(unittest.TestCase):
    """「菜單上沒有這類菜」要跟「排不進預算」分開回報。

    口試委員指出表4.6 裡森森燒肉的三組推薦都沒有湯鍋，質疑是規則的例外。
    實際原因是燒肉店菜單上一項湯品都沒有，但原本的程式只是安靜跳過，
    兩種情形的輸出一模一樣，看不出差別。
    """

    def test_yakiniku_menu_has_no_soup_bucket(self):
        pool = rows(
            ("香菇", 60, "蔬菜"),
            ("玉子鮭魚石鍋飯", 180, "主食"),
            ("霜降牛小排", 320, "牛肉"),
        )
        self.assertEqual(absent_required_buckets(pool), ["湯鍋"])

    def test_goose_menu_has_no_staple_bucket(self):
        pool = rows(
            ("鵝油高麗菜", 120, "蔬菜"),
            ("蟹黃豆腐煲", 330, "鍋物"),
            ("燒臘雙拼盤", 480, "肉類"),
        )
        self.assertEqual(absent_required_buckets(pool), ["主食"])

    def test_nothing_absent_when_all_required_groups_present(self):
        # 必備類別依國健署「我的餐盤」：蔬菜、全穀雜糧（主食）、豆魚蛋肉，3 人以上再加湯。
        pool = rows(
            ("清炒蘆筍", 120, "蔬菜"),
            ("當歸清湯", 60, "湯品"),
            ("鴨肉飯", 50, "主食"),
            ("蜜汁叉燒", 380, "燒臘"),
        )
        self.assertEqual(absent_required_buckets(pool), [])

    def test_protein_group_is_reported_when_missing(self):
        pool = rows(("清炒蘆筍", 120, "蔬菜"), ("當歸清湯", 60, "湯品"), ("鴨肉飯", 50, "主食"))
        self.assertEqual(absent_required_buckets(pool), ["豆魚蛋肉"])

    def test_soup_is_not_required_for_two_people(self):
        # 套餐資料：2 人的 3 組都不附湯，3 人以上 15/15 組附湯。
        pool = rows(("清炒蘆筍", 120, "蔬菜"), ("鴨肉飯", 50, "主食"), ("蜜汁叉燒", 380, "燒臘"))
        self.assertEqual(absent_required_buckets(pool, people=2), [])
        self.assertEqual(absent_required_buckets(pool, people=3), ["湯鍋"])

    def test_unpriced_items_still_count_as_present(self):
        # 時價的湯品不會被推薦，但「菜單上有湯」這件事不變；
        # 混在一起的話，有湯的店會被說成沒有湯。
        pool = [{"name": "時價鮮魚湯", "price": None, "category": "湯品", "family": "湯"}]
        self.assertNotIn("湯鍋", absent_required_buckets(pool))


class SharedTableTest(unittest.TestCase):
    def test_detection_uses_menu_shape(self):
        self.assertTrue(is_shared_table(135, 1))    # 大肥鵝
        self.assertTrue(is_shared_table(81, 9))     # 森森燒肉
        self.assertFalse(is_shared_table(28, 9))    # 斗六當歸鴨
        self.assertFalse(is_shared_table(8, 7))     # 奔頂牛排
        self.assertFalse(is_shared_table(20, 0))    # 品項太少，看不出是合菜店

    def test_table_size_follows_set_menu_regression(self):
        # 主菜＋湯 = round(1.972 + 1.093 × 人數)，再加一道主食（套餐每人附白飯）。
        # 對照套餐原始資料：4 人 6–7 道、6 人 8–9 道、10 人 12–13 道（不含小菜）。
        self.assertEqual(table_size(2), 5)
        self.assertEqual(table_size(4), 7)
        self.assertEqual(table_size(6), 10)
        self.assertEqual(table_size(10), 14)
        self.assertEqual(table_size(1), 4)    # 資料從 2 人起，1 人外插一格
        self.assertEqual(table_size(20), 14)  # 超過 10 人資料沒有涵蓋，不外插


class ComposeTableTest(unittest.TestCase):
    def menu_rows(self):
        return rows(
            ("清炒時蔬", 250, "季節時蔬"),
            ("鵝油高麗菜", 220, "季節時蔬"),
            ("酸白菜肉鍋", 880, "經典鍋物"),
            ("魷魚螺肉蒜鍋", 1080, "經典鍋物"),
            ("燒臘雙拼盤", 480, "潮粵燒臘"),
            ("川味水煮牛", 680, "明火好味"),
            ("菠蘿蝦球", 450, "明火好味"),
            ("蒜香蒸魚", 520, "海鮮區"),
            ("櫻花蝦炒飯", 300, "主食"),
            ("什錦水果盤", 300, "水果甜品"),
        )

    def test_table_has_vegetable_soup_and_staple(self):
        # 4 人 7 道（6 道主菜＋湯、1 道主食）在這份菜單約 $3560，預算給足。
        table = compose_table(self.menu_rows(), people=4, budget=4000)
        buckets = [row["bucket"] for row in table]
        self.assertEqual(len(table), table_size(4))
        for required in ("青菜", "湯鍋", "主食"):
            self.assertIn(required, buckets)
        self.assertTrue({"肉類", "海鮮", "蛋豆腐"} & set(buckets))

    def test_stays_within_budget(self):
        table = compose_table(self.menu_rows(), people=6, budget=2500)
        self.assertLessEqual(sum(row["price"] for row in table), 2500)

    def test_bucket_caps_follow_set_menu_shares(self):
        # 同類上限 = 套餐中該類最大比例 × 道數（下限 1）：6 人 9 道主菜，湯鍋最多 1 道。
        policy = TablePolicy()
        caps = policy.caps(6)
        self.assertEqual(caps["湯鍋"], 1)
        table = compose_table(self.menu_rows(), people=6, budget=6000)
        for bucket in {row["bucket"] for row in table}:
            self.assertLessEqual(sum(1 for row in table if row["bucket"] == bucket), caps[bucket])

    def test_fill_order_approaches_set_menu_shares(self):
        # Sainte-Laguë：必備各一道之後，依「平均比例 ÷ (2×已有道數+1)」挑下一類。
        menu = rows(
            ("清炒時蔬", 200, "蔬菜"), ("鵝油高麗菜", 200, "蔬菜"),
            ("排骨湯", 200, "湯品"), ("酸菜鍋", 200, "鍋物"),
            ("炒飯", 200, "主食"),
            ("叉燒", 200, ""), ("滷肉", 200, ""), ("三杯雞", 200, ""),
            ("中卷", 200, ""), ("蝦球", 200, ""),
            ("麻婆豆腐", 200, ""), ("菜脯蛋", 200, ""),
        )
        table = compose_table(menu, people=4)
        counts = {}
        for row in table:
            counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1
        self.assertEqual(
            counts, {"青菜": 1, "主食": 1, "湯鍋": 1, "肉類": 2, "海鮮": 1, "蛋豆腐": 1}
        )

    def test_market_price_items_are_left_out_when_a_budget_is_set(self):
        pool = [*self.menu_rows(), *rows(("時價龍蝦", None, "海鮮區"))]
        table = compose_table(pool, people=4, budget=3000)
        self.assertNotIn("時價龍蝦", [row["name"] for row in table])
        # 沒有預算就無所謂，時價可以出現
        self.assertTrue(compose_table(pool, people=4, budget=None))


class DiagnoseTableTest(unittest.TestCase):
    """這一桌少了什麼、為什麼少（期末規劃 4.2，委員5-7）。

    舊版只用「篩選後的候選」判斷，使用者說「不吃鍋」也會被講成「這家店的菜單上
    沒有湯鍋」；時價湯品、預算排不進、貪婪沒挑到則完全不說明。
    """

    def menu(self):
        return rows(
            ("鵝油高麗菜", 220, "季節時蔬"),
            ("蒜炒地瓜葉", 180, "季節時蔬"),
            ("酸白菜肉鍋", 880, "經典鍋物"),
            ("蜜汁叉燒", 380, "潮粵燒臘"),
            ("三杯中卷", 380, "海鮮區"),
            ("櫻花蝦炒飯", 300, "主食"),
        )

    def causes(self, unmet):
        return {entry["constraint"]: entry["cause"] for entry in unmet}

    def test_absent_in_menu(self):
        menu = [row for row in self.menu() if row["name"] != "櫻花蝦炒飯"]
        table = compose_table(menu, people=2)
        self.assertEqual(self.causes(diagnose_table(menu, menu, table, 2))["主食"], "absent_in_menu")

    def test_user_exclusion_is_not_reported_as_absent(self):
        menu = self.menu()
        candidates = [row for row in menu if "鍋" not in row["name"]]
        table = compose_table(candidates, people=3)
        unmet = diagnose_table(menu, candidates, table, 3)
        self.assertEqual(self.causes(unmet)["湯鍋"], "excluded_by_user")
        self.assertIn("被你的條件排除", describe_unmet(unmet))
        self.assertNotIn("菜單上沒有湯鍋", describe_unmet(unmet))

    def test_market_price_soup_under_budget(self):
        menu = [dict(row, price=None) if "鍋" in row["name"] else row for row in self.menu()]
        table = compose_table(menu, people=3, budget=3000)
        self.assertEqual(self.causes(diagnose_table(menu, menu, table, 3, 3000))["湯鍋"], "price_unknown")

    def test_soup_that_cannot_fit_the_budget(self):
        menu = self.menu()
        table = compose_table(menu, people=3, budget=700)
        self.assertNotIn("酸白菜肉鍋", [row["name"] for row in table])
        self.assertEqual(self.causes(diagnose_table(menu, menu, table, 3, 700))["湯鍋"], "over_budget")

    def test_dish_count_short_because_expensive_required_dish_used_the_budget(self):
        # 3 人目標 6 道。貪婪先放 880 的鍋，預算 1700 只排得到 4 道；
        # 但不放鍋的話 6 道只要 1550。
        menu = [*self.menu(), *rows(("麻婆豆腐", 160, ""), ("滷肉", 150, ""))]
        table = compose_table(menu, people=3, budget=1700)
        self.assertLess(len(table), table_size(3))
        unmet = diagnose_table(menu, menu, table, 3, 1700)
        self.assertEqual(self.causes(unmet)["道數"], "search_not_found")

    def test_dish_count_short_because_budget_is_small(self):
        menu = self.menu()
        table = compose_table(menu, people=4, budget=500)
        self.assertEqual(self.causes(diagnose_table(menu, menu, table, 4, 500))["道數"], "over_budget")

    def test_hard_constraints_are_never_relaxed(self):
        menu = self.menu()
        for budget in (300, 500, 700, 1000, 1500):
            table = compose_table(menu, people=4, budget=budget)
            self.assertLessEqual(sum(row["price"] for row in table), budget)

    def test_policy_parameters_are_explicit_and_sourced(self):
        policy = TablePolicy()
        self.assertEqual(policy.table_size(4), 7)
        self.assertEqual(TablePolicy(staple_dishes=0).table_size(4), 6)
        self.assertTrue(all(source for _, source in policy.sources))
        # 沒有任何一項還是作者預設
        self.assertFalse(any("author_default" in source for _, source in policy.sources))

    def test_min_table_cost_matches_brute_force_on_small_menus(self):
        """規劃 4.5：在可窮舉的小菜單上確認最低總價計算正確。"""
        names = ["高麗菜", "地瓜葉", "排骨湯", "酸菜鍋", "炒飯", "炒麵", "叉燒", "中卷", "蝦球", "滷肉"]
        names = [*names, "麻婆豆腐", "菜脯蛋", "三杯雞"]
        policy = TablePolicy()
        groups = {None: None, "湯鍋": {"湯鍋"}, "主食": {"主食"},
                  "豆魚蛋肉": {"肉類", "海鮮", "蛋豆腐"}}
        rng = random.Random(20261126)
        for _ in range(120):
            menu = rows(*[(name, rng.randrange(50, 900, 10), "") for name in rng.sample(names, 9)])
            for people in (2, 4, 6):
                caps = policy.caps(people)
                for size in range(1, 6):
                    for must, members in groups.items():
                        best = None
                        for combo in itertools.combinations(menu, size):
                            buckets = [bucket_of(row["name"]) for row in combo]
                            if members and not members & set(buckets):
                                continue
                            if any(buckets.count(b) > caps.get(b, 1) for b in set(buckets)):
                                continue
                            cost = sum(row["price"] for row in combo)
                            best = cost if best is None else min(best, cost)
                        self.assertEqual(
                            min_table_cost(menu, size, policy, must, people), best,
                            (people, size, must),
                        )


if __name__ == "__main__":
    unittest.main()
