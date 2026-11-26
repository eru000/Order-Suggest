"""Scoped observation of urllib request attempts, without changing HTTP calls.

Usage::

    with trace_transport() as trace:
        result = analyze_menu_image(...)
    transport = trace.to_dict()

The process-wide audit hook is installed once and stays inert outside a scope.
ContextVars isolate concurrent case workers; nested scopes each observe the event.
The urllib.Request event occurs before opening a connection: it does not establish
that a server received a request, nor expose responses, gateway caching, or model
versions. Zero observed events is never interpreted as a cache hit. Other HTTP
clients and work delegated to a new thread are outside this observer's scope.
For offline replay, deny_network=True explicitly blocks scoped urllib attempts.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_ACTIVE: ContextVar[tuple[TransportTrace, ...]] = ContextVar(
    "menu_ocr_transport_traces", default=()
)
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def _request_record(args: tuple[Any, ...]) -> dict[str, Any]:
    # Audit arguments are (fullurl, data, headers, method). Never inspect headers
    # or persist URL/body content; only their fingerprints leave this function.
    url, data, _, method = args
    body = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else None
    digest = None
    if isinstance(url, str) and isinstance(method, str) and (body is not None or data is None):
        fingerprint = hashlib.sha256()
        for part in (method.encode("utf-8"), url.encode("utf-8"), body or b""):
            fingerprint.update(len(part).to_bytes(8, "big"))
            fingerprint.update(part)
        digest = fingerprint.hexdigest()
    model = None
    if body is not None:
        try:
            payload = json.loads(body)
            candidate = payload.get("model") if isinstance(payload, dict) else None
            if isinstance(candidate, str) and len(candidate) <= 256:
                model = candidate
        except (ValueError, UnicodeError):
            pass
    return {
        "requestSha256": digest,
        "bodySha256": hashlib.sha256(body).hexdigest() if body is not None else None,
        "requestedModel": model,
    }


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event != "urllib.Request":
        return
    traces = _ACTIVE.get()
    if not traces:
        return
    denied = any(trace.deny_network for trace in traces)
    try:
        record = {**_request_record(args), "blockedByReplay": denied}
        for trace in traces:
            trace._record(record)
    except Exception:
        # Instrumentation must not abort or change the real request. Report lost
        # observations without persisting exception text (which may contain data).
        for trace in traces:
            trace._drop_event()
    if denied:
        raise RuntimeError("replay mode forbids network requests")


def _install_hook() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if not _INSTALLED:
            sys.addaudithook(_audit_hook)
            _INSTALLED = True


class TransportTrace:
    def __init__(self, *, deny_network: bool = False) -> None:
        self.deny_network = deny_network
        self._started = time.monotonic()
        self._requests: list[dict[str, Any]] = []
        self._dropped = 0
        self._lock = threading.Lock()

    def _record(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._requests.append({
                "index": len(self._requests) + 1,
                "elapsedSec": round(time.monotonic() - self._started, 6),
                **record,
            })

    def _drop_event(self) -> None:
        with self._lock:
            self._dropped += 1

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            requests = [dict(row) for row in self._requests]
            dropped = self._dropped
        counts = Counter(row["requestSha256"] for row in requests if row["requestSha256"])
        return {
            "observer": "python.audit.urllib.Request",
            "observationScope": "current context urllib request attempts before network I/O",
            "requestHashScope": "method, URL and body bytes; headers excluded; SHA-256 only",
            "requestCount": len(requests),
            "blockedRequestCount": sum(row["blockedByReplay"] for row in requests),
            "droppedEventCount": dropped,
            # Repeated bodies can also be deliberate calls: do not label as retries.
            "identicalRequestRepeats": sum(count - 1 for count in counts.values()),
            "responsesObserved": False,
            "cacheStatus": "unknown",
            "underlyingModelVersion": "unknown",
            "requests": requests,
        }


@contextmanager
def trace_transport(*, deny_network: bool = False) -> Iterator[TransportTrace]:
    """Observe urllib attempts in this case/thread, restoring scope on exceptions."""
    _install_hook()
    trace = TransportTrace(deny_network=deny_network)
    token = _ACTIVE.set((*_ACTIVE.get(), trace))
    try:
        yield trace
    finally:
        _ACTIVE.reset(token)
