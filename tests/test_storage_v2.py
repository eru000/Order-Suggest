import json
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from persistence import SQLiteSessionRepository

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from storage.database import Database  # noqa: E402
from storage.importer import LegacyImporter  # noqa: E402
from storage.repositories import (  # noqa: E402
    ImportRepository,
    MenuCatalog,
    MenuRepository,
    ReviewRepository,
    SQLAlchemySessionRepository,
)


class StorageV2Test(unittest.TestCase):
    def build_importer(self, directory):
        database = Database(
            f"sqlite+pysqlite:///{Path(directory, 'test.db').as_posix()}", create_schema=True
        )
        menus = MenuRepository(database)
        reviews = ReviewRepository(database)
        return (
            database,
            menus,
            reviews,
            LegacyImporter(
                menus,
                reviews,
                ImportRepository(database),
                SQLAlchemySessionRepository(database),
            ),
        )

    def test_importer_supports_both_menu_shapes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            _, menus, _, importer = self.build_importer(directory)
            old_path = Path(directory, "menu_old.json")
            old_path.write_text(
                json.dumps(
                    {
                        "name": "舊格式店",
                        "categories": [
                            {"name": "主餐", "items": [{"name": "牛肉麵", "price": 120}]}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            new_path = Path(directory, "menu_new.json")
            new_path.write_text(
                json.dumps(
                    {
                        "name": "新格式店",
                        "schemaVersion": 2,
                        "menu_items": [{"name": "中辣麵", "price": 130, "category": "麵類"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertTrue(importer.import_json(old_path))
            self.assertTrue(importer.import_json(new_path))
            self.assertFalse(importer.import_json(new_path))
            self.assertIsNotNone(menus.load("舊格式店"))
            new_menu = menus.load("新格式店")
            item = new_menu["restaurants"]["新格式店"]["categories"]["麵類"]["items"][0]
            self.assertEqual(item["semantic"]["spice"]["max"], 3)

    def test_separate_catalog_instances_observe_database_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            _, menus, _, _ = self.build_importer(directory)
            first = MenuCatalog(menus)
            second = MenuCatalog(menus)
            document = {
                "restaurants": {
                    "Shared": {
                        "name": "Shared",
                        "categories": {"Main": {"items": [{"name": "Soup", "price": 80}]}},
                    }
                }
            }

            first["Shared"] = document
            self.assertIn("Shared", second)
            del first["Shared"]
            self.assertNotIn("Shared", second)

    def test_review_report_is_normalized_into_related_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database, _, reviews, _ = self.build_importer(directory)
            report = {
                "schemaVersion": 3,
                "success": True,
                "sources": [{"sourceId": "S1", "url": "https://example.com", "title": "來源"}],
                "evidence": [
                    {"evidenceId": "E1", "aspect": "taste", "polarity": "positive", "text": "好吃"}
                ],
                "aspects": {"taste": {"score": 80, "confidence": 70}},
            }
            reviews.save("評論店", report)
            self.assertEqual(reviews.load("評論店")["schemaVersion"], 3)
            with database.session() as session:
                self.assertEqual(session.execute(text("SELECT 1")).scalar(), 1)

    def test_sqlite_session_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, importer = self.build_importer(directory)
            legacy_path = Path(directory, "legacy.db")
            legacy = SQLiteSessionRepository(legacy_path)
            legacy.save(
                "session_legacy",
                {"prefs": {"people": 2}, "history": [], "active_restaurant": "店"},
                3600,
            )
            self.assertEqual(importer.import_sqlite_sessions(legacy_path), 1)
            self.assertEqual(importer.import_sqlite_sessions(legacy_path), 0)


if __name__ == "__main__":
    unittest.main()
