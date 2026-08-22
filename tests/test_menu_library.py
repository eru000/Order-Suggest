import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from menu_library import load_menu_library  # noqa: E402


class MenuLibraryTest(unittest.TestCase):
    def test_loads_crawled_documents_and_chooses_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "menu_first.json"
            second = root / "menu_second.json"
            first.write_text(
                json.dumps({"name": "A", "menu_items": [{"name": "飯", "price": "$10"}]}),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps({"name": "B", "menu_items": [{"name": "麵", "price": "20"}]}),
                encoding="utf-8",
            )
            os.utime(first, (100, 100))
            os.utime(second, (200, 200))
            menus, active = load_menu_library(root, "預設")
        self.assertEqual({"A", "B"}, set(menus))
        self.assertEqual("B", active)
        self.assertEqual(
            "20", menus["B"]["restaurants"]["B"]["categories"]["全部菜色"]["items"][0]["price"]
        )


if __name__ == "__main__":
    unittest.main()
