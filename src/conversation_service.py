from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from main import generate_conversation, generate_conversation_stream
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

    def _context(self, session_id: str):
        state = self.sessions.get(session_id, self.default_restaurant)
        restaurant = state.active_restaurant
        menu = self.menus.get(restaurant or "")
        if not restaurant or menu is None:
            raise LookupError("目前沒有可用的餐廳菜單")
        return state, menu

    def chat(self, session_id: str, text: str) -> str:
        state, menu = self._context(session_id)
        with state.lock:
            reply, _ = generate_conversation(state.history, text, menu, state.prefs)
        self.audit(session_id, text, reply, state.prefs)
        self.sessions.save(session_id)
        return reply

    def stream(self, session_id: str, text: str) -> Iterator[dict[str, Any]]:
        state, menu = self._context(session_id)
        pieces: list[str] = []
        with state.lock:
            for event in generate_conversation_stream(state.history, text, menu, state.prefs):
                if event.get("type") == "delta":
                    pieces.append(str(event.get("text") or ""))
                yield event
        self.audit(session_id, text, "".join(pieces), state.prefs)
        self.sessions.save(session_id)
