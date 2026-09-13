import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_service import ConversationService
from decision_catalog import menu_choices
from decision_service import DecisionService, StaleDecision
from persistence import SQLiteSessionRepository
from session_store import SessionStore
from storage.database import Database
from storage.repositories import SQLAlchemySessionRepository


def menu():
    return {
        "categories": [
            {
                "name": "主餐",
                "items": [
                    {"name": "滷肉飯", "price": 50},
                    {"name": "雞肉飯", "price": 70},
                    {"name": "雞腿飯", "price": 100},
                    {"name": "牛肉炒飯", "price": 130},
                    {"name": "麻辣乾麵", "price": 80},
                    {"name": "香菇湯麵", "price": 90},
                    {"name": "雞絲乾麵", "price": 110},
                    {"name": "鮮蝦湯麵", "price": 140},
                    {"name": "豬肉水餃", "price": 65},
                    {"name": "牛肉水餃", "price": 85},
                ],
            },
            {"name": "飲料", "items": [{"name": "紅茶", "price": 20}]},
            {"name": "小菜", "items": [{"name": "燙青菜", "price": 40}]},
        ]
    }


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionStore()
        self.menus = {"測試餐廳": menu(), "另一家": menu()}
        self.service = DecisionService(self.sessions, self.menus, "測試餐廳")
        self.sid = "decision_test_session"
        self.view = None

    def step(self, action="message", **kwargs):
        self.view = self.service.handle(
            self.sid,
            action=action,
            revision=self.view["revision"] if self.view else None,
            **kwargs,
        )
        return self.view

    def ready(self, text=""):
        self.step("start", text=text)
        if self.view["type"] == "question":
            self.step("recommend")
        return self.view

    def test_skipping_at_most_two_questions_then_choose(self):
        self.step("start")
        count = 0
        while self.view["type"] == "question":
            count += 1
            self.assertLessEqual(count, 2)
            self.step("answer", value="skip")
        self.assertEqual(self.view["type"], "recommendation")
        self.assertEqual(len(self.view["items"]), 3)
        chosen = self.view["items"][1]
        self.step("choose", item_id=chosen["id"])
        self.assertEqual(self.view["type"], "selection")
        self.assertEqual(self.view["items"], [chosen])
        self.assertEqual(self.service.current(self.sid), self.view)

    def test_complete_preferences_skip_questions_and_keep_constraints(self):
        self.step("start", text="想吃飯，預算100元，不吃牛")
        self.assertEqual(self.view["type"], "recommendation")
        self.assertEqual(self.view["questionCount"], 0)
        self.assertTrue(self.view["items"])
        for item in self.view["items"]:
            self.assertIn("飯", item["name"])
            self.assertNotIn("牛", item["name"])
            self.assertLessEqual(item["total"], 100)

    def test_text_budget_answer_and_do_not_ask_known_budget(self):
        self.step("start", text="預算200元")
        self.assertEqual(self.view["question"]["field"], "dishType")
        self.step(text="第一個")
        self.assertNotEqual((self.view.get("question") or {}).get("field"), "budget")

    def test_no_meaningless_rice_question_in_noodle_only_menu(self):
        self.menus["測試餐廳"]["categories"] = [
            {
                "name": "麵",
                "items": [
                    {"name": name + "麵", "price": 80}
                    for name in ["雞肉", "牛肉", "豬肉", "香菇", "鮮蝦"]
                ],
            }
        ]
        self.step("start")
        self.assertEqual(self.view["type"], "question")
        self.assertEqual(self.view["question"]["field"], "protein")
        self.assertNotIn("飯", [o["label"] for o in self.view["question"]["options"]])

    def test_unknown_answer_does_not_consume_second_question(self):
        self.step("start")
        self.step("answer", value="skip")
        self.assertEqual(self.view["type"], "question")
        field = self.view["question"]["field"]
        self.step(text="心情是藍色的")
        self.assertEqual(self.view["type"], "question")
        self.assertEqual(field, self.view["question"]["field"])
        self.step(text="都可以")
        self.assertEqual(self.view["type"], "recommendation")

    def test_replace_one_preserves_others_and_avoids_rejection(self):
        self.ready("預算200元，不吃牛，不吃辣")
        original = self.view["items"]
        rejected = original[0]["id"]
        self.step("replace", item_id=rejected)
        ids = {item["id"] for item in self.view["items"]}
        self.assertNotIn(rejected, ids)
        self.assertTrue({r["id"] for r in original[1:]}.issubset(ids))
        for item in self.view["items"]:
            self.assertNotIn("牛", item["name"])
            self.assertNotIn("麻辣", item["name"])
            self.assertLessEqual(item["total"], 200)

    def test_reject_all_eventually_exhausts_and_review_is_explicit(self):
        self.ready()
        seen = set()
        for _ in range(10):
            if self.view["type"] == "no_match":
                break
            ids = {row["id"] for row in self.view["items"]}
            self.assertFalse(seen & ids)
            seen |= ids
            self.step("reject_all")
        self.assertEqual(self.view["type"], "no_match")
        self.assertTrue(self.view["exhausted"])
        self.step("review")
        self.assertEqual(self.view["type"], "recommendation")

    def test_price_rejection_uses_lower_price_keeps_other_constraints(self):
        self.ready("想吃麵，預算200元，不吃牛")
        expensive = max(self.view["items"], key=lambda row: row["total"])
        self.step("replace", item_id=expensive["id"], reason="price")
        self.assertTrue(self.view["items"])
        for row in self.view["items"]:
            self.assertLess(row["total"], expensive["total"])
            self.assertIn("麵", row["name"])
            self.assertNotIn("牛", row["name"])

    def test_type_rejection_only_changes_this_meal(self):
        self.ready("預算200元")
        target = self.view["items"][0]
        self.step("replace", item_id=target["id"], reason="type")
        self.assertTrue(all(r["dishType"] != target["dishType"] for r in self.view["items"]))
        self.assertEqual(self.sessions.get(self.sid).prefs, {})
        self.step("reset")
        self.assertNotIn("rejectedTypes", self.sessions.get(self.sid).decision["prefs"])

    def test_no_match_does_not_silently_raise_budget(self):
        self.step("start", text="預算10元，不吃牛")
        self.assertEqual(self.view["type"], "no_match")
        self.assertTrue(self.view["canRelaxBudget"])
        self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["budget"], 10)
        self.step("relax_budget")
        self.assertTrue(self.view["items"])
        self.assertTrue(all("牛" not in r["name"] for r in self.view["items"]))

    def test_free_text_changes_type_and_budget_after_results(self):
        self.ready("想吃飯，預算200元")
        self.step(text="改吃麵，預算120元")
        self.assertTrue(self.view["items"])
        for row in self.view["items"]:
            self.assertEqual(row["dishType"], "麵")
            self.assertLessEqual(row["total"], 120)
        self.step(text="就吃第二個")
        self.assertEqual(self.view["type"], "selection")

    def test_old_or_foreign_actions_and_menu_changes_cannot_select(self):
        self.ready()
        snapshot = copy.deepcopy(self.view)
        self.step("reject_all")
        with self.assertRaises(StaleDecision):
            self.service.handle(
                self.sid,
                action="choose",
                revision=snapshot["revision"],
                item_id=snapshot["items"][0]["id"],
            )
        with self.assertRaises(StaleDecision):
            self.service.handle(
                "another_session", action="answer", revision=self.view["revision"], value="skip"
            )
        self.menus["測試餐廳"]["categories"][0]["items"][0]["price"] = 900
        with self.assertRaises(StaleDecision):
            self.step("choose", item_id=self.view["items"][0]["id"])
        self.assertIsNone(self.service.current(self.sid))

    def test_unknown_prices_and_service_fee_not_invented(self):
        self.menus["測試餐廳"] = {
            "categories": [
                {
                    "name": "主餐",
                    "items": [
                        {"name": "時價魚排飯", "price": None},
                        {"name": "雞肉飯", "price": 100},
                    ],
                }
            ]
        }
        self.ready("預算105元")
        self.assertEqual(len(self.view["items"]), 1)
        self.assertEqual(self.view["items"][0]["total"], 100)
        self.assertIsNone(self.view["items"][0]["serviceRate"])
        self.menus["測試餐廳"]["serviceRate"] = 0.1
        self.view = None
        self.step("start", text="預算105元")
        self.assertEqual(self.view["type"], "no_match")

    def test_identified_allergens_excluded_unknowns_labelled(self):
        self.ready("對蝦過敏，預算200元")
        for row in self.view["items"]:
            self.assertNotIn("蝦", row["name"])
            self.assertTrue(row["warnings"])

    def test_no_drinks_or_sides_as_main(self):
        rows, _ = menu_choices(menu())
        self.assertNotIn("紅茶", [r["name"] for r in rows])
        self.assertNotIn("燙青菜", [r["name"] for r in rows])

    def test_persistence_in_both_repository_implementations(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database("sqlite:///" + str(Path(directory) / "v2.db"), create_schema=True)
            for repository in (
                SQLiteSessionRepository(Path(directory) / "v1.db"),
                SQLAlchemySessionRepository(db),
            ):
                with self.subTest(repository=type(repository).__name__):
                    first = DecisionService(
                        SessionStore(repository=repository), self.menus, "測試餐廳"
                    )
                    view = first.handle(self.sid, action="start")
                    second = DecisionService(
                        SessionStore(repository=repository), self.menus, "測試餐廳"
                    )
                    self.assertEqual(view, second.current(self.sid))
                    view = second.handle(self.sid, action="recommend", revision=view["revision"])
                    view = second.handle(
                        self.sid,
                        action="choose",
                        revision=view["revision"],
                        item_id=view["items"][0]["id"],
                    )
                    third = DecisionService(
                        SessionStore(repository=repository), self.menus, "測試餐廳"
                    )
                    self.assertEqual(view, third.current(self.sid))
                    self.assertNotIn("_meal_decision", repository.load(self.sid)["prefs"])
            db.engine.dispose()

    def test_stream_and_sync_discovery_work_without_model(self):
        conversations = ConversationService(
            self.sessions, self.menus, "測試餐廳", lambda *args: None
        )
        with patch(
            "conversation_service.generate_conversation", side_effect=AssertionError("model called")
        ):
            reply = conversations.chat(self.sid, "不知道吃什麼")
            self.assertTrue(reply)
        events = list(conversations.stream(self.sid, "直接推薦"))
        self.assertEqual(events[0]["type"], "decision")
        self.assertEqual(events[0]["decision"]["type"], "recommendation")

    def test_question_routing_does_not_change_selection_or_preferences(self):
        self.ready("預算100元")
        state = self.sessions.get(self.sid)
        original = copy.deepcopy(state.decision)
        for text in (
            "這個會辣嗎？", "有沒有不辣的？", "為什麼幫我選這個？",
            "有沒有便宜一點的？", "這道飯多少錢", "介紹一下這道餐點",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.service.accepts_message(self.sid, text))
                self.assertEqual(state.decision, original)
        for text in ("預算80元", "我不吃辣", "換一批", "就吃第一個"):
            with self.subTest(text=text):
                self.assertTrue(self.service.accepts_message(self.sid, text))
                self.assertEqual(state.decision, original)


if __name__ == "__main__":
    unittest.main()
