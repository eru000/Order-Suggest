import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from security import SlidingWindowRateLimiter, require_admin_key  # noqa: E402


class SecurityTest(unittest.TestCase):
    def test_admin_key_fails_closed_when_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                require_admin_key("anything")
        self.assertEqual(503, raised.exception.status_code)

    def test_admin_key_rejects_wrong_value_and_accepts_exact_value(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "correct"}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                require_admin_key("wrong")
            self.assertEqual(401, raised.exception.status_code)
            self.assertIsNone(require_admin_key("correct"))

    def test_sliding_window_is_scoped_and_recovers_after_window(self):
        now = [100.0]
        limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
        self.assertEqual(0, limiter.check("vision", "client", 2, 60))
        self.assertEqual(0, limiter.check("vision", "client", 2, 60))
        self.assertEqual(60, limiter.check("vision", "client", 2, 60))
        self.assertEqual(0, limiter.check("crawl", "client", 2, 60))
        self.assertEqual(0, limiter.check("vision", "other", 2, 60))
        now[0] = 161.0
        self.assertEqual(0, limiter.check("vision", "client", 2, 60))


if __name__ == "__main__":
    unittest.main()
