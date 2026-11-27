"""節點 6（委員5-5）：提問分數的公式與缺標註時的邊界行為。"""

import math
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from decision_service import QUESTION_STRATEGIES, DecisionService, question_score  # noqa: E402


def rows(dish_types, total=100.0):
    return [
        {"id": str(i), "name": f"菜{i}", "total": total, "dishType": value, "texture": None,
         "protein": None, "taste": None, "portion": None}
        for i, value in enumerate(dish_types)
    ]


def ask(items, strategy="gini"):
    d = {"prefs": {}, "asked": [], "skipped": []}
    return DecisionService._next_question(d, items, strategy=strategy)


class QuestionScoreTest(unittest.TestCase):
    def test_formulas(self):
        # gini：(1 − Σp²) × K/N；entropy：H/log₂4 × K/N
        self.assertAlmostEqual(question_score({"飯": 2, "麵": 2}, 4, 4, "gini"), 0.5)
        self.assertAlmostEqual(question_score({"飯": 2, "麵": 2}, 4, 4, "entropy"), 0.5)
        self.assertAlmostEqual(question_score({"飯": 3, "麵": 1}, 4, 4, "gini"), 0.375)
        h = -(0.75 * math.log2(0.75) + 0.25 * math.log2(0.25))
        self.assertAlmostEqual(question_score({"飯": 3, "麵": 1}, 4, 4, "entropy"), h / 2)
        self.assertAlmostEqual(question_score({"飯": 2, "麵": 2}, 4, 8, "gini"), 0.25)  # 覆蓋率折扣

    def test_no_division_by_zero_without_labels(self):
        for strategy in QUESTION_STRATEGIES:
            self.assertEqual(question_score({}, 0, 0, strategy), 0.0)
            self.assertEqual(question_score({}, 0, 10, strategy), 0.0)


class EdgeCaseTest(unittest.TestCase):
    """缺標註、標註不足、只有一類、只有一道菜時都不能問出沒意義的題目，也不能出錯。"""

    def test_zero_labels_asks_nothing(self):
        for strategy in QUESTION_STRATEGIES:
            self.assertIsNone(ask(rows([None] * 8), strategy))

    def test_coverage_threshold_is_seventy_percent(self):
        enough = rows(["飯", "麵", "飯", "麵", "飯", "麵", "飯", None, None, None])
        too_few = rows(["飯", "麵", "飯", "麵", "飯", "麵", None, None, None, None])
        self.assertEqual(ask(enough)["field"], "dishType")
        self.assertIsNone(ask(too_few))

    def test_all_items_same_value_asks_nothing(self):
        for strategy in QUESTION_STRATEGIES:
            self.assertIsNone(ask(rows(["飯"] * 6), strategy))

    def test_single_item_menu_asks_nothing(self):
        for strategy in QUESTION_STRATEGIES:
            self.assertIsNone(ask(rows(["飯"]), strategy))

    def test_fixed_order_asks_budget_first(self):
        items = rows(["飯", "麵", "飯", "麵", "飯", "麵"])
        for row, total in zip(items, (40, 60, 80, 100, 120, 140), strict=True):
            row["total"] = float(total)
        self.assertEqual(ask(items, "fixed")["field"], "budget")

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            DecisionService(None, {}, "x", question_strategy="magic")


if __name__ == "__main__":
    unittest.main()
