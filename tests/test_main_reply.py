"""main.py 回覆組裝路徑的行為測試。

這條路徑之前完全沒有測試（main.py 覆蓋率 16%），但它是 LLM 掛掉時使用者實際
會看到的東西，而且全部是不打網路的純函式，沒有不測的理由。

這裡釘的是「現在實際會產生什麼」，不是主張這些文案是對的。
"""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import main  # noqa: E402


def rec(items, **meta):
    return {"items": items, "notes": "", "meta": meta}


def item(name, price, type_="main", category="主餐", reason="符合你的條件"):
    return {"name": name, "price": price, "category": category, "reason": reason, "type": type_}


class FallbackTemplateTest(unittest.TestCase):
    """_fallback_format：LLM 失敗時的備援文案。"""

    def test_no_items_asks_for_more_conditions(self):
        for empty in (rec([]), {"meta": {}}, {"items": "not-a-list"}):
            text = main._fallback_format(empty)
            self.assertIn("目前沒有很適合的選項", text)

    def test_groups_by_type_in_fixed_section_order(self):
        text = main._fallback_format(rec([
            item("烏龍茶", 30, "drink"),
            item("燙青菜", 40, "veggie"),
            item("酸菜白肉鍋", 380, "core"),
        ]))
        positions = [text.index(x) for x in ("核心主鍋", "時蔬解膩", "飲品")]
        self.assertEqual(positions, sorted(positions), "區段順序應為 core → veggie → drink")
        self.assertIn("【酸菜白肉鍋】", text)

    def test_service_fee_is_ten_percent_of_subtotal(self):
        text = main._fallback_format(rec([item("A", 100), item("B", 200)]))
        self.assertIn("餐點小計：約 $ 300", text)
        self.assertIn("10% 服務費：約 $ 30", text)
        self.assertIn("總計：約 $ 330", text)

    def test_budget_surplus_and_overrun_use_different_wording(self):
        under = main._fallback_format(rec([item("A", 100)], budget=1000))
        self.assertIn("離預算還有約 $ 890", under)

        over = main._fallback_format(rec([item("A", 1000)], budget=500))
        self.assertIn("超出預算 $ 600", over)

    def test_market_price_item_is_labelled_and_estimated_at_350(self):
        """時價品項會被當成 350 元計入小計，而畫面上只說「時價」。

        這個 350 是寫死在 price_text() 裡的，沒有任何地方對使用者說明。金額愈
        接近預算，這個隱形數字愈可能讓「離預算還有多少」算錯。
        """
        text = main._fallback_format(rec([item("時價海鮮", None)]))
        self.assertIn("價格為時價，可現場再確認", text)
        self.assertIn("餐點小計：約 $ 350", text)

    def test_intro_mentions_people_and_drinks(self):
        text = main._fallback_format(rec([item("A", 100)], people=5, needDrink=True))
        self.assertIn("5 位用餐", text)
        self.assertIn("含飲料", text)
        self.assertIn("多人聚餐", text)


class MenuPromptTest(unittest.TestCase):
    """_format_menu_for_prompt：讓 LLM 看得到候選以外的品項。

    沒有這段，被問到「有沒有飯類」時模型只看得到 recommend() 挑出的 5 項，
    會照它看到的東西回答「這家沒有飯」——但菜單上其實有。
    """

    @staticmethod
    def menu(items):
        return {"categories": [{"name": "飯類", "items": items}]}

    def test_lists_every_item_not_just_the_candidates(self):
        text = main._format_menu_for_prompt(self.menu([
            {"name": "雞腿飯", "price": 90},
            {"name": "排骨飯", "price": 85},
        ]))
        self.assertIn("完整菜單（這家店共 2 項）", text)
        self.assertIn("[飯類] 雞腿飯 $90、排骨飯 $85", text)

    def test_missing_price_is_marked_rather_than_dropped(self):
        text = main._format_menu_for_prompt(self.menu([{"name": "時價魚", "price": None}]))
        self.assertIn("時價魚（價格未標示）", text)

    def test_truncates_long_menus_and_says_so(self):
        many = [{"name": f"品項{i}", "price": 50} for i in range(main._MENU_PROMPT_MAX_ITEMS + 20)]
        text = main._format_menu_for_prompt(self.menu(many))
        self.assertIn(f"共 {main._MENU_PROMPT_MAX_ITEMS + 20} 項", text)
        self.assertIn(f"只列出前 {main._MENU_PROMPT_MAX_ITEMS} 項", text)
        self.assertNotIn(f"品項{main._MENU_PROMPT_MAX_ITEMS + 1} ", text)

    def test_non_dict_menu_is_ignored_quietly(self):
        self.assertEqual(main._format_menu_for_prompt(None), "")
        self.assertEqual(main._format_menu_for_prompt("菜單"), "")
        self.assertEqual(main._format_menu_for_prompt({}), "")


class ReplyFallbackTest(unittest.TestCase):
    """LLM 掛掉不能讓對話中斷。"""

    def _reply(self, chat_impl):
        buf = io.StringIO()
        with mock.patch.dict(sys.modules), mock.patch("sys.stdout", buf):
            import ollama_fuc
            with mock.patch.object(ollama_fuc, "chat", chat_impl):
                return main.generate_ai_reply(rec([item("A", 100)]), "隨便推薦")

    def test_falls_back_to_template_when_the_model_raises(self):
        def boom(*a, **k):
            raise RuntimeError("model API request failed: HTTP 500")

        self.assertIn("餐點小計", self._reply(boom))

    def test_falls_back_when_the_model_returns_only_whitespace(self):
        self.assertIn("餐點小計", self._reply(lambda *a, **k: "   \n  "))

    def test_uses_the_model_reply_when_there_is_one(self):
        self.assertEqual(self._reply(lambda *a, **k: "  推薦你點 A  "), "推薦你點 A")


class PrepareRecommendationTest(unittest.TestCase):
    """人數會改變要幾道菜——固定 5 項對四個人太少。"""

    def _prepare(self, user_input, prefs):
        captured = {}

        def fake_recommend(menu, prefs_, top_k=5, model=None):
            captured["top_k"] = top_k
            captured["prefs"] = prefs_
            return {"items": [], "meta": {}}

        history = []
        with mock.patch.object(main, "ollama_recommend", fake_recommend):
            main.prepare_recommendation(history, user_input, {"categories": []}, prefs)
        return captured, history

    def test_top_k_scales_with_people_and_is_capped(self):
        captured, _ = self._prepare("隨便", {})
        self.assertEqual(captured["top_k"], 5, "沒講人數就是 5")

        captured, _ = self._prepare("隨便", {"people": 3})
        self.assertEqual(captured["top_k"], 6)

        captured, _ = self._prepare("隨便", {"people": 20})
        self.assertEqual(captured["top_k"], 8, "上限 8，不會無限增加")

    def test_user_turn_is_recorded_and_preferences_merge(self):
        captured, history = self._prepare("我不吃辣", {})
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "我不吃辣")
        self.assertEqual(captured["prefs"].get("spiceLevel"), "不辣")

    def test_missing_recommender_raises_instead_of_returning_nothing(self):
        with mock.patch.object(main, "ollama_recommend", None):
            with self.assertRaisesRegex(RuntimeError, "推薦功能未載入"):
                main.prepare_recommendation([], "隨便", {"categories": []}, {})


if __name__ == "__main__":
    unittest.main()
