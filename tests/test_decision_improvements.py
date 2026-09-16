import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_service import ConversationService
from decision_catalog import menu_choices
from decision_service import DecisionService
from persistence import SQLiteSessionRepository
from session_store import SessionStore
from test_decision_service import menu


def evidence_menu():
    return {"categories": [{"name": "主餐", "items": [
        {"name": "酥炸雞腿飯", "price": 60, "tags": ["小份"]},
        {"name": "濃郁拌麵", "price": 65, "tags": ["小份"]},
        {"name": "清爽雞肉飯", "price": 90, "tags": ["大份"]},
        {"name": "少油乾麵", "price": 95, "tags": ["大份"]},
        {"name": "清淡水餃", "price": 100, "tags": ["大份"]},
        {"name": "雞肉湯麵", "price": 80},
    ]}]}


class DiscoveryImprovementTest(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionStore()
        self.menus = {"測試": evidence_menu()}
        self.service = DecisionService(self.sessions, self.menus, "測試")
        self.sid = "improvement_session"
        self.view = None

    def step(self, action="message", **kwargs):
        self.view = self.service.handle(
            self.sid, action=action, revision=self.view["revision"] if self.view else None, **kwargs
        )
        return self.view

    def test_vague_wishes_change_the_top_pick_with_menu_evidence(self):
        self.step("start")
        self.step("decide")
        before = self.view["items"][0]["name"]
        self.step(text="想吃清爽一點，今天很餓")
        self.assertEqual(self.view["items"][0]["name"], "清爽雞肉飯")
        self.assertNotEqual(before, self.view["items"][0]["name"])
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertEqual(prefs["taste"], "light")
        self.assertEqual(prefs["portion"], "large")
        self.assertNotIn("清爽一點", prefs.get("likes", []))
        self.assertIn("大份", self.view["items"][0]["reason"])

    def test_less_oily_is_not_a_literal_item_exclusion(self):
        self.step("start", text="不要太油")
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertEqual(prefs["taste"], "light")
        self.assertNotIn("太油", prefs.get("excludes", []))
        self.assertNotIn("excludes", self.sessions.get(self.sid).prefs)

    def test_negated_wishes_are_not_interpreted_as_positive_ones(self):
        self.step("start", text="不想吃清淡的，不要大份")
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertEqual(prefs["taste"], "rich")
        self.assertEqual(prefs["portion"], "small")

    def test_new_allergy_in_same_message_prevents_confirming_a_conflicting_dish(self):
        self.menus["測試"] = {"categories": [{"name": "主餐", "items": [
            {"name": "鮮蝦炒飯", "price": 50}, {"name": "雞肉飯", "price": 70},
        ]}]}
        self.step("start", text="幫我決定")
        self.assertIn("蝦", self.view["items"][0]["name"])
        self.step(text="就吃第一個，我對蝦過敏")
        self.assertEqual(self.view["type"], "recommendation")
        self.assertTrue(all("蝦" not in i["name"] for i in self.view["items"]))
        self.assertIn("shellfish", self.sessions.get(self.sid).prefs["allergens"])

    def test_unknown_menu_attributes_are_not_invented(self):
        self.menus["測試"] = {"categories": [{"name": "主餐", "items": [
            {"name": "雞肉湯麵", "price": 80},
        ]}]}
        self.step("start", text="很餓，想吃清爽一點")
        item = self.view["items"][0]
        self.assertIn("未標示份量", str(item["warnings"]))
        self.assertIn("未標示口味", str(item["warnings"]))
        self.assertNotIn("菜單標示大份", item["reason"])
        rows, _ = menu_choices(self.menus["測試"])
        self.assertIsNone(rows[0]["taste"])
        self.assertIsNone(rows[0]["portion"])

    def test_portion_feedback_can_upgrade_the_same_dish_size(self):
        self.menus["測試"] = {"categories": [{"name": "主餐", "items": [
            {"name": "雞肉飯（小份）", "price": 60},
            {"name": "雞肉飯（大份）", "price": 90},
        ]}]}
        self.step("start", text="幫我決定")
        self.assertIn("小份", self.view["items"][0]["name"])
        self.step("replace", item_id=self.view["items"][0]["id"], reason="portion")
        self.assertIn("大份", self.view["items"][0]["name"])

    def test_decide_gives_one_suggestion_until_explicit_confirmation(self):
        self.step("start", text="幫我決定，預算100元")
        self.assertEqual(self.view["type"], "recommendation")
        self.assertTrue(self.view["focused"])
        self.assertEqual(len(self.view["items"]), 1)
        first_id = self.view["items"][0]["id"]
        self.step("replace", item_id=first_id)
        self.assertEqual(len(self.view["items"]), 1)
        self.assertNotEqual(first_id, self.view["items"][0]["id"])
        self.step("choose", item_id=self.view["items"][0]["id"])
        self.assertEqual(self.view["type"], "selection")
        self.step("reconsider")
        self.assertFalse(self.view["focused"])
        self.assertGreater(len(self.view["items"]), 1)

    def test_batch_feedback_changes_direction_and_preserves_hard_constraints(self):
        self.step("start", text="預算110元，不吃牛")
        self.step("recommend")
        old_ids = {i["id"] for i in self.view["items"]}
        self.step("reject_all", reason="light")
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertEqual(prefs["taste"], "light")
        self.assertEqual(prefs["budget"], 110)
        self.assertIn("牛", prefs["excludes"])
        self.assertFalse(old_ids & {i["id"] for i in self.view["items"]})
        self.assertIn("菜單標示清爽", self.view["items"][0]["reason"])
        self.assertEqual(self.sessions.get(self.sid).decision["feedback"][-1]["reason"], "light")

    def test_batch_price_feedback_never_silently_relaxes_budget(self):
        self.step("start", text="預算100元")
        self.step("recommend")
        price = min(i["total"] for i in self.view["items"])
        self.step("reject_all", reason="price")
        self.assertTrue(all(i["total"] < price for i in self.view.get("items", [])))
        self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["budget"], 100)
        self.assertEqual(self.view["type"], "no_match")

    def test_fixed_restrictions_survive_new_meal_but_temporary_ones_do_not(self):
        self.menus["測試"] = menu()
        self.step("start", text="對蝦過敏，我不吃牛，預算200元")
        self.step(text="今天不吃雞，很餓")
        self.step("reset")
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertIn("shellfish", prefs["allergens"])
        self.assertIn("牛", prefs["excludes"])
        self.assertNotIn("雞", prefs["excludes"])
        self.assertNotIn("budget", prefs)
        self.assertNotIn("portion", prefs)
        self.step("recommend")
        self.assertTrue(all("蝦" not in i["name"] and "牛" not in i["name"]
                            for i in self.view["items"]))

    def test_temporary_dislike_can_be_promoted_to_fixed_even_without_ranking_change(self):
        self.step("start", text="今天不吃牛")
        self.assertTrue(self.service.accepts_message(self.sid, "我不吃牛"))
        self.step(text="我不吃牛")
        self.step("reset")
        self.assertIn("牛", self.sessions.get(self.sid).decision["prefs"]["excludes"])

    def test_dietary_restriction_and_explicit_removal_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteSessionRepository(Path(directory) / "sessions.db")
            self.sessions = SessionStore(repository=repo)
            self.service = DecisionService(self.sessions, self.menus, "測試")
            self.step("start", text="我吃素，對蝦過敏")
            restored = DecisionService(SessionStore(repository=repo), self.menus, "測試")
            view = restored.handle(self.sid, action="reset", revision=self.view["revision"])
            prefs = restored.sessions.get(self.sid).decision["prefs"]
            self.assertIn("vegetarian", prefs["dietaryRestrictions"])
            self.assertIn("shellfish", prefs["allergens"])
            view = restored.handle(self.sid, text="取消蝦過敏", revision=view["revision"])
            restored.handle(self.sid, action="reset", revision=view["revision"])
            self.assertNotIn("shellfish", restored.sessions.get(self.sid).prefs.get("allergens", []))

    def test_useful_question_changes_with_remaining_candidates(self):
        self.menus["測試"] = menu()
        self.step("start")
        self.assertEqual(self.view["question"]["field"], "dishType")
        self.step("answer", value="麵")
        self.assertNotEqual(self.view["question"]["field"], "dishType")
        self.step("answer", value="skip")
        self.assertEqual(self.view["type"], "recommendation")
        self.assertLessEqual(self.view["questionCount"], 2)

    def test_measurement_records_decision_interactions_without_preference_text(self):
        with patch("decision_service.emit") as event:
            self.step("start", text="我對蝦過敏")
            self.step("decide")
            self.step("choose", item_id=self.view["items"][0]["id"])
        payload = event.call_args.kwargs
        self.assertEqual(payload["phase"], "selection")
        self.assertEqual(payload["interactions"], 3)
        self.assertGreaterEqual(payload["elapsedSeconds"], 0)
        self.assertNotIn("蝦", str(payload))

    def test_polite_change_routes_to_decision_but_information_stays_with_ai(self):
        self.step("start")
        for text in ("可以幫我換成麵嗎？", "能不能改成飯？", "幫我決定一道"):
            with self.subTest(text=text):
                self.assertTrue(self.service.accepts_message(self.sid, text))
        snapshot = copy.deepcopy(self.sessions.get(self.sid).decision)
        for text in (
            "這個會辣嗎？", "有沒有不辣的？", "為什麼幫我換成麵？", "如果改成麵呢？",
            "不要幫我決定", "不要選第一個", "這個會不會太辣", "這個會不會過敏",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.service.accepts_message(self.sid, text))
        self.assertEqual(snapshot, self.sessions.get(self.sid).decision)

    def test_mixed_change_and_question_updates_cards_and_answers_with_new_context(self):
        self.step("start")
        service = ConversationService(self.sessions, self.menus, "測試", lambda *args: None)
        with patch("ollama_fuc.chat", return_value="辣度未標示，需要向店家確認。") as model:
            view = service.discovery(self.sid, "可以幫我換成麵嗎？這個會辣嗎？")
        self.assertTrue(all(i["dishType"] == "麵" for i in view["items"]))
        self.assertIn("辣度未標示", view["answer"])
        self.assertIn(view["items"][0]["name"], str(model.call_args.args[0]))
        self.assertEqual(service.decisions.current(self.sid), view)
        self.assertEqual(self.sessions.get(self.sid).history[-2]["content"],
                         "可以幫我換成麵嗎？這個會辣嗎？")


class MenuWordingTest(unittest.TestCase):
    """店家寫在括號裡的註記，跟寫在品名裡的是同一件事，不該漏掉。"""

    def rows(self, *names):
        return {
            row["name"]: row
            for row in menu_choices({"categories": [{"name": "主餐", "items": [
                {"name": name, "price": 60} for name in names
            ]}]})[0]
        }

    def test_bracketed_texture_and_portion_are_read(self):
        rows = self.rows("香排骨麵(乾)", "鴨肉飯(大)", "鴨肉飯(小)", "湯麵（湯）")
        self.assertEqual(rows["香排骨麵(乾)"]["texture"], "乾的")
        self.assertEqual(rows["湯麵（湯）"]["texture"], "湯的")
        self.assertEqual(rows["鴨肉飯(大)"]["portion"], "large")
        self.assertEqual(rows["鴨肉飯(小)"]["portion"], "small")

    def test_bracketed_note_that_is_not_a_size_stays_unknown(self):
        rows = self.rows("麻辣拌麵(大辣)", "咖哩飯(小辣)", "牛肉麵(加蛋)")
        for name in rows:
            self.assertIsNone(rows[name]["portion"], name)


class DiversityTest(unittest.TestCase):
    """同一道菜的修飾版不該佔滿三個推薦位。"""

    def setUp(self):
        self.sessions = SessionStore()
        self.menus = {"測試": {"categories": [{"name": "排餐", "items": [
            {"name": "Prime霜降牛小排", "price": None},
            {"name": "Prime玫瑰霜降牛小排", "price": None},
            {"name": "Prime霜降牛小排切厚切", "price": None},
            {"name": "原木煙燻牛小排", "price": None},
            {"name": "果香壺漬牛小排", "price": None},
        ]}]}}
        self.service = DecisionService(self.sessions, self.menus, "測試")
        self.sid = "diversity_session"

    def test_variants_of_one_dish_do_not_fill_every_slot(self):
        view = self.service.handle(self.sid, action="start")
        if view["type"] == "question":
            view = self.service.handle(
                self.sid, action="recommend", revision=view["revision"]
            )
        names = [item["name"] for item in view["items"]]
        self.assertEqual(len(names), 3)
        self.assertEqual(len([n for n in names if "霜降牛小排" in n]), 1, names)

    def test_unpriced_menu_says_so_instead_of_blaming_the_conditions(self):
        view = self.service.handle(self.sid, action="start", text="預算 500 元以內")
        if view["type"] == "question":
            view = self.service.handle(
                self.sid, action="recommend", revision=view["revision"]
            )
        self.assertEqual(view["type"], "no_match")
        self.assertIn("沒有標示價格", view["message"])
        self.assertTrue(view["canRelaxBudget"])


if __name__ == "__main__":
    unittest.main()
