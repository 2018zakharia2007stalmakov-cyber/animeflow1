"""Async SQLAlchemy database setup using SQLite (aiosqlite). Python 3.8 compatible."""
import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "animeflow.db")

DATABASE_URL = "sqlite+aiosqlite:///{}".format(DB_PATH)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)

SessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app.models import achievement as _achievement  # noqa: F401
    from app.models import anime as _anime  # noqa: F401
    from app.models import settings as _settings  # noqa: F401
    from app.models import user as _user  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_columns(conn)


async def _ensure_columns(conn) -> None:
    """Add new columns to existing SQLite tables if they're missing.

    Safe to re-run on every startup — ignores columns that already exist.
    Covers all columns added after the original schema so old databases keep
    working without a destructive migration.
    """
    pending = {
        "anime": [
            ("studio",          "VARCHAR(120) DEFAULT ''"),
            ("episodes_total",  "INTEGER DEFAULT 0"),
            ("series_group",    "VARCHAR(255) DEFAULT ''"),
            ("anilibria_code",  "VARCHAR(120) DEFAULT ''"),
            ("backdrop_url",    "VARCHAR(500) DEFAULT ''"),
        ],
        "episodes": [
            ("anilibria_id",     "INTEGER DEFAULT NULL"),
            ("anilibria_host",   "VARCHAR(255) DEFAULT ''"),
            ("anilibria_hls_hd", "VARCHAR(500) DEFAULT ''"),
            ("anilibria_hls_sd", "VARCHAR(500) DEFAULT ''"),
            ("anilibria_hls_fhd","VARCHAR(500) DEFAULT ''"),
            ("anilibria_iframe", "VARCHAR(500) DEFAULT ''"),
            ("release_date",     "DATETIME DEFAULT NULL"),
        ],
        "users": [
            ("avatar_url",    "VARCHAR(500) DEFAULT NULL"),
            ("bio",           "TEXT DEFAULT NULL"),
            ("login_streak",  "INTEGER DEFAULT 0"),
            ("last_login_at", "DATETIME DEFAULT NULL"),
            ("google_id",     "VARCHAR(255) DEFAULT NULL"),
            ("vk_id",         "VARCHAR(255) DEFAULT NULL"),
        ],
        "user_achievements": [
            ("notified",    "INTEGER DEFAULT 0"),
            ("updated_at",  "DATETIME DEFAULT NULL"),
            ("unlocked_at", "DATETIME DEFAULT NULL"),
        ],
        "watch_progress": [
            ("updated_at",  "DATETIME DEFAULT NULL"),
        ],
    }
    for table, cols in pending.items():
        try:
            res = await conn.exec_driver_sql(
                "PRAGMA table_info({})".format(table)
            )
            existing = {row[1] for row in res.fetchall()}
        except Exception:
            continue
        for name, ddl in cols:
            if name in existing:
                continue
            try:
                await conn.exec_driver_sql(
                    "ALTER TABLE {} ADD COLUMN {} {}".format(table, name, ddl)
                )
            except Exception:
                pass
