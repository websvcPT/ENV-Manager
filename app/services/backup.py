"""Consistent SQLite snapshots taken with the online backup API."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

BACKUP_PREFIX = "envmanager-"
BACKUP_SUFFIX = ".db"


def backup_file_name(reason: str, moment: datetime | None = None) -> str:
    """Build a sortable backup file name that records why the backup was taken."""
    stamp = (moment or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{BACKUP_PREFIX}{stamp}-{reason}{BACKUP_SUFFIX}"


def _copy_database(source: Path, destination: Path) -> None:
    """Copy a live database into a new file using SQLite's online backup."""
    with sqlite3.connect(source) as source_connection:
        target_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    destination.chmod(0o600)


async def create_backup(source: Path, backup_dir: Path, reason: str) -> Path:
    """Snapshot ``source`` into ``backup_dir`` and return the new file path."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / backup_file_name(reason)
    await asyncio.to_thread(_copy_database, source, destination)
    return destination
