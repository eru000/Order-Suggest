import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]

from conversation_service import ConversationService
from decision_service import DecisionService
from session_store import SessionStore
from test_decision_improvements import evidence_menu


def dishes(*items):
    return {"categories": [{"name": "主餐", "items": [
        {"name": name, "price": price} for name, price in items
    ]}]}


class ReviewFixesTest(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionStore()
        self.menus = {"測試": dishes(("鮮蝦炒飯", 50), ("豬肉飯", 60), ("雞腿飯", 90))}
        self.chat = ConversationService(self.sessions, self.menus, "測試", lambda *args: None)
        self.sid = "review_fixes_session"

    def say(self, text):
        view = self.chat.discovery(self.sid, text)
        self.assertIsNotNone(view)
        return view

    def test_cheaper_and_allergy_are_applied_together_before_selecting(self):
        self.say("想吃雞，幫我決定")
        view = self.say("太貴了，我對蝦過敏")
        self.assertEqual([i["name"] for i in view["items"]], ["豬肉飯"])
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertIn("shellfish", prefs["allergens"])
        self.assertIn("shellfish", self.sessions.get(self.sid).prefs["allergens"])
        self.assertLess(view["items"][0]["total"], 90)

    def test_cheaper_also_applies_budget_and_dislikes_once(self):
        self.say("想吃雞，幫我決定")
        view = self.say("便宜一點，不吃蝦，預算55元")
        self.assertEqual(view["type"], "no_match")
        prefs = self.sessions.get(self.sid).decision["prefs"]
        self.assertEqual(prefs["budget"], 55)
        self.assertIn("蝦", prefs["excludes"])

    def test_per_person_budget_is_converted_once_in_a_compound_action(self):
        self.say("想吃雞，幫我決定")
        self.say("便宜一點，2人預算120元")
        self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["budget"], 60)

    def test_rejection_keeps_the_requested_direction_in_both_directions(self):
        for text, wanted in (
            ("這些太清淡，想吃重口味，換一批", "rich"),
            ("想吃重口味，還是改清淡一點，換一批", "light"),
            ("這些太清淡，換一批", "rich"),
        ):
            with self.subTest(text=text):
                self.menus["測試"] = evidence_menu()
                self.say("幫我決定")
                self.say(text)
                self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["taste"], wanted)
                self.sessions = SessionStore()
                self.chat = ConversationService(self.sessions, self.menus, "測試", lambda *a: None)

    def test_smaller_portion_feedback_is_not_changed_to_larger(self):
        self.menus["測試"] = evidence_menu()
        self.say("幫我決定")
        self.say("份量太多，想吃小份一點，換一批")
        self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["portion"], "small")

    def test_unknown_soft_preferences_get_a_question_about_known_differences(self):
        self.menus["測試"] = dishes(
            ("牛排", 300), ("沙朗牛排", 300), ("雞排", 300), ("魚排", 300),
        )
        self.say("幫我決定")
        view = self.say("很餓，想吃清爽一點")
        self.assertEqual(view["type"], "question")
        self.assertEqual(view["question"]["field"], "protein")
        self.assertTrue(view.get("dataNote"))
        view = self.chat.decisions.handle(self.sid, action="answer", value="雞肉",
                                         revision=view["revision"])
        self.assertEqual([i["name"] for i in view["items"]], ["雞排"])
        self.assertIn("未標示份量", str(view["items"][0]["warnings"]))

    def test_unknown_data_comparison_refines_without_confirming_or_looping(self):
        self.menus["測試"] = dishes(("沙朗牛排", 300), ("菲力牛排", 300), ("翼板牛排", 300))
        self.say("幫我決定")
        view = self.say("想吃大份一點")
        self.assertEqual(view["type"], "question")
        self.assertEqual(view["question"]["field"], "comparison")
        option = view["question"]["options"][1]
        view = self.chat.decisions.handle(self.sid, action="answer", value=option["value"],
                                         revision=view["revision"])
        self.assertEqual(view["type"], "recommendation")
        self.assertEqual(view["items"][0]["name"], option["label"])
        self.assertTrue(view["focused"])
        self.assertTrue(view.get("dataNote"))
        self.assertLessEqual(view["questionCount"], 2)

    def test_explicit_decide_does_not_force_an_extra_question_for_unknown_data(self):
        view = self.say("很餓，想吃清爽一點，幫我決定")
        self.assertEqual(view["type"], "recommendation")
        self.assertEqual(len(view["items"]), 1)
        self.assertTrue(view.get("dataNote"))
        self.assertNotIn("菜單標示大份", view["items"][0]["reason"])

    def test_information_gap_does_not_exceed_two_questions(self):
        self.menus["測試"] = dishes(
            ("牛排", 100), ("沙朗牛排", 150), ("雞排", 200), ("魚排", 250),
            ("豬排", 300), ("雞腿排", 350),
        )
        view = self.say("不知道吃什麼，很餓")
        while view["type"] == "question":
            self.assertLessEqual(view["questionCount"], 2)
            view = self.chat.decisions.handle(self.sid, action="answer", value="skip",
                                             revision=view["revision"])
        view = self.say("想吃清爽一點")
        self.assertEqual(view["type"], "recommendation")
        self.assertLessEqual(view["questionCount"], 2)
        self.assertTrue(view.get("dataNote"))

    def test_protein_refinement_can_be_changed_and_relaxed(self):
        self.menus["測試"] = dishes(("雞排", 100), ("牛排", 100), ("魚排", 100))
        self.say("幫我決定")
        view = self.say("很餓")
        view = self.chat.decisions.handle(self.sid, action="answer", value="雞肉",
                                         revision=view["revision"])
        view = self.say("改成牛肉")
        self.assertEqual(view["items"][0]["name"], "牛排")
        view = self.say("預算50元")
        self.assertEqual(view["type"], "no_match")
        self.assertTrue(view["canRelaxType"])
        self.chat.decisions.handle(self.sid, action="relax_type", revision=view["revision"])
        self.assertNotIn("protein", self.sessions.get(self.sid).decision["prefs"])

    def test_canceling_budget_does_not_count_as_an_extra_question(self):
        self.say("幫我決定")
        state = self.sessions.get(self.sid)
        state.decision["asked"] = ["protein", "texture"]
        view = self.say("取消預算")
        self.assertEqual(view["questionCount"], 2)

    def test_downgrading_a_portion_can_offer_the_same_dish_in_a_smaller_size(self):
        self.menus["測試"] = dishes(("雞肉飯（大份）", 90), ("雞肉飯（小份）", 60))
        self.say("想吃大份，幫我決定")
        view = self.say("這份太大份，換一批")
        self.assertEqual(view["items"][0]["name"], "雞肉飯（小份）")


if __name__ == "__main__":
    unittest.main()
