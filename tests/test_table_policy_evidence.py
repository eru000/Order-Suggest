"""table_menu 的常數必須等於公開套餐資料推導出的數值（期末規劃節點 C，委員5-4）。

改了 evals/table_rules/set_menus.json 或推導方法，就要同步更新 table_menu.py 的
常數；反過來，不能只改常數而不改資料。這支測試讓兩邊不會悄悄分岔。
"""

import importlib.util
import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from table_menu import (  # noqa: E402
    DEFAULT_TABLE_POLICY,
    FOOD_GROUPS,
    compose_table,
    cover_cost,
    coverable_group_sets,
    describe_unmet,
    diagnose_table,
)


def load_derive():
    spec = importlib.util.spec_from_file_location(
        "derive_policy", ROOT / "evals" / "table_rules" / "derive_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyMatchesEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.derived = load_derive().derive()

    def test_every_set_menu_adds_up_to_its_title(self):
        self.assertEqual(self.derived["countErrors"], [])
        self.assertEqual(self.derived["sets"], 18)

    def test_dish_count_constants_match_regression(self):
        fit = self.derived["dishCountMain"]
        self.assertAlmostEqual(DEFAULT_TABLE_POLICY.dish_intercept, fit["intercept"], places=3)
        self.assertAlmostEqual(DEFAULT_TABLE_POLICY.dish_slope, fit["slope"], places=3)
        self.assertGreater(fit["r2"], 0.95)
        low, high = fit["peopleRange"]
        self.assertEqual(DEFAULT_TABLE_POLICY.people_range[1], high)

    def test_share_constants_match_set_menus(self):
        shares = self.derived["shares"]
        self.assertEqual({b for b, _ in DEFAULT_TABLE_POLICY.share_mean}, set(shares))
        for bucket, value in DEFAULT_TABLE_POLICY.share_mean:
            self.assertAlmostEqual(value, shares[bucket]["mean"], places=3, msg=bucket)
        for bucket, value in DEFAULT_TABLE_POLICY.share_max:
            self.assertAlmostEqual(value, shares[bucket]["max"], places=3, msg=bucket)

    def test_required_groups_are_present_in_every_set(self):
        coverage = self.derived["coverage"]
        total = coverage["total"]
        self.assertEqual(coverage["青菜"], total)
        self.assertEqual(coverage["豆魚蛋肉"], total)
        self.assertEqual(coverage["主食(每人白飯)"], total)
        # 湯：2 人 0/3，3 人以上 15/15，所以 soup_min_people = 3
        self.assertEqual(coverage["湯鍋(2人)"][0], 0)
        self.assertEqual(coverage["湯鍋(3人以上)"][0], coverage["湯鍋(3人以上)"][1])
        self.assertEqual(DEFAULT_TABLE_POLICY.soup_min_people, 3)

    def test_policy_reproduces_set_menu_dish_counts(self):
        # 迴歸四捨五入後，與每組套餐的主菜＋湯道數相差不超過 2 道（RMSE 0.44）
        for point in self.derived["points"]:
            predicted = DEFAULT_TABLE_POLICY.main_dishes(point["people"])
            self.assertLessEqual(abs(predicted - point["main"]), 2, point)


def rows(*specs):
    return [{"name": n, "price": p, "category": c, "family": n} for n, p, c in specs]


class BudgetTradeoffTest(unittest.TestCase):
    """預算不夠湊齊必備類別時，先湊最多類，再比道數，再比預算用量。"""

    def test_one_person_small_budget_keeps_protein(self):
        # 2026-11-27 recommendation eval：先放青菜和石鍋飯，剩的錢買不起任何蛋白質。
        menu = rows(
            ("杏鮑菇", 90, "蔬菜"), ("玉子鮭魚石鍋飯", 149, "主食"),
            ("酒蒸海鮮石鍋燒", 99, "海鮮"), ("霜降豬", 220, ""),
        )
        table = compose_table(menu, people=1, budget=300)
        buckets = {row["bucket"] for row in table}
        self.assertEqual(len(buckets & {"青菜", "主食", "肉類", "海鮮", "蛋豆腐"}), 2)
        self.assertTrue(buckets & set(FOOD_GROUPS["豆魚蛋肉"]))
        self.assertLessEqual(sum(row["price"] for row in table), 300)
        unmet = {entry["constraint"]: entry["cause"]
                 for entry in diagnose_table(menu, menu, table, 1, 300)}
        missing = [g for g in ("青菜", "主食") if not buckets & set(FOOD_GROUPS[g])]
        self.assertEqual(len(missing), 1)
        # 放不進的那一類要說「預算排不進」，不能說組合沒挑好
        self.assertEqual(unmet[missing[0]], "over_budget")
        self.assertIn("放不進這次的預算", describe_unmet(
            diagnose_table(menu, menu, table, 1, 300)))

    def test_coverable_group_sets_match_brute_force(self):
        menu = rows(("高麗菜", 120, ""), ("炒飯", 200, ""), ("排骨湯", 260, ""), ("叉燒", 300, ""))
        groups = ("青菜", "主食", "湯鍋", "豆魚蛋肉")
        for budget in range(0, 1000, 20):
            best = 0
            for size in range(len(groups) + 1):
                for combo in itertools.combinations(groups, size):
                    cost = cover_cost(menu, combo)
                    if cost is not None and cost <= budget:
                        best = max(best, size)
            sets = coverable_group_sets(menu, groups, budget)
            self.assertTrue(all(len(s) == best for s in sets), budget)
            self.assertTrue(all(cover_cost(menu, s) <= budget for s in sets), budget)


if __name__ == "__main__":
    unittest.main()
