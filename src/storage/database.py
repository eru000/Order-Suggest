from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .models import Base


class Database:
    """Owns the SQLAlchemy engine and short-lived transaction sessions."""

    def __init__(self, url: str, *, create_schema: bool = False) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.url = url
        self.engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
            poolclass=NullPool if url.startswith("sqlite") else None,
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        if create_schema:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def build_database(project_root: str | Path, *, create_schema: bool = True) -> Database:
    root = Path(project_root)
    configured = os.getenv("DATABASE_URL", "").strip()
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if configured:
        url = configured
    else:
        path = (root / "data" / "ordersuggest-v2.db").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{path.as_posix()}"
    if app_env in {"production", "prod"} and not url.startswith("postgresql"):
        raise RuntimeError("正式環境必須設定 PostgreSQL DATABASE_URL")
    return Database(url, create_schema=create_schema)
