"""現有 recommend() 行為的基準線。

這些測試不是在主張目前的邏輯是對的，而是把「合併對方的推薦引擎之前，
recommend() 實際會做什麼」釘住。對方的版本只回傳主餐、也沒有加料偵測，
所以這幾項最容易在合併時無聲消失。

固定行為的做法：
- 每一類品項刻意低於 recommend() 內部洗牌門檻（主食 >5、其他 >3 才洗牌），
  比固定隨機種子穩固
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ollama_fuc import recommend  # noqa: E402


def build_menu(items: List[Dict[str, Any]], category: str = "全部菜色") -> Dict[str, Any]:
    return {"restaurants": {"測試餐廳": {"categories": {category: {"items": items}}}}}


def run_recommend(menu: Dict[str, Any], prefs: Optional[Dict[str, Any]] = None, top_k: int = 5):
    """呼叫 recommend() 並吞掉它大量的除錯輸出。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return recommend(menu, prefs, top_k=top_k)


def reasons(result: Dict[str, Any]) -> List[str]:
    return [item["reason"] for item in result["items"]]


def names_by_reason(result: Dict[str, Any], reason: str) -> List[str]:
    return [item["name"] for item in result["items"] if item["reason"] == reason]


class RecommendBaselineTest(unittest.TestCase):
    # --- 加料偵測（對方完全沒有這個概念）---

    def test_addon_below_40_percent_of_median_is_forced_to_side(self):
        """「加飯」用關鍵字會被判成主食，但價格低於中位數 40% 必須改判配菜。"""
        menu = build_menu([
            {"name": "牛肉麵", "price": 150},
            {"name": "豬排飯", "price": 120},
            {"name": "雞腿飯", "price": 100},
            {"name": "加飯", "price": 20},  # 中位數 110，門檻 44
        ])
        result = run_recommend(menu)

        self.assertIn("加飯", names_by_reason(result, "搭配配菜"))
        self.assertNotIn("加飯", names_by_reason(result, "主餐推薦"))

    def test_addon_detection_requires_at_least_three_priced_items(self):
        """少於 3 個有價格的品項就不做加料判定，「加飯」仍是主食。"""
        menu = build_menu([
            {"name": "牛肉麵", "price": 150},
            {"name": "加飯", "price": 20},
        ])
        result = run_recommend(menu)

        self.assertIn("加飯", names_by_reason(result, "主餐推薦"))
        self.assertEqual([], names_by_reason(result, "搭配配菜"))

    # --- 預算分配 ---

    def test_budget_uses_whole_meal_total_instead_of_arbitrary_main_ratio(self):
        """能放進含服務費總預算的主食，不應被固定 40% 比例排除。"""
        menu = build_menu([
            {"name": "牛肉麵", "price": 50},
            {"name": "陽春麵", "price": 45},
        ])
        result = run_recommend(menu, {"budget": 100})

        self.assertEqual(["陽春麵"], names_by_reason(result, "主餐推薦"))
        self.assertLessEqual(result["meta"]["estimatedTotal"], 100)

    def test_people_can_request_two_mains_when_total_stays_in_budget(self):
        menu = build_menu([
            {"name": "陽春麵", "price": 60},
            {"name": "牛肉麵", "price": 100},
        ])
        result = run_recommend(menu, {"budget": 200, "people": 4})

        self.assertEqual(["陽春麵", "牛肉麵"], names_by_reason(result, "主餐推薦"))
        self.assertLessEqual(result["meta"]["estimatedTotal"], 200)

    def test_side_is_skipped_when_it_would_pass_90_percent_of_budget(self):
        """預算 100、主食 40，配菜會讓總額破 90 就一個都不加。"""
        menu = build_menu([
            {"name": "陽春麵", "price": 40},
            {"name": "薯條", "price": 55},
            {"name": "雞塊", "price": 60},
        ])
        result = run_recommend(menu, {"budget": 100})

        self.assertEqual(["陽春麵"], names_by_reason(result, "主餐推薦"))
        self.assertEqual([], names_by_reason(result, "搭配配菜"))

    # --- 數量上限 ---

    def test_at_most_two_mains(self):
        menu = build_menu([
            {"name": "陽春麵", "price": 100},
            {"name": "牛肉麵", "price": 110},
            {"name": "豬排麵", "price": 120},
            {"name": "雞腿飯", "price": 130},
        ])
        result = run_recommend(menu, {"people": 4})

        self.assertEqual(2, len(names_by_reason(result, "主餐推薦")))

    def test_at_most_one_side(self):
        menu = build_menu([
            {"name": "陽春麵", "price": 100},
            {"name": "薯條", "price": 30},
            {"name": "雞塊", "price": 35},
            {"name": "沙拉", "price": 40},
        ])
        result = run_recommend(menu)

        self.assertEqual(1, len(names_by_reason(result, "搭配配菜")))

    # --- 飲料 ---

    def test_need_drink_false_skips_drinks(self):
        menu = build_menu([
            {"name": "陽春麵", "price": 100},
            {"name": "紅茶", "price": 30},
        ])
        result = run_recommend(menu, {"needDrink": False})

        self.assertEqual([], names_by_reason(result, "搭配飲品"))

    def test_drinks_included_by_default(self):
        menu = build_menu([
            {"name": "陽春麵", "price": 100},
            {"name": "紅茶", "price": 30},
        ])
        result = run_recommend(menu)

        self.assertEqual(["紅茶"], names_by_reason(result, "搭配飲品"))

    # --- 套餐組合：換成只推主餐的引擎時，這條會第一個炸 ---

    def test_returns_a_combo_not_just_a_main_dish(self):
        menu = build_menu([
            {"name": "陽春麵", "price": 100},
            {"name": "薯條", "price": 30},
            {"name": "紅茶", "price": 25},
        ])
        result = run_recommend(menu)

        self.assertEqual(
            {"主餐推薦", "搭配配菜", "搭配飲品"},
            set(reasons(result)),
            "recommend() 應該組出一整餐，不是只回主餐",
        )

    # --- 排除條件 ---

    def test_excludes_keyword_removes_matching_items(self):
        menu = build_menu([
            {"name": "牛肉麵", "price": 150},
            {"name": "陽春麵", "price": 100},
        ])
        result = run_recommend(menu, {"excludes": ["牛"]})

        picked = [item["name"] for item in result["items"]]
        self.assertNotIn("牛肉麵", picked)
        self.assertIn("陽春麵", picked)

    def test_spice_level_not_spicy_excludes_spicy_items(self):
        menu = build_menu([
            {"name": "辣子雞飯", "price": 120},
            {"name": "陽春麵", "price": 100},
        ])
        result = run_recommend(menu, {"spiceLevel": "不辣"})

        picked = [item["name"] for item in result["items"]]
        self.assertNotIn("辣子雞飯", picked)
        self.assertIn("陽春麵", picked)

    # --- 邊界 ---

    def test_empty_menu_returns_explanatory_note(self):
        result = run_recommend({"restaurants": {}})

        self.assertEqual([], result["items"])
        self.assertEqual("菜單中沒有找到任何菜品", result["notes"])

    def test_meta_echoes_parsed_budget(self):
        menu = build_menu([{"name": "陽春麵", "price": 100}])
        result = run_recommend(menu, {"budget": "250", "people": 2})

        self.assertEqual(250.0, result["meta"]["budget"])
        self.assertEqual(2, result["meta"]["people"])

    # --- 新推薦契約：這三條在舊引擎上會失敗 ---

    def test_top_k_is_a_hard_output_limit(self):
        menu = build_menu([
            {"name": "牛肉麵", "price": 100},
            {"name": "雞腿飯", "price": 110},
            {"name": "薯條", "price": 30},
            {"name": "紅茶", "price": 20},
        ])
        result = run_recommend(menu, top_k=1)
        self.assertLessEqual(len(result["items"]), 1)

    def test_budget_is_never_violated_by_fallback(self):
        menu = build_menu([{"name": "牛肉麵", "price": 100}])
        result = run_recommend(menu, {"budget": 50})
        total = sum(item["price"] for item in result["items"] if item["price"] is not None)
        self.assertLessEqual(total, 50)
        self.assertEqual([], result["items"])

    def test_people_changes_required_main_course_count(self):
        menu = build_menu([
            {"name": "主餐牛肉麵", "price": 100},
            {"name": "主餐雞腿飯", "price": 110},
            {"name": "主餐豬排飯", "price": 120},
            {"name": "主餐魚排飯", "price": 130},
            {"name": "主餐排骨飯", "price": 140},
        ])
        one_person = run_recommend(menu, {"people": 1}, top_k=5)
        eight_people = run_recommend(menu, {"people": 8}, top_k=5)
        self.assertLess(len(one_person["items"]), len(eight_people["items"]))


if __name__ == "__main__":
    unittest.main()
