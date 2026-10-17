"""Shared fixtures: a real temporary SQLite database and a crypto service."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_engine
from app.core.migrations import migrate_database


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """Path of a database file that does not exist yet."""
    return tmp_path / "envmanager.db"


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """Folder the migration backups are written to."""
    return tmp_path / "backups"


@pytest.fixture
async def engine(database_path: Path, backup_dir: Path) -> AsyncIterator[AsyncEngine]:
    """An engine on a freshly migrated temporary database."""
    database_engine = create_engine(database_path)
    await migrate_database(database_engine, database_path, backup_dir)
    yield database_engine
    await database_engine.dispose()


@pytest.fixture
def crypto() -> CryptoService:
    """A crypto service on a fresh random key."""
    return CryptoService(parse_master_key(generate_master_key()))
