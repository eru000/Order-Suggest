from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, MutableMapping
from typing import Any, cast


class ExpiringJsonStore(MutableMapping[str, dict[str, Any]]):
    """Redis-backed short-lived store with an in-process development fallback."""

    def __init__(self, namespace: str, ttl_seconds: int) -> None:
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self._local: dict[str, dict[str, Any]] = {}
        self._redis = None
        redis_url = os.getenv("REDIS_URL", "").strip()
        self.required = bool(redis_url) and os.getenv("APP_ENV", "development").lower() in {
            "production",
            "prod",
        }
        if redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
            except Exception:
                self._redis = None

    @property
    def available(self) -> bool:
        return not self.required or self._redis is not None

    @property
    def is_distributed(self) -> bool:
        """Whether reads are backed by Redis instead of process-local memory."""
        return self._redis is not None

    def _key(self, key: str) -> str:
        return f"ordersuggest:{self.namespace}:{key}"

    def __getitem__(self, key: str) -> dict[str, Any]:
        if self._redis is not None:
            try:
                raw = cast(str | bytes | bytearray | None, self._redis.get(self._key(key)))
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        return self._local[key]

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        self._local[key] = value
        if self._redis is not None:
            try:
                self._redis.setex(
                    self._key(key), self.ttl_seconds, json.dumps(value, ensure_ascii=False)
                )
            except Exception:
                pass

    def __delitem__(self, key: str) -> None:
        existed = key in self._local
        self._local.pop(key, None)
        if self._redis is not None:
            try:
                existed = bool(self._redis.delete(self._key(key))) or existed
            except Exception:
                pass
        if not existed:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        keys = set(self._local)
        if self._redis is not None:
            try:
                prefix = self._key("")
                keys.update(
                    str(key).removeprefix(prefix) for key in self._redis.scan_iter(f"{prefix}*")
                )
            except Exception:
                pass
        return iter(keys)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key: str, default: Any = None) -> Any:
        try:
            value = self[key]
        except KeyError:
            return default
        del self[key]
        return value

    def clear(self) -> None:
        for key in list(self):
            try:
                del self[key]
            except KeyError:
                pass


class DistributedLock:
    """Redis lock in deployed environments, threading lock for local development."""

    def __init__(self, name: str, timeout: int = 300) -> None:
        self._local = threading.Lock()
        self._redis_lock = None
        redis_url = os.getenv("REDIS_URL", "").strip()
        self.required = bool(redis_url) and os.getenv("APP_ENV", "development").lower() in {
            "production",
            "prod",
        }
        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url)
                client.ping()
                self._redis_lock = client.lock(f"ordersuggest:lock:{name}", timeout=timeout)
            except Exception:
                self._redis_lock = None

    @property
    def available(self) -> bool:
        return not self.required or self._redis_lock is not None

    def acquire(self, blocking: bool = True) -> bool:
        if self._redis_lock is not None:
            try:
                return bool(self._redis_lock.acquire(blocking=blocking))
            except Exception:
                return False
        if self.required:
            return False
        return self._local.acquire(blocking=blocking)

    def release(self) -> None:
        if self._redis_lock is not None:
            try:
                if self._redis_lock.owned():
                    self._redis_lock.release()
            except Exception:
                pass
            return
        if self._local.locked():
            self._local.release()

    def locked(self) -> bool:
        if self._redis_lock is not None:
            try:
                return bool(self._redis_lock.owned())
            except Exception:
                return False
        return self._local.locked()
