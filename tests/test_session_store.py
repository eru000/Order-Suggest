import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from session_store import SessionStore  # noqa: E402


class SessionStoreTest(unittest.TestCase):
    def test_restaurant_is_isolated_per_session(self):
        store = SessionStore()
        first = store.get("session_a", "A")
        second = store.get("session_b", "A")
        first.active_restaurant = "B"
        self.assertEqual("B", store.get("session_a").active_restaurant)
        self.assertEqual("A", second.active_restaurant)

    def test_deleted_restaurant_is_replaced_for_affected_sessions_only(self):
        store = SessionStore()
        store.get("session_a", "A").active_restaurant = "B"
        store.get("session_b", "A").active_restaurant = "A"
        store.replace_deleted_restaurant("B", "C")
        self.assertEqual("C", store.get("session_a").active_restaurant)
        self.assertEqual("A", store.get("session_b").active_restaurant)

    def test_invalid_session_id_is_rejected(self):
        store = SessionStore()
        with self.assertRaises(ValueError):
            store.get("short")


if __name__ == "__main__":
    unittest.main()
