from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, cast


class SessionRepository(Protocol):
    def load(self, session_id: str) -> dict[str, Any] | None: ...
    def save(self, session_id: str, payload: dict[str, Any], ttl_seconds: int) -> None: ...


class SQLiteSessionRepository:
    """Durable session storage; each operation owns its short-lived connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)"
            )

    def load_entry(self, session_id: str) -> tuple[dict[str, Any], int] | None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= unixepoch()")
            row = connection.execute(
                "SELECT payload_json, expires_at - unixepoch() FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        return (value, max(1, int(row[1]))) if isinstance(value, dict) else None

    def load(self, session_id: str) -> dict[str, Any] | None:
        entry = self.load_entry(session_id)
        return entry[0] if entry else None

    def save(self, session_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, payload_json, expires_at, updated_at)
                VALUES (?, ?, unixepoch() + ?, unixepoch())
                ON CONFLICT(session_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (session_id, encoded, ttl_seconds),
            )


class RedisSessionCache:
    def __init__(self, url: str) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - deployment configuration error
            raise RuntimeError("REDIS_URL 已設定，但尚未安裝 redis 套件") from exc
        self._client = redis.Redis.from_url(url, decode_responses=True)

    @staticmethod
    def _key(session_id: str) -> str:
        return f"ordersuggest:session:{session_id}"

    def load(self, session_id: str) -> dict[str, Any] | None:
        raw = self._client.get(self._key(session_id))
        if not raw:
            return None
        try:
            value = json.loads(cast(str | bytes | bytearray, raw))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def save(self, session_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        self._client.setex(
            self._key(session_id),
            ttl_seconds,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )


class CachedSessionRepository:
    def __init__(self, primary: SessionRepository, cache: SessionRepository) -> None:
        self.primary = primary
        self.cache = cache

    def load(self, session_id: str) -> dict[str, Any] | None:
        try:
            cached = self.cache.load(session_id)
        except Exception:
            cached = None
        if cached is not None:
            return cached
        remaining_ttl = 0
        if hasattr(self.primary, "load_entry"):
            entry = self.primary.load_entry(session_id)
            value, remaining_ttl = entry if entry else (None, 0)
        else:
            value = self.primary.load(session_id)
        if value is not None and remaining_ttl > 0:
            try:
                # Never let Redis outlive the authoritative session record.
                self.cache.save(session_id, value, remaining_ttl)
            except Exception:
                pass
        return value

    def save(self, session_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        self.primary.save(session_id, payload, ttl_seconds)
        try:
            self.cache.save(session_id, payload, ttl_seconds)
        except Exception:
            # Redis is an acceleration layer; SQLite remains the source of truth.
            pass


def build_session_repository(project_root: str | Path) -> SessionRepository:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        from storage.database import build_database
        from storage.repositories import SQLAlchemySessionRepository

        primary: SessionRepository = SQLAlchemySessionRepository(
            build_database(project_root, create_schema=False)
        )
    else:
        configured = os.getenv("DATABASE_PATH", "").strip()
        database_path = (
            Path(configured) if configured else Path(project_root) / "data" / "ordersuggest.db"
        )
        primary = SQLiteSessionRepository(database_path)
    redis_url = os.getenv("REDIS_URL", "").strip()
    return CachedSessionRepository(primary, RedisSessionCache(redis_url)) if redis_url else primary
