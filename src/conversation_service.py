from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from decision_preferences import split_message
from decision_service import DecisionService
from main import (
    generate_ai_reply,
    generate_ai_reply_stream,
    generate_conversation,
    generate_conversation_stream,
)
from session_store import SessionStore


class ConversationService:
    """Single orchestration path for synchronous and streaming chat transports."""

    def __init__(
        self,
        sessions: SessionStore,
        menus: Mapping[str, dict[str, Any]],
        default_restaurant: str | None,
        audit: Callable[[str, str, str, dict[str, Any]], None],
    ) -> None:
        self.sessions = sessions
        self.menus = menus
        self.default_restaurant = default_restaurant
        self.audit = audit
        self.decisions = DecisionService(sessions, menus, default_restaurant)

    def discovery(self, session_id: str, text: str):
        if self.decisions.accepts_message(session_id, text):
            command, question = split_message(text)
            view = self.decisions.handle(session_id, text=command)
            if question:
                state, menu = self._context(session_id)
                with state.lock:
                    state.history[-2]["content"] = text
                    answer = generate_ai_reply(
                        {}, question, menu=menu, decision_context=self._question_context(state),
                        timeout=40,
                    )
                    view["answer"] = answer
                    state.decision["view"]["answer"] = answer
                    state.history[-1]["content"] = view["message"] + "\n" + answer
                    self.sessions.save(session_id)
            return view
        return None

    def _context(self, session_id: str):
        state = self.sessions.get(session_id, self.default_restaurant)
        restaurant = state.active_restaurant
        menu = self.menus.get(restaurant or "")
        if not restaurant or menu is None:
            raise LookupError("目前沒有可用的餐廳菜單")
        return state, menu

    @staticmethod
    def _question_context(state):
        return {
            "view": state.decision["view"],
            "preferences": state.decision["prefs"],
            "recentConversation": [
                {"role": turn["role"], "content": turn["content"]}
                for turn in state.history[-8:]
            ],
        }

    def chat(self, session_id: str, text: str) -> str:
        decision = self.discovery(session_id, text)
        if decision:
            return "\n".join(filter(None, [decision["message"], decision.get("answer")]))
        state, menu = self._context(session_id)
        with state.lock:
            if state.decision:
                context = self._question_context(state)
                state.history.append({"role": "user", "content": text, "meta": {}})
                reply = generate_ai_reply({}, text, menu=menu, decision_context=context)
                state.history.append({"role": "assistant", "content": reply, "meta": {}})
            else:
                reply, _ = generate_conversation(state.history, text, menu, state.prefs)
        self.audit(session_id, text, reply, state.prefs)
        self.sessions.save(session_id)
        return reply

    def stream(self, session_id: str, text: str) -> Iterator[dict[str, Any]]:
        decision = self.discovery(session_id, text)
        if decision:
            yield {"type": "decision", "decision": decision}
            return
        state, menu = self._context(session_id)
        pieces: list[str] = []
        with state.lock:
            if state.decision:
                context = self._question_context(state)
                state.history.append({"role": "user", "content": text, "meta": {}})
                for piece in generate_ai_reply_stream(
                    {}, text, menu=menu, decision_context=context
                ):
                    pieces.append(piece)
                    yield {"type": "delta", "text": piece}
                state.history.append({"role": "assistant", "content": "".join(pieces), "meta": {}})
            else:
                for event in generate_conversation_stream(state.history, text, menu, state.prefs):
                    if event.get("type") == "delta":
                        pieces.append(str(event.get("text") or ""))
                    yield event
        self.audit(session_id, text, "".join(pieces), state.prefs)
        self.sessions.save(session_id)
