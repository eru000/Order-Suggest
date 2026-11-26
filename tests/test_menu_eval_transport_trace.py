"""Offline audit-hook checks: synthetic events and a socket-free urllib handler."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib import request, response

MODULE_PATH = Path(__file__).resolve().parents[1] / "evals/menu_ocr/transport_trace.py"
SPEC = importlib.util.spec_from_file_location("menu_eval_transport_trace", MODULE_PATH)
assert SPEC and SPEC.loader
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


def emit_request(model="actual-model", *, body=None, url="https://models.invalid/chat"):
    if body is None:
        body = json.dumps({"model": model, "messages": ["private prompt"]}).encode()
    sys.audit("urllib.Request", url, body, {"Authorization": "Bearer private-key"}, "POST")


class MenuEvalTransportTraceTests(unittest.TestCase):
    def test_records_repeated_attempts_and_model_without_raw_secrets(self):
        with transport.trace_transport() as trace:
            for _ in range(2):
                emit_request(url="https://private-user:private-pass@models.invalid/?token=secret")
        observed = trace.to_dict()
        self.assertEqual(observed["requestCount"], 2)
        self.assertEqual(observed["identicalRequestRepeats"], 1)
        self.assertEqual(observed["droppedEventCount"], 0)
        self.assertEqual([row["requestedModel"] for row in observed["requests"]],
                         ["actual-model", "actual-model"])
        self.assertEqual(len(observed["requests"][0]["requestSha256"]), 64)
        self.assertEqual(len(observed["requests"][0]["bodySha256"]), 64)
        serialized = json.dumps(observed)
        for secret in ("private-user", "private-pass", "token=secret", "private-key",
                       "Authorization", "models.invalid", "private prompt"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(observed["cacheStatus"], "unknown")
        self.assertFalse(observed["responsesObserved"])

    def test_parallel_case_scopes_do_not_mix_models_or_counts(self):
        barrier = threading.Barrier(2)

        def run(model):
            with transport.trace_transport() as trace:
                barrier.wait(timeout=5)
                emit_request(model)
                barrier.wait(timeout=5)
                emit_request(model)
            return trace.to_dict()

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(run, ("model-a", "model-b")))
        for model, observed in zip(("model-a", "model-b"), rows, strict=True):
            self.assertEqual(observed["requestCount"], 2)
            self.assertEqual({r["requestedModel"] for r in observed["requests"]}, {model})

    def test_nested_scopes_and_exception_restore_previous_observer(self):
        with transport.trace_transport() as outer:
            with self.assertRaisesRegex(RuntimeError, "expected"):
                with transport.trace_transport() as inner:
                    emit_request("inner-model")
                    raise RuntimeError("expected")
            emit_request("outer-model")
        emit_request("unscoped-model")
        self.assertEqual(outer.to_dict()["requestCount"], 2)
        self.assertEqual(inner.to_dict()["requestCount"], 1)
        self.assertEqual(inner.to_dict()["requests"][0]["requestedModel"], "inner-model")

    def test_urllib_event_is_captured_without_changing_actual_request(self):
        seen = []

        class OfflineHandler(request.BaseHandler):
            def default_open(self, req):
                seen.append((req.full_url, req.data, req.get_method(),
                             req.get_header("Authorization")))
                result = response.addinfourl(
                    io.BytesIO(b"offline response"), {}, req.full_url, 200
                )
                result.msg = "OK"
                return result

        opener = request.build_opener(OfflineHandler())
        payload = b'{"model":"actual-wire-model","messages":["secret-image-data"]}'
        url = "https://models.invalid/chat?key=secret-url-key"
        req = request.Request(url, data=payload, headers={"Authorization": "Bearer secret-key"})
        with transport.trace_transport() as trace, opener.open(req) as result:
            self.assertEqual(result.read(), b"offline response")
        self.assertEqual(seen, [(url, payload, "POST", "Bearer secret-key")])
        observed = trace.to_dict()
        self.assertEqual(observed["requestCount"], 1)
        self.assertEqual(observed["requests"][0]["requestedModel"], "actual-wire-model")
        self.assertNotIn("secret", json.dumps(observed))

    def test_replay_guard_blocks_before_the_urllib_handler_sends_a_request(self):
        seen = []

        class OfflineHandler(request.BaseHandler):
            def default_open(self, req):
                seen.append(req)
                raise AssertionError("handler should not be reached")

        opener = request.build_opener(OfflineHandler())
        with transport.trace_transport(deny_network=True) as trace:
            with self.assertRaisesRegex(RuntimeError, "replay mode forbids network requests"):
                opener.open(request.Request("https://models.invalid/chat", data=b'{}'))
        self.assertEqual(seen, [])
        observed = trace.to_dict()
        self.assertEqual(observed["requestCount"], 1)
        self.assertEqual(observed["blockedRequestCount"], 1)
        self.assertTrue(observed["requests"][0]["blockedByReplay"])

    def test_replay_guard_still_blocks_if_recording_fails(self):
        with transport.trace_transport(deny_network=True) as trace:
            with patch.object(transport, "_request_record", side_effect=ValueError("secret")):
                with self.assertRaisesRegex(RuntimeError, "replay mode forbids network requests"):
                    emit_request()
        self.assertEqual(trace.to_dict()["droppedEventCount"], 1)

    def test_no_requests_keeps_cache_unknown_and_does_not_claim_a_hit(self):
        with transport.trace_transport() as trace:
            pass
        observed = trace.to_dict()
        self.assertEqual(observed["requestCount"], 0)
        self.assertEqual(observed["cacheStatus"], "unknown")
        self.assertEqual(observed["requests"], [])

    def test_observer_failure_does_not_abort_request_or_persist_exception(self):
        with transport.trace_transport() as trace:
            with patch.object(transport, "_request_record", side_effect=ValueError("secret")):
                emit_request()
            emit_request()
        observed = trace.to_dict()
        self.assertEqual(observed["droppedEventCount"], 1)
        self.assertEqual(observed["requestCount"], 1)
        self.assertNotIn("secret", json.dumps(observed))

    def test_iterable_body_is_not_consumed_to_compute_hash(self):
        consumed = []

        def chunks():
            consumed.append(True)
            yield b"private body"

        with transport.trace_transport() as trace:
            sys.audit("urllib.Request", "https://models.invalid/chat", chunks(), {}, "POST")
            emit_request(body=b"not JSON")
        self.assertEqual(consumed, [])
        rows = trace.to_dict()["requests"]
        self.assertIsNone(rows[0]["requestSha256"])
        self.assertIsNone(rows[0]["requestedModel"])
        self.assertIsNone(rows[1]["requestedModel"])
        self.assertEqual(len(rows[1]["requestSha256"]), 64)

    def test_fingerprint_changes_when_endpoint_model_or_body_changes(self):
        with transport.trace_transport() as trace:
            emit_request("a")
            emit_request("b")
            emit_request("a", url="https://another-models.invalid/chat")
            emit_request(body=b'{"model":"a","messages":["different image"]}')
        hashes = {row["requestSha256"] for row in trace.to_dict()["requests"]}
        self.assertEqual(len(hashes), 4)


if __name__ == "__main__":
    unittest.main()
