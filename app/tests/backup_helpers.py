"""Helpers shared by the backup tests: notifiers, locations, settings and rows read back."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AllowedPath, AuditEntry, Backup
from app.services.app_settings import BackupSettings, save_section
from app.services.audit import AuditActor
from app.services.backup_events import BackupFinished
from app.services.backup_run import BackupLocations


@dataclass
class RecordingBackupNotifier:
    """Keeps the backup events it receives."""

    events: list[BackupFinished] = field(default_factory=list)

    async def backup_finished(self, event: BackupFinished) -> None:
        """Remember the event."""
        self.events.append(event)


class FailingBackupNotifier:
    """A notifier that always fails, to prove a broken notifier never fails a backup."""

    async def backup_finished(self, event: BackupFinished) -> None:
        """Fail."""
        raise RuntimeError("the mail server is down")


def locations_in(root: Path, database_path: Path) -> BackupLocations:
    """Backup, lock and database locations below ``root``."""
    return BackupLocations(database_path, root / "backups", root / "locks")


async def save_backup_settings(
    session_factory: async_sessionmaker[AsyncSession], **values: Any
) -> BackupSettings:
    """Store backup settings (defaults for what is not given) the way a save does."""
    settings = BackupSettings(**values)
    async with session_factory() as db:
        await save_section(db, "backup", settings, actor=AuditActor.cli())
        await db.commit()
    return settings


async def allow_path(session_factory: async_sessionmaker[AsyncSession], path: Path) -> None:
    """Add a folder to the allowed paths."""
    async with session_factory() as db:
        db.add(AllowedPath(path=str(path)))
        await db.commit()


async def backup_records(session_factory: async_sessionmaker[AsyncSession]) -> list[Backup]:
    """Every backup record, oldest first."""
    async with session_factory() as db:
        rows = await db.execute(select(Backup).order_by(Backup.created_at, Backup.id))
        return list(rows.scalars())


async def audit_entries(
    session_factory: async_sessionmaker[AsyncSession], action: str
) -> list[AuditEntry]:
    """The audit entries of one action, oldest first."""
    async with session_factory() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


def app_session_factory(application: FastAPI) -> async_sessionmaker[AsyncSession]:
    """The session factory of a running application."""
    factory: async_sessionmaker[AsyncSession] = application.state.session_factory
    return factory


def app_locations(application: FastAPI) -> BackupLocations:
    """The backup locations of a running application."""
    locations: BackupLocations = application.state.backup_locations
    return locations


def read_rows(
    database: Path, sql: str, parameters: tuple[object, ...] = ()
) -> list[tuple[Any, ...]]:
    """Rows of a query run on a file, read straight with sqlite3."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        return connection.execute(sql, parameters).fetchall()
