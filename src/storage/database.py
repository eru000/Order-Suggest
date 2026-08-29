from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .models import Base

# src/storage/database.py -> src/storage -> src -> 專案根目錄
REPO_ROOT = Path(__file__).resolve().parents[2]

_TEST_DATA_DIR: Path | None = None


def resolve_data_dir(project_root: str | Path) -> Path:
    """決定 sqlite 檔要放在哪個目錄。

    測試 ``import back``，而 ``api_compat`` 在 import 當下就對專案根目錄建
    catalog——也就是說跑一次測試就會動到正式資料庫。實測過：不重啟伺服器、
    只跑一次測試，四間餐廳全部被刪掉重建（UUID 全換），而
    ``tests/test_back_vision_endpoints.py`` 的 ``_drop_unnamed_menus`` 還會把
    任何叫「未命名菜單」的資料整筆刪除——使用者剛辨識完的菜單就是這樣沒的。

    所以在 unittest 底下，指向專案根目錄的請求一律改用行程專屬的暫存目錄。
    測試自己傳進來的暫存 root 不受影響，它們本來就是隔離的。
    """
    project_root = Path(project_root)
    override = os.getenv("ORDERSUGGEST_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if "unittest" in sys.modules and project_root.resolve() == REPO_ROOT:
        return _test_data_dir()

    return project_root / "data"


def _test_data_dir() -> Path:
    global _TEST_DATA_DIR
    if _TEST_DATA_DIR is None:
        _TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="ordersuggest-tests-"))
    return _TEST_DATA_DIR


def isolate_test_path(path: str | Path) -> Path:
    """測試底下，把落在專案目錄內的資料庫檔改到暫存目錄。

    只擋「沒有明寫路徑」的情況是不夠的：``.env`` 的 ``DATABASE_PATH`` 在
    ``import ollama_fuc`` 當下就被讀進環境變數，測試同樣吃得到，於是舊版
    sqlite 還是會被寫穿。
    """
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    resolved = resolved.resolve()
    if "unittest" in sys.modules and REPO_ROOT in resolved.parents:
        return _test_data_dir() / resolved.name
    return resolved


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
        path = (resolve_data_dir(root) / "ordersuggest-v2.db").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{path.as_posix()}"
    if app_env in {"production", "prod"} and not url.startswith("postgresql"):
        raise RuntimeError("正式環境必須設定 PostgreSQL DATABASE_URL")
    return Database(url, create_schema=create_schema)
