from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RestaurantRecord(Base):
    __tablename__ = "restaurants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    menus: Mapped[list[MenuVersionRecord]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )


class MenuVersionRecord(Base):
    __tablename__ = "menu_versions"
    __table_args__ = (UniqueConstraint("restaurant_id", "version", name="uq_menu_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="unknown")
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    restaurant: Mapped[RestaurantRecord] = relationship(back_populates="menus")
    categories: Mapped[list[MenuCategoryRecord]] = relationship(
        back_populates="menu", cascade="all, delete-orphan", order_by="MenuCategoryRecord.position"
    )


class MenuCategoryRecord(Base):
    __tablename__ = "menu_categories"
    __table_args__ = (UniqueConstraint("menu_id", "position", name="uq_menu_category_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    menu_id: Mapped[str] = mapped_column(ForeignKey("menu_versions.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    menu: Mapped[MenuVersionRecord] = relationship(back_populates="categories")
    items: Mapped[list[MenuItemRecord]] = relationship(
        back_populates="category", cascade="all, delete-orphan", order_by="MenuItemRecord.position"
    )


class MenuItemRecord(Base):
    __tablename__ = "menu_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    category_id: Mapped[str] = mapped_column(ForeignKey("menu_categories.id"), index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="TWD")
    role: Mapped[str] = mapped_column(String(24), default="other", index=True)
    spice_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spice_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spice_adjustable: Mapped[bool] = mapped_column(Boolean, default=False)
    semantic_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    semantic_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    semantic_source: Mapped[str] = mapped_column(String(40), default="unknown")
    position: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[MenuCategoryRecord] = relationship(back_populates="items")


class ReviewRunRecord(Base):
    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="complete", index=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReviewSourceRecord(Base):
    __tablename__ = "review_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    review_run_id: Mapped[str] = mapped_column(ForeignKey("review_runs.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(80), nullable=False)
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(240), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReviewEvidenceRecord(Base):
    __tablename__ = "review_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    review_run_id: Mapped[str] = mapped_column(ForeignKey("review_runs.id"), index=True)
    evidence_id: Mapped[str] = mapped_column(String(80), nullable=False)
    aspect: Mapped[str] = mapped_column(String(40), default="")
    polarity: Mapped[str] = mapped_column(String(20), default="neutral")
    text: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReviewAspectRecord(Base):
    __tablename__ = "review_aspects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    review_run_id: Mapped[str] = mapped_column(ForeignKey("review_runs.id"), index=True)
    aspect: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SessionRecord(Base):
    __tablename__ = "user_sessions"

    session_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    preference_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active_restaurant: Mapped[str | None] = mapped_column(String(160), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ConversationTurnRecord(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(160), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ImportRunRecord(Base):
    __tablename__ = "import_runs"
    __table_args__ = (UniqueConstraint("source_hash", name="uq_import_source_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


Index("idx_active_menu", MenuVersionRecord.restaurant_id, MenuVersionRecord.is_active)
Index("idx_active_review", ReviewRunRecord.restaurant_id, ReviewRunRecord.is_active)
