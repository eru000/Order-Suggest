import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import back
from conversation_service import ConversationService
from session_store import SessionStore
from test_decision_service import menu


class DecisionAPITest(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionStore()
        self.menus = {"測試": menu(), "第二家": menu()}
        service = ConversationService(self.sessions, self.menus, "測試", lambda *args: None)
        self.patches = [
            patch.object(back, "CONVERSATIONS", service),
            patch.object(back, "SESSIONS", self.sessions),
            patch.object(back, "RESTAURANT_MENUS", self.menus),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(back.app)
        self.addCleanup(self.client.close)
        self.sid = "api_decision_session"

    def post(self, **kwargs):
        return self.client.post("/api/decision", json={"sessionId": self.sid, **kwargs})

    def test_real_http_start_answer_choose_restore_and_stale(self):
        first = self.post(action="start").json()["decision"]
        view = self.post(action="answer", value="skip", revision=first["revision"]).json()["decision"]
        view = self.post(action="recommend", revision=view["revision"]).json()["decision"]
        response = self.post(action="choose", itemId=view["items"][0]["id"], revision=view["revision"])
        self.assertEqual(response.status_code, 200)
        chosen = response.json()["decision"]
        self.assertEqual(chosen["type"], "selection")
        current = self.client.get("/api/decision", params={"session_id": self.sid})
        self.assertEqual(current.json()["decision"], chosen)
        self.assertEqual(current.headers["cache-control"], "no-store")
        stale = self.post(action="answer", value="skip", revision=first["revision"])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["decision"], chosen)

    def test_session_and_action_validation(self):
        bad = self.client.post("/api/decision", json={"sessionId": "../bad", "action": "start"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.post(action="invent").status_code, 422)
        self.assertEqual(self.post(action="choose", itemId="invented").status_code, 409)

    def test_switch_clears_old_decision_and_rejects_old_buttons(self):
        view = self.post(action="start").json()["decision"]
        response = self.client.post("/api/switch-restaurant", params={
            "restaurant_name": "第二家", "session_id": self.sid,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.get("/api/decision", params={"session_id": self.sid}).json()["decision"])
        self.assertEqual(self.post(action="recommend", revision=view["revision"]).status_code, 409)

    def test_sync_and_stream_transport_include_structured_discovery(self):
        sync = self.client.post("/api/chat", json={"sessionId": self.sid, "text": "不知道吃什麼"})
        self.assertEqual(sync.status_code, 200)
        self.assertEqual(sync.json()["decision"]["type"], "question")
        stream = self.client.post("/api/chat/stream", json={"sessionId": self.sid, "text": "直接推薦"})
        self.assertIn('"type": "decision"', stream.text)
        self.assertIn('"type": "recommendation"', stream.text)
        self.assertIn("[DONE]", stream.text)

    def test_missing_menu_has_helpful_recovery(self):
        self.menus.clear()
        response = self.post(action="start")
        self.assertEqual(response.status_code, 409)
        self.assertIn("菜單", response.json()["detail"])

    def test_questions_after_interactive_order_reach_ai_and_preserve_cards(self):
        view = self.post(action="start").json()["decision"]
        for phase in ("question", "recommendation", "selection"):
            if phase == "recommendation":
                view = self.post(action="recommend", revision=view["revision"]).json()["decision"]
            elif phase == "selection":
                view = self.post(action="choose", itemId=view["items"][0]["id"],
                                 revision=view["revision"]).json()["decision"]
            for endpoint in ("/api/chat", "/api/chat/stream"):
                with self.subTest(phase=phase, endpoint=endpoint), \
                        patch("ollama_fuc.chat", return_value="可以搭配紅茶。") as sync, \
                        patch("ollama_fuc.chat_stream", return_value=iter(["可以搭配紅茶。"])) as stream:
                    response = self.client.post(endpoint, json={
                        "sessionId": self.sid, "text": "這個可以搭配什麼飲料？",
                    })
                    self.assertEqual(response.status_code, 200)
                    model = stream if endpoint.endswith("stream") else sync
                    model.assert_called_once()
                    self.assertIn("可以搭配紅茶", response.text)
                    prompt = str(model.call_args.args[0])
                    self.assertIn("紅茶", prompt)
                    if view.get("items"):
                        self.assertIn(view["items"][0]["name"], prompt)
                    current = self.client.get("/api/decision", params={"session_id": self.sid})
                    self.assertEqual(current.json()["decision"], view)

        # Asking a question must not invalidate the selected card's buttons.
        self.assertEqual(self.post(action="reconsider", revision=view["revision"]).status_code, 200)

    def test_ai_failure_keeps_selection_and_allows_retry(self):
        view = self.post(action="start").json()["decision"]
        for endpoint, model in (("/api/chat", "chat"), ("/api/chat/stream", "chat_stream")):
            with self.subTest(endpoint=endpoint), patch(
                "ollama_fuc." + model, side_effect=RuntimeError("offline")
            ):
                response = self.client.post(endpoint, json={
                    "sessionId": self.sid, "text": "這個是什麼？",
                })
                self.assertIn("AI 暫時無法回答", response.text)
                self.assertEqual(self.sessions.get(self.sid).decision["view"], view)
        with patch("ollama_fuc.chat", return_value="已恢復回答。"):
            response = self.client.post("/api/chat", json={
                "sessionId": self.sid, "text": "再說明一次",
            })
            self.assertEqual(response.json()["reply"], "已恢復回答。")

    def test_text_commands_still_update_selection_after_a_question(self):
        self.post(action="start")
        with patch("ollama_fuc.chat", return_value="可以自由調整預算。"):
            self.client.post("/api/chat", json={"sessionId": self.sid, "text": "預算怎麼設定？"})
        with patch("ollama_fuc.chat", side_effect=AssertionError("commands must not call AI")):
            for text in ("預算100元", "直接推薦", "換一批"):
                response = self.client.post("/api/chat", json={"sessionId": self.sid, "text": text})
                self.assertIsNotNone(response.json().get("decision"))
        self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["budget"], 100)

    def test_decide_and_batch_reason_actions_work_over_http(self):
        view = self.post(action="start").json()["decision"]
        view = self.post(action="decide", revision=view["revision"]).json()["decision"]
        self.assertTrue(view["focused"])
        self.assertEqual(len(view["items"]), 1)
        response = self.post(action="reject_all", reason="portion", revision=view["revision"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sessions.get(self.sid).decision["prefs"]["portion"], "large")
        self.assertEqual(self.post(action="reject_all", reason="invented").status_code, 422)

    def test_polite_change_and_mixed_question_work_in_both_chat_transports(self):
        first = self.post(action="start").json()["decision"]
        self.post(action="recommend", revision=first["revision"])
        for endpoint in ("/api/chat", "/api/chat/stream"):
            with self.subTest(endpoint=endpoint), patch(
                "ollama_fuc.chat", return_value="菜單未標示辣度。"
            ) as model, patch("ollama_fuc.chat_stream", side_effect=AssertionError("wrong route")):
                response = self.client.post(endpoint, json={
                    "sessionId": self.sid, "text": "可以幫我換成麵嗎？這個會辣嗎？",
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn("菜單未標示辣度", response.text)
                model.assert_called_once()
                view = self.sessions.get(self.sid).decision["view"]
                self.assertTrue(all(i["dishType"] == "麵" for i in view["items"]))
                self.assertIn("菜單未標示辣度", view["answer"])
                if endpoint == "/api/chat":
                    self.assertIn("菜單未標示辣度", response.json()["reply"])

    def test_mixed_question_model_failure_keeps_the_successful_change(self):
        first = self.post(action="start").json()["decision"]
        self.post(action="recommend", revision=first["revision"])
        with patch("ollama_fuc.chat", side_effect=RuntimeError("offline")):
            response = self.client.post("/api/chat", json={
                "sessionId": self.sid, "text": "改吃麵，這個會辣嗎？",
            })
        view = response.json()["decision"]
        self.assertTrue(all(i["dishType"] == "麵" for i in view["items"]))
        self.assertIn("暫時無法回答", view["answer"])
        self.assertEqual(self.post(action="decide", revision=view["revision"]).status_code, 200)

    def test_price_and_allergy_regression_in_both_transports(self):
        self.menus["測試"] = {"categories": [{"name": "主餐", "items": [
            {"name": "鮮蝦炒飯", "price": 50}, {"name": "豬肉飯", "price": 60},
            {"name": "雞腿飯", "price": 90},
        ]}]}
        for suffix in ("", "/stream"):
            sid = self.sid + ("_sync" if not suffix else "_stream")
            with self.subTest(transport=suffix), patch(
                "ollama_fuc.chat", side_effect=AssertionError("unexpected AI call")
            ), patch("ollama_fuc.chat_stream", side_effect=AssertionError("unexpected AI call")):
                endpoint = "/api/chat" + suffix
                self.client.post(endpoint, json={"sessionId": sid, "text": "想吃雞，幫我決定"})
                response = self.client.post(endpoint, json={
                    "sessionId": sid, "text": "太貴了，我對蝦過敏",
                })
                self.assertEqual(response.status_code, 200)
                state = self.sessions.get(sid)
                self.assertIn("shellfish", state.prefs["allergens"])
                self.assertEqual([i["name"] for i in state.decision["view"]["items"]], ["豬肉飯"])


if __name__ == "__main__":
    unittest.main()
