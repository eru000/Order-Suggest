from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .repositories import (
    ImportRepository,
    MenuRepository,
    ReviewRepository,
    SQLAlchemySessionRepository,
    content_hash,
)


def menu_document_to_runtime(
    data: dict[str, Any], fallback_name: str
) -> tuple[str, dict[str, Any]]:
    name = str(data.get("name") or fallback_name).strip() or fallback_name
    if isinstance(data.get("restaurants"), dict):
        restaurants = data["restaurants"]
        first = next(iter(restaurants), name)
        return str(first), data
    if isinstance(data.get("categories"), list):
        categories = {
            str(category.get("name") or "其他"): {"items": list(category.get("items") or [])}
            for category in data["categories"]
            if isinstance(category, dict)
        }
    elif isinstance(data.get("menu_items"), list):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in data["menu_items"]:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            category = str(item.get("category") or "全部菜色")
            grouped.setdefault(category, []).append(
                {key: value for key, value in item.items() if key != "category"}
            )
        categories = {category: {"items": items} for category, items in grouped.items()}
    else:
        raise ValueError("unsupported menu JSON schema")
    return name, {
        "restaurants": {name: {"name": name, "categories": categories}},
    }


class LegacyImporter:
    def __init__(
        self,
        menu_repository: MenuRepository,
        review_repository: ReviewRepository,
        import_repository: ImportRepository,
        session_repository: SQLAlchemySessionRepository | None = None,
    ) -> None:
        self.menus = menu_repository
        self.reviews = review_repository
        self.imports = import_repository
        self.sessions = session_repository

    def import_json(self, path: str | Path) -> bool:
        source = Path(path)
        raw = source.read_bytes()
        digest = content_hash(raw)
        if self.imports.contains(digest):
            return False
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"JSON root must be an object: {source}")
        if source.name.startswith("reviews_") or "recommendationScore" in data:
            restaurant_name = str(
                data.get("restaurantName") or source.stem.removeprefix("reviews_")
            )
            self.reviews.save(restaurant_name, data)
            entity_type, count = "review", 1
        else:
            fallback = source.stem.removeprefix("menu_").replace("_", " ")
            if source.name == "menu.json":
                fallback = os.getenv("DEFAULT_RESTAURANT_NAME", "預設餐廳")
            restaurant_name, menu = menu_document_to_runtime(data, fallback)
            self.menus.save(restaurant_name, menu, source="legacy_json", source_hash=digest)
            count = sum(
                len(category.get("items", []))
                for category in menu["restaurants"][restaurant_name]["categories"].values()
            )
            entity_type = "menu"
        self.imports.record(str(source.resolve()), digest, entity_type, count)
        return True

    def import_project(self, project_root: str | Path) -> dict[str, int]:
        root = Path(project_root)
        candidates = [root / "menu.json", root / "db" / "menu.json", root / "src" / "menu.json"]
        candidates.extend(sorted(root.glob("menu_*.json")))
        candidates.extend(sorted(root.glob("reviews_*.json")))
        result = {"imported": 0, "skipped": 0, "failed": 0}
        seen: set[Path] = set()
        for path in candidates:
            if not path.exists() or path in seen:
                continue
            seen.add(path)
            try:
                changed = self.import_json(path)
                result["imported" if changed else "skipped"] += 1
            except Exception:
                result["failed"] += 1
        return result

    def import_sqlite_sessions(self, path: str | Path) -> int:
        source = Path(path)
        if self.sessions is None or not source.exists():
            return 0
        digest = content_hash(source.read_bytes())
        if self.imports.contains(digest):
            return 0
        imported = 0
        connection = sqlite3.connect(source)
        try:
            rows = connection.execute(
                "SELECT session_id, payload_json, expires_at - unixepoch() "
                "FROM sessions WHERE expires_at > unixepoch()"
            ).fetchall()
            for session_id, payload_json, remaining in rows:
                try:
                    payload = json.loads(payload_json)
                    self.sessions.save(str(session_id), payload, max(1, int(remaining)))
                    imported += 1
                except Exception:
                    continue
        finally:
            connection.close()
        self.imports.record(str(source.resolve()), digest, "sessions", imported)
        return imported
