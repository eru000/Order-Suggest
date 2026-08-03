from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import text

from runtime_cache import ExpiringJsonStore
from storage.database import Database, build_database
from storage.importer import LegacyImporter
from storage.repositories import (
    ConversationRepository,
    ImportRepository,
    MenuCatalog,
    MenuRepository,
    ReviewRepository,
    SQLAlchemySessionRepository,
)


class CachedReviewRepository:
    def __init__(self, primary: ReviewRepository, cache: ExpiringJsonStore) -> None:
        self.primary = primary
        self.cache = cache

    def load(self, restaurant_name: str):
        if self.cache.is_distributed:
            cached = self.cache.get(restaurant_name)
            if cached is not None:
                return cached
        report = self.primary.load(restaurant_name)
        if report is not None and self.cache.is_distributed:
            self.cache[restaurant_name] = report
        return report

    def save(self, restaurant_name: str, report: dict[str, Any]) -> None:
        self.primary.save(restaurant_name, report)
        if self.cache.is_distributed:
            self.cache[restaurant_name] = report


@lru_cache(maxsize=8)
def database_for(project_root: str) -> Database:
    return build_database(
        Path(project_root), create_schema=not bool(os.getenv("DATABASE_URL", "").strip())
    )


def build_menu_catalog(project_root: str | Path, legacy_menus: dict[str, Any]) -> MenuCatalog:
    database = database_for(str(Path(project_root).resolve()))
    menus = MenuRepository(database)
    if not os.getenv("DATABASE_URL", "").strip():
        importer = LegacyImporter(
            menus,
            ReviewRepository(database),
            ImportRepository(database),
            SQLAlchemySessionRepository(database),
        )
        importer.import_project(project_root)
    return MenuCatalog(menus, legacy_menus, ExpiringJsonStore("menu", 24 * 60 * 60))


@lru_cache(maxsize=8)
def _review_repository(project_root: str) -> CachedReviewRepository:
    primary = ReviewRepository(database_for(project_root))
    return CachedReviewRepository(primary, ExpiringJsonStore("review", 60 * 60))


def review_repository(project_root: str | Path) -> CachedReviewRepository:
    return _review_repository(str(Path(project_root).resolve()))


def conversation_repository(project_root: str | Path) -> ConversationRepository:
    return ConversationRepository(database_for(str(Path(project_root).resolve())))


def database_ready(project_root: str | Path) -> bool:
    try:
        with database_for(str(Path(project_root).resolve())).session() as session:
            return session.execute(text("SELECT 1")).scalar() == 1
    except Exception:
        return False
