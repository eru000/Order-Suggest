from __future__ import annotations

import argparse
from pathlib import Path

from storage.database import build_database
from storage.importer import LegacyImporter
from storage.repositories import (
    ImportRepository,
    MenuRepository,
    ReviewRepository,
    SQLAlchemySessionRepository,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import OrderSuggest legacy JSON/SQLite data")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    database = build_database(root)
    importer = LegacyImporter(
        MenuRepository(database),
        ReviewRepository(database),
        ImportRepository(database),
        SQLAlchemySessionRepository(database),
    )
    result = importer.import_project(root)
    session_count = importer.import_sqlite_sessions(root / "data" / "ordersuggest.db")
    print({**result, "sessions": session_count})


if __name__ == "__main__":
    main()
