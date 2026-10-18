"""Retention: by count, by days, both, the newest always kept, only our own files removed."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import Backup, BackupStatus, BackupTrigger
from app.services.app_settings import BackupSettings
from app.services.backup import backup_file_name
from app.services.backup_retention import MAX_FAILED_RECORDS, apply_retention, expired_backups

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def record(number: int, age_days: float, status: BackupStatus = BackupStatus.SUCCESS) -> Backup:
    """A record of a backup taken ``age_days`` before NOW (an unsaved object)."""
    moment = NOW - timedelta(days=age_days)
    return Backup(
        id=number,
        filename=backup_file_name("manual", moment),
        trigger=BackupTrigger.MANUAL,
        status=status,
        size_bytes=10,
        created_at=moment,
    )


def numbers(rows: list[Backup]) -> list[int]:
    """The ids of records, sorted."""
    return sorted(row.id for row in rows)


def test_a_count_keeps_the_newest_backups() -> None:
    """Keeping 3 of 5 removes the two oldest, whatever order the rows come in."""
    rows = [record(index, age_days=index) for index in (3, 1, 5, 2, 4)]
    expired = expired_backups(rows, keep_count=3, keep_days=0, now=NOW)
    assert numbers(expired) == [4, 5]


def test_days_keep_the_backups_inside_the_window() -> None:
    """Anything older than the window goes, the rest stays however many there are."""
    rows = [record(1, 1), record(2, 10), record(3, 29), record(4, 31), record(5, 90)]
    expired = expired_backups(rows, keep_count=0, keep_days=30, now=NOW)
    assert numbers(expired) == [4, 5]


def test_both_limits_must_hold() -> None:
    """With a count and days set, a backup that breaks either one goes."""
    rows = [record(1, 1), record(2, 2), record(3, 3), record(4, 40)]
    expired = expired_backups(rows, keep_count=3, keep_days=30, now=NOW)
    assert numbers(expired) == [4]
    expired = expired_backups(rows, keep_count=2, keep_days=30, now=NOW)
    assert numbers(expired) == [3, 4]


def test_no_limit_removes_nothing() -> None:
    """Zero for both is no retention at all."""
    rows = [record(index, index * 100) for index in range(1, 6)]
    assert expired_backups(rows, keep_count=0, keep_days=0, now=NOW) == []


def test_the_newest_backup_is_always_kept() -> None:
    """A one day window with only old backups still leaves the newest one."""
    rows = [record(1, 50), record(2, 60), record(3, 70)]
    expired = expired_backups(rows, keep_count=0, keep_days=1, now=NOW)
    assert numbers(expired) == [2, 3]


async def test_retention_removes_old_files_and_records_only(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The files of expired backups go with their records; other files and kept ones stay."""
    folder = tmp_path / "backups"
    folder.mkdir()
    rows = [record(index, index) for index in range(1, 6)]
    for row in rows:
        (folder / row.filename).write_bytes(b"x")
    (folder / "notes.txt").write_text("not a backup")
    (folder / "envmanager-20200101T000000000000Z-manual.db.bak").write_text("not ours")
    async with create_session_factory(engine)() as db:
        db.add_all(
            [
                Backup(
                    filename=row.filename,
                    trigger=row.trigger,
                    status=row.status,
                    size_bytes=10,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )
        await db.commit()
        removed = await apply_retention(
            db, folder, BackupSettings(retention_count=3, retention_days=0), now=NOW
        )
        await db.commit()
        left = list((await db.execute(select(Backup.filename))).scalars())
    assert removed == 2
    kept = sorted(row.filename for row in rows[:3])
    assert sorted(left) == kept
    assert sorted(entry.name for entry in folder.iterdir()) == sorted(
        [*kept, "notes.txt", "envmanager-20200101T000000000000Z-manual.db.bak"]
    )


async def test_a_file_that_cannot_be_removed_keeps_its_record(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The list never says a file is gone while it is still there."""
    folder = tmp_path / "backups"
    folder.mkdir()
    old, new = record(1, 10), record(2, 1)
    (folder / old.filename).mkdir()
    (folder / new.filename).write_bytes(b"x")
    async with create_session_factory(engine)() as db:
        for row in (old, new):
            db.add(
                Backup(
                    filename=row.filename,
                    trigger=row.trigger,
                    status=row.status,
                    size_bytes=10,
                    created_at=row.created_at,
                )
            )
        await db.commit()
        removed = await apply_retention(
            db, folder, BackupSettings(retention_count=1, retention_days=0), now=NOW
        )
        await db.commit()
        left = list((await db.execute(select(Backup.filename))).scalars())
    assert removed == 0
    assert sorted(left) == sorted([old.filename, new.filename])


async def test_only_the_newest_failed_records_are_kept(engine: AsyncEngine, tmp_path: Path) -> None:
    """Records of failures carry no file; a long outage does not fill the list."""
    async with create_session_factory(engine)() as db:
        for index in range(MAX_FAILED_RECORDS + 5):
            moment = NOW - timedelta(hours=index)
            db.add(
                Backup(
                    filename=backup_file_name("scheduled", moment),
                    trigger=BackupTrigger.SCHEDULED,
                    status=BackupStatus.FAILURE,
                    error="The database could not be copied.",
                    created_at=moment,
                )
            )
        await db.commit()
        await apply_retention(db, tmp_path, BackupSettings(), now=NOW)
        await db.commit()
        times = list((await db.execute(select(Backup.created_at))).scalars())
    assert len(times) == MAX_FAILED_RECORDS
    assert min(times) == NOW - timedelta(hours=MAX_FAILED_RECORDS - 1)


@pytest.mark.parametrize("name", ["../escape.db", "envmanager-1-manual.db", "sub/x.db"])
async def test_a_record_with_a_name_that_is_not_ours_is_never_unlinked(
    engine: AsyncEngine, tmp_path: Path, name: str
) -> None:
    """A tampered record name cannot make retention remove a file outside the folder."""
    folder = tmp_path / "backups"
    folder.mkdir()
    outside = tmp_path / "escape.db"
    outside.write_text("keep me")
    async with create_session_factory(engine)() as db:
        db.add(
            Backup(
                filename=name,
                trigger=BackupTrigger.MANUAL,
                status=BackupStatus.SUCCESS,
                size_bytes=1,
                created_at=NOW - timedelta(days=100),
            )
        )
        newest = record(1, 0)
        db.add(
            Backup(
                filename=newest.filename,
                trigger=newest.trigger,
                status=newest.status,
                size_bytes=1,
                created_at=newest.created_at,
            )
        )
        await db.commit()
        await apply_retention(db, folder, BackupSettings(retention_count=1), now=NOW)
    assert outside.read_text() == "keep me"
