"""Async SQLAlchemy engine and session factory for the SQLite database."""

from pathlib import Path
from sqlite3 import Connection as SqliteConnection
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

BUSY_TIMEOUT_MS = 5000


def _apply_pragmas(dbapi_connection: Any, _record: ConnectionPoolEntry) -> None:
    """Enable WAL, foreign keys and a busy timeout on every new connection."""
    connection: SqliteConnection = dbapi_connection
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cursor.close()


def sqlite_url(database_path: Path) -> str:
    """Return the async SQLAlchemy URL for a SQLite file."""
    return f"sqlite+aiosqlite:///{database_path}"


def create_engine(database_path: Path) -> AsyncEngine:
    """Create an async engine for the database file with the connection pragmas applied."""
    engine = create_async_engine(sqlite_url(database_path))
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory that keeps objects usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
