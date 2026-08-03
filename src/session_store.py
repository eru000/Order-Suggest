from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

from main import ConversationTurn, Preferences

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


@dataclass
class SessionState:
    prefs: Preferences = field(default_factory=lambda: cast(Preferences, {}))
    history: list[ConversationTurn] = field(default_factory=list)
    active_restaurant: str | None = None
    touched_at: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class SessionStore:
    def __init__(self, ttl_seconds: int = 24 * 60 * 60, repository: Any = None) -> None:
        self._ttl_seconds = ttl_seconds
        self._repository = repository
        self._states: dict[str, SessionState] = {}
        self._lock = threading.RLock()

    @staticmethod
    def validate_id(session_id: str) -> str:
        value = str(session_id or "").strip()
        if not SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("sessionId 必須是 8 到 160 字的英數、底線或連字號")
        return value

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        expired = [key for key, state in self._states.items() if state.touched_at < cutoff]
        for key in expired:
            self._states.pop(key, None)

    def get(self, session_id: str, default_restaurant: str | None = None) -> SessionState:
        key = self.validate_id(session_id)
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            state = self._states.get(key)
            if state is None:
                loaded = self._repository.load(key) if self._repository else None
                state = SessionState(
                    prefs=cast(Preferences, dict((loaded or {}).get("prefs") or {})),
                    history=list((loaded or {}).get("history") or []),
                    active_restaurant=(loaded or {}).get("active_restaurant") or default_restaurant,
                )
                self._states[key] = state
            elif not state.active_restaurant and default_restaurant:
                state.active_restaurant = default_restaurant
            state.touched_at = now
            return state

    def save(self, session_id: str) -> None:
        if not self._repository:
            return
        key = self.validate_id(session_id)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            with state.lock:
                payload = {
                    "prefs": state.prefs,
                    "history": state.history,
                    "active_restaurant": state.active_restaurant,
                }
            self._repository.save(key, payload, self._ttl_seconds)

    def replace_deleted_restaurant(
        self, restaurant_name: str, fallback_restaurant: str | None
    ) -> None:
        with self._lock:
            for session_id, state in self._states.items():
                with state.lock:
                    if state.active_restaurant == restaurant_name:
                        state.active_restaurant = fallback_restaurant
                        if self._repository:
                            self._repository.save(
                                session_id,
                                {
                                    "prefs": state.prefs,
                                    "history": state.history,
                                    "active_restaurant": state.active_restaurant,
                                },
                                self._ttl_seconds,
                            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)
