from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, MutableMapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update

from menu_semantics import annotate_menu

from .database import Database
from .models import (
    ConversationTurnRecord,
    ImportRunRecord,
    MenuCategoryRecord,
    MenuItemRecord,
    MenuVersionRecord,
    RestaurantRecord,
    ReviewAspectRecord,
    ReviewEvidenceRecord,
    ReviewRunRecord,
    ReviewSourceRecord,
    SessionRecord,
)


def menu_price(value: Any) -> Decimal | None:
    """Accept a single explicit price, never the first number of a range."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(?:(?:NT|TWD)\s*)?\$?\s*(\d+(?:\.\d+)?)\s*(?:元)?", raw, re.I)
    if not match:
        return None
    price = Decimal(match.group(1))
    return price if price.is_finite() and price >= 0 else None


def normalize_name(value: str) -> str:
    return re.sub(r"\W+", "", str(value or "")).casefold()


class RestaurantRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def get_or_create(session: Any, name: str) -> RestaurantRecord:
        restaurant = session.scalar(select(RestaurantRecord).where(RestaurantRecord.name == name))
        if restaurant is None:
            restaurant = RestaurantRecord(name=name, normalized_name=normalize_name(name))
            session.add(restaurant)
            session.flush()
        return restaurant


class MenuRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(
        self,
        restaurant_name: str,
        menu: dict[str, Any],
        *,
        source: str = "runtime",
        source_hash: str | None = None,
    ) -> None:
        annotated = annotate_menu(menu)
        with self.database.session() as session:
            restaurant = RestaurantRepository.get_or_create(session, restaurant_name)
            current = (
                session.scalar(
                    select(func.max(MenuVersionRecord.version)).where(
                        MenuVersionRecord.restaurant_id == restaurant.id
                    )
                )
                or 0
            )
            session.execute(
                update(MenuVersionRecord)
                .where(MenuVersionRecord.restaurant_id == restaurant.id)
                .values(is_active=False)
            )
            version = MenuVersionRecord(
                restaurant_id=restaurant.id,
                version=int(current) + 1,
                source=source,
                source_hash=source_hash,
                schema_version=2,
                is_active=True,
            )
            session.add(version)
            session.flush()
            restaurant_data = annotated.get("restaurants", {}).get(restaurant_name, {})
            categories = restaurant_data.get("categories", {})
            for category_position, (category_name, category) in enumerate(categories.items()):
                category_row = MenuCategoryRecord(
                    menu_id=version.id,
                    name=str(category_name),
                    position=category_position,
                )
                session.add(category_row)
                session.flush()
                for item_position, item in enumerate(category.get("items", [])):
                    semantic = dict(item.get("semantic") or {})
                    raw_spice = semantic.get("spice")
                    spice: dict[str, Any] = dict(raw_spice) if isinstance(raw_spice, dict) else {}
                    price_value = menu_price(item.get("price"))
                    session.add(
                        MenuItemRecord(
                            category_id=category_row.id,
                            name=str(item.get("name") or "").strip(),
                            normalized_name=normalize_name(str(item.get("name") or "")),
                            price=price_value,
                            role=str(semantic.get("role") or "other"),
                            spice_min=spice.get("min"),
                            spice_max=spice.get("max"),
                            spice_adjustable=bool(spice.get("adjustable")),
                            semantic_profile=semantic,
                            semantic_confidence=float(semantic.get("confidence") or 0),
                            semantic_source=str(semantic.get("source") or "deterministic_v1"),
                            position=item_position,
                        )
                    )

    def repair_imported_prices(self, restaurant_name: str, menu: dict[str, Any], digest: str) -> int:
        """Backfill null prices only in an unchanged, active legacy import."""
        source_categories = menu.get("restaurants", {}).get(restaurant_name, {}).get("categories", {})
        changed = 0
        with self.database.session() as session:
            restaurant = session.scalar(
                select(RestaurantRecord).where(RestaurantRecord.name == restaurant_name)
            )
            if restaurant is None:
                return 0
            version = session.scalar(
                select(MenuVersionRecord).where(
                    MenuVersionRecord.restaurant_id == restaurant.id,
                    MenuVersionRecord.is_active.is_(True),
                    MenuVersionRecord.source == "legacy_json",
                    MenuVersionRecord.source_hash == digest,
                )
            )
            if version is None:
                return 0
            for category in version.categories:
                incoming = source_categories.get(category.name, {}).get("items", [])
                for row in category.items:
                    matches = [item for item in incoming if item.get("name") == row.name]
                    if row.price is None and len(matches) == 1:
                        price = menu_price(matches[0].get("price"))
                        if price is not None:
                            row.price = price
                            changed += 1
        return changed

    def load(self, restaurant_name: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            restaurant = session.scalar(
                select(RestaurantRecord).where(RestaurantRecord.name == restaurant_name)
            )
            if restaurant is None:
                return None
            version = session.scalar(
                select(MenuVersionRecord)
                .where(
                    MenuVersionRecord.restaurant_id == restaurant.id,
                    MenuVersionRecord.is_active.is_(True),
                )
                .order_by(MenuVersionRecord.version.desc())
            )
            if version is None:
                return None
            categories: dict[str, Any] = {}
            for category in version.categories:
                items = []
                for item in category.items:
                    items.append(
                        {
                            "name": item.name,
                            "price": float(item.price) if item.price is not None else None,
                            "semantic": item.semantic_profile or {},
                        }
                    )
                categories[category.name] = {"items": items}
            return {
                "restaurants": {
                    restaurant.name: {"name": restaurant.name, "categories": categories}
                }
            }

    def load_all(self) -> dict[str, dict[str, Any]]:
        with self.database.session() as session:
            names = list(
                session.scalars(select(RestaurantRecord.name).order_by(RestaurantRecord.name))
            )
        return {name: menu for name in names if (menu := self.load(name)) is not None}

    def delete(self, restaurant_name: str) -> bool:
        with self.database.session() as session:
            restaurant = session.scalar(
                select(RestaurantRecord).where(RestaurantRecord.name == restaurant_name)
            )
            if restaurant is None:
                return False
            session.delete(restaurant)
            return True


class MenuCatalog(MutableMapping[str, dict[str, Any]]):
    """Mapping-compatible catalog backed by menu versions in the database."""

    def __init__(
        self,
        repository: MenuRepository,
        initial: dict[str, dict[str, Any]] | None = None,
        cache: Any = None,
    ):
        self.repository = repository
        self.cache = cache
        self._menus = repository.load_all()
        for name, menu in (initial or {}).items():
            if name not in self._menus:
                self.repository.save(name, menu, source="legacy_import")
                self._menus[name] = annotate_menu(menu)

    def __getitem__(self, key: str) -> dict[str, Any]:
        if self.cache is not None and getattr(self.cache, "is_distributed", False):
            cached = self.cache.get(key)
            if cached is not None:
                self._menus[key] = cached
                return cached
        loaded = self.repository.load(key)
        if loaded is None:
            raise KeyError(key)
        self._menus[key] = loaded
        if self.cache is not None and getattr(self.cache, "is_distributed", False):
            self.cache[key] = loaded
        return loaded

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        annotated = annotate_menu(value)
        self.repository.save(key, annotated)
        self._menus[key] = annotated
        if self.cache is not None and getattr(self.cache, "is_distributed", False):
            self.cache[key] = annotated

    def __delitem__(self, key: str) -> None:
        if key not in self._menus and self.repository.load(key) is None:
            raise KeyError(key)
        if not self.repository.delete(key):
            raise KeyError(key)
        self._menus.pop(key, None)
        if self.cache is not None and getattr(self.cache, "is_distributed", False):
            self.cache.pop(key, None)

    def __iter__(self) -> Iterator[str]:
        self._menus = self.repository.load_all()
        return iter(self._menus)

    def __len__(self) -> int:
        self._menus = self.repository.load_all()
        return len(self._menus)


class ReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def load(self, restaurant_name: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            restaurant = session.scalar(
                select(RestaurantRecord).where(RestaurantRecord.name == restaurant_name)
            )
            if restaurant is None:
                return None
            run = session.scalar(
                select(ReviewRunRecord)
                .where(
                    ReviewRunRecord.restaurant_id == restaurant.id,
                    ReviewRunRecord.is_active.is_(True),
                )
                .order_by(ReviewRunRecord.created_at.desc())
            )
            return dict(run.report) if run is not None else None

    def save(self, restaurant_name: str, report: dict[str, Any]) -> None:
        with self.database.session() as session:
            restaurant = RestaurantRepository.get_or_create(session, restaurant_name)
            session.execute(
                update(ReviewRunRecord)
                .where(ReviewRunRecord.restaurant_id == restaurant.id)
                .values(is_active=False)
            )
            run = ReviewRunRecord(
                restaurant_id=restaurant.id,
                schema_version=int(report.get("schemaVersion") or 1),
                status="complete" if report.get("success") else "incomplete",
                report=report,
                is_active=True,
            )
            session.add(run)
            session.flush()
            for source in report.get("sources", []):
                if isinstance(source, dict):
                    session.add(
                        ReviewSourceRecord(
                            review_run_id=run.id,
                            source_id=str(source.get("sourceId") or ""),
                            url=str(source.get("url") or ""),
                            title=str(source.get("title") or ""),
                            payload=source,
                        )
                    )
            for evidence in report.get("evidence", []):
                if isinstance(evidence, dict):
                    session.add(
                        ReviewEvidenceRecord(
                            review_run_id=run.id,
                            evidence_id=str(evidence.get("evidenceId") or ""),
                            aspect=str(evidence.get("aspect") or ""),
                            polarity=str(evidence.get("polarity") or "neutral"),
                            text=str(evidence.get("text") or ""),
                            payload=evidence,
                        )
                    )
            for aspect, value in (report.get("aspects") or {}).items():
                if isinstance(value, dict):
                    session.add(
                        ReviewAspectRecord(
                            review_run_id=run.id,
                            aspect=str(aspect),
                            score=float(value["score"]) if value.get("score") is not None else None,
                            confidence=float(value.get("confidence") or 0),
                            payload=value,
                        )
                    )


class ConversationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self, session_id: str, role: str, content: str, meta: dict[str, Any] | None = None
    ) -> None:
        with self.database.session() as session:
            session.add(
                ConversationTurnRecord(
                    session_id=session_id,
                    role=role,
                    content=content,
                    meta=meta or {},
                )
            )


class SQLAlchemySessionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def load_entry(self, session_id: str) -> tuple[dict[str, Any], int] | None:
        now = datetime.now(UTC)
        with self.database.session() as session:
            row = session.get(SessionRecord, session_id)
            if row is None:
                return None
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            remaining = int((expires - now).total_seconds())
            if remaining <= 0:
                session.delete(row)
                return None
            preferences = dict(row.preference_state or {})
            # Reserved storage key keeps discovery separate from user preferences,
            # without requiring a schema change for existing PostgreSQL deployments.
            decision = preferences.pop("_meal_decision", {})
            payload: dict[str, Any] = {
                "prefs": preferences,
                "decision": decision,
                "active_restaurant": row.active_restaurant,
                "history": [],
            }
            turns = list(
                session.scalars(
                    select(ConversationTurnRecord)
                    .where(ConversationTurnRecord.session_id == session_id)
                    .order_by(ConversationTurnRecord.created_at.asc())
                    .limit(100)
                )
            )
            payload["history"] = [
                {"role": turn.role, "content": turn.content, "meta": turn.meta or {}}
                for turn in turns
            ]
            return payload, remaining

    def load(self, session_id: str) -> dict[str, Any] | None:
        entry = self.load_entry(session_id)
        return entry[0] if entry else None

    def save(self, session_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        with self.database.session() as session:
            row = session.get(SessionRecord, session_id)
            if row is None:
                row = SessionRecord(session_id=session_id, expires_at=expires)
                session.add(row)
            row.preference_state = dict(payload.get("prefs") or {})
            if payload.get("decision"):
                row.preference_state = {
                    **row.preference_state, "_meal_decision": payload["decision"],
                }
            row.active_restaurant = payload.get("active_restaurant")
            row.expires_at = expires
            existing = int(
                session.scalar(
                    select(func.count())
                    .select_from(ConversationTurnRecord)
                    .where(ConversationTurnRecord.session_id == session_id)
                )
                or 0
            )
            for turn in list(payload.get("history") or [])[existing:]:
                if isinstance(turn, dict):
                    session.add(
                        ConversationTurnRecord(
                            session_id=session_id,
                            role=str(turn.get("role") or "user"),
                            content=str(turn.get("content") or ""),
                            meta=dict(turn.get("meta") or {}),
                        )
                    )


class ImportRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def contains(self, source_hash: str) -> bool:
        with self.database.session() as session:
            return (
                session.scalar(
                    select(ImportRunRecord.id).where(ImportRunRecord.source_hash == source_hash)
                )
                is not None
            )

    def record(self, path: str, source_hash: str, entity_type: str, count: int) -> None:
        with self.database.session() as session:
            session.add(
                ImportRunRecord(
                    source_path=path,
                    source_hash=source_hash,
                    entity_type=entity_type,
                    entity_count=count,
                )
            )


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
