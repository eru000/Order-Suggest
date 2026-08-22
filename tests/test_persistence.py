import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from persistence import CachedSessionRepository, SQLiteSessionRepository  # noqa: E402
from session_store import SessionStore  # noqa: E402


class PersistenceTest(unittest.TestCase):
    def test_cache_uses_authoritative_remaining_ttl(self):
        class Primary:
            def load_entry(self, session_id):
                return ({"prefs": {}, "history": []}, 37)

            def save(self, session_id, payload, ttl_seconds):
                pass

        class Cache:
            def __init__(self):
                self.saved = None

            def load(self, session_id):
                return None

            def save(self, session_id, payload, ttl_seconds):
                self.saved = (session_id, ttl_seconds)

        cache = Cache()
        repository = CachedSessionRepository(Primary(), cache)

        self.assertIsNotNone(repository.load("session_one"))
        self.assertEqual(("session_one", 37), cache.saved)

    def test_sqlite_round_trip_restores_session(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteSessionRepository(Path(directory) / "test.db")
            first = SessionStore(repository=repository)
            state = first.get("session_one", "餐廳A")
            state.active_restaurant = "餐廳B"
            state.prefs["people"] = 4
            state.history.append({"role": "user", "content": "四個人", "meta": {}})
            first.save("session_one")

            second = SessionStore(repository=repository)
            restored = second.get("session_one", "餐廳A")
            self.assertEqual("餐廳B", restored.active_restaurant)
            self.assertEqual(4, restored.prefs["people"])
            self.assertEqual("四個人", restored.history[0]["content"])


if __name__ == "__main__":
    unittest.main()
