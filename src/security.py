from __future__ import annotations

import hmac
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_admin_key(api_key: str | None = Security(_admin_key_header)) -> None:
    """Protect operations that mutate shared data or consume paid services."""
    expected = os.getenv("ADMIN_API_KEY", "").strip()
    if not expected:
        raise HTTPException(503, "管理操作尚未設定 ADMIN_API_KEY")
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(
            401,
            "管理金鑰無效",
            headers={"WWW-Authenticate": "ApiKey"},
        )


class SlidingWindowRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, scope: str, client_key: str, limit: int, window_seconds: int) -> int:
        """Return zero when allowed, otherwise seconds until the next request."""
        now = self._clock()
        cutoff = now - window_seconds
        bucket_key = (scope, client_key)
        with self._lock:
            bucket = self._hits[bucket_key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, int(window_seconds - (now - bucket[0]) + 0.999))
            bucket.append(now)
            if not bucket:
                self._hits.pop(bucket_key, None)
        return 0


RATE_LIMITER = SlidingWindowRateLimiter()


def _client_key(request: Request) -> str:
    if os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def rate_limit(scope: str, limit: int, window_seconds: int):
    async def dependency(request: Request) -> None:
        retry_after = RATE_LIMITER.check(scope, _client_key(request), limit, window_seconds)
        if retry_after:
            raise HTTPException(
                429,
                "操作過於頻繁，請稍後再試",
                headers={"Retry-After": str(retry_after)},
            )

    # Keep route-level policy introspectable for tests and operational audits.
    dependency.__dict__.update(
        rate_limit_scope=scope,
        rate_limit_limit=limit,
        rate_limit_window_seconds=window_seconds,
    )
    return dependency
