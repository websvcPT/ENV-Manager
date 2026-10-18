"""Snapshots: consistent under concurrent writes, private on disk, free of keys and plaintext."""

import asyncio
import os
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, parse_master_key
from app.core.db import create_session_factory
from app.models import EnvironmentVersion
from app.seeds import SeedData
from app.services.backup import (
    BackupProblem,
    BackupSnapshotError,
    backup_file_name,
    create_backup,
    ensure_backup_folder,
    parse_backup_name,
    write_snapshot,
)
from app.tests.backup_helpers import read_rows

BATCH_SIZE = 5
PADDING = "x" * 100_000
NOW = "2026-10-18 02:00:00"


def mode_of(path: Path) -> int:
    """The permission bits of a path, without following a link."""
    return stat.S_IMODE(path.lstat().st_mode)


class Writer(threading.Thread):
    """Commits batches of rows to a database file as fast as it can until told to stop.

    Each batch is one transaction that adds ``BATCH_SIZE`` rows with the same name prefix, so a
    snapshot that shows a batch only in part has seen a transaction half done.
    """

    def __init__(self, database: Path) -> None:
        super().__init__(daemon=True)
        self.database = database
        self.stop_event = threading.Event()
        self.batches = 0
        self.failure: BaseException | None = None

    def run(self) -> None:
        try:
            with closing(sqlite3.connect(self.database, timeout=30)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                while not self.stop_event.is_set():
                    with connection:
                        for index in range(BATCH_SIZE):
                            connection.execute(
                                "INSERT INTO app_settings (key, value, updated_at) "
                                "VALUES (?, ?, ?)",
                                (f"batch.{self.batches}.{index}", "v", NOW),
                            )
                    self.batches += 1
        except BaseException as error:  # reported to the test thread
            self.failure = error


def fill_database(database: Path) -> None:
    """Make the database large enough that copying it takes a moment."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        for index in range(300):
            connection.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (f"padding.{index}", PADDING, NOW),
            )


async def test_a_snapshot_is_consistent_while_the_database_is_written(
    engine: AsyncEngine, database_path: Path, tmp_path: Path
) -> None:
    """Every batch in a snapshot is whole, the file is intact, and writes went on meanwhile."""
    fill_database(database_path)
    writer = Writer(database_path)
    writer.start()
    try:
        while writer.batches < 5 and writer.failure is None:
            await asyncio.sleep(0.01)
        started = writer.batches
        snapshots = [
            await create_backup(database_path, tmp_path / "backups", f"manual-{index}")
            for index in range(4)
        ]
        finished = writer.batches
    finally:
        writer.stop_event.set()
        writer.join(timeout=30)
    assert writer.failure is None
    assert finished > started
    for snapshot in snapshots:
        assert read_rows(snapshot, "PRAGMA integrity_check") == [("ok",)]
        counts = read_rows(
            snapshot,
            "SELECT substr(key, 1, instr(substr(key, 7), '.') + 6) AS batch, count(*) "
            "FROM app_settings WHERE key LIKE 'batch.%' GROUP BY batch",
        )
        assert counts
        assert {count for _batch, count in counts} == {BATCH_SIZE}


async def test_a_snapshot_is_one_private_self_contained_file(
    engine: AsyncEngine, database_path: Path, tmp_path: Path
) -> None:
    """The folder is 0700, the file 0600, it needs no side files and no temporary file stays."""
    snapshot = await create_backup(database_path, tmp_path / "backups", "manual")
    assert mode_of(snapshot.parent) == 0o700
    assert mode_of(snapshot) == 0o600
    assert [entry.name for entry in snapshot.parent.iterdir()] == [snapshot.name]
    header = snapshot.read_bytes()[:100]
    assert (header[18], header[19]) == (1, 1)
    assert read_rows(snapshot, "PRAGMA journal_mode") == [("delete",)]
    assert [entry.name for entry in snapshot.parent.iterdir()] == [snapshot.name]


async def test_an_existing_wider_folder_is_tightened(
    engine: AsyncEngine, database_path: Path, tmp_path: Path
) -> None:
    """A backup folder made earlier with the default mode becomes 0700 at the next backup."""
    folder = tmp_path / "backups"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    await create_backup(database_path, folder, "manual")
    assert mode_of(folder) == 0o700


def test_a_symlink_or_file_in_place_of_the_folder_is_refused(tmp_path: Path) -> None:
    """The folder is never a link to somewhere else, nor a file."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "backups"
    link.symlink_to(target)
    with pytest.raises(BackupSnapshotError) as refused:
        ensure_backup_folder(link)
    assert refused.value.problem is BackupProblem.FOLDER_UNUSABLE
    plain = tmp_path / "file"
    plain.write_text("x")
    with pytest.raises(BackupSnapshotError):
        ensure_backup_folder(plain)
    assert list(target.iterdir()) == []


async def test_a_snapshot_holds_no_master_key_and_no_plaintext_value(
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    database_path: Path,
    tmp_path: Path,
    master_key: str,
    crypto: CryptoService,
) -> None:
    """Values are only in encrypted form and the key never enters the database file."""
    async with create_session_factory(seeded_database)() as db:
        stored = list((await db.execute(select(EnvironmentVersion.content_encrypted))).scalars())
    plaintexts = [crypto.decrypt(token) for token in stored]
    long_values = [value for value in plaintexts if len(value) >= 12]
    assert long_values
    snapshot = await create_backup(database_path, tmp_path / "backups", "manual")
    data = snapshot.read_bytes()
    assert all(token.encode() in data for token in stored)
    assert not any(value in data for value in long_values)
    assert master_key.encode() not in data
    assert parse_master_key(master_key) not in data


async def test_a_failed_snapshot_leaves_no_file_and_names_no_path(
    tmp_path: Path, engine: AsyncEngine, database_path: Path
) -> None:
    """A missing database is a fixed message, and the folder holds nothing afterwards."""
    folder = tmp_path / "backups"
    with pytest.raises(BackupSnapshotError) as failed:
        await create_backup(tmp_path / "missing.db", folder, "manual")
    assert failed.value.problem is BackupProblem.COPY_FAILED
    assert "/" not in str(failed.value)
    assert list(folder.iterdir()) == []


def test_a_snapshot_never_replaces_an_existing_file(tmp_path: Path, database_path: Path) -> None:
    """The destination is created exclusively: an existing name makes the snapshot fail."""
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("CREATE TABLE t (a)")
    destination = tmp_path / "taken.db"
    destination.write_text("keep")
    with pytest.raises(BackupSnapshotError):
        write_snapshot(database_path, destination)
    assert destination.read_text() == "keep"
    assert not any(entry.name.endswith(".tmp") for entry in tmp_path.iterdir())


def test_backup_names_carry_the_moment_and_the_reason() -> None:
    """The name is built and read back without loss; other names are not ours."""
    moment = datetime(2026, 10, 18, 2, 0, 5, 123456, tzinfo=UTC)
    name = backup_file_name("pre-migration", moment)
    parsed = parse_backup_name(name)
    assert parsed is not None
    assert (parsed.moment, parsed.reason) == (moment, "pre-migration")
    for other in ("notes.txt", "envmanager-bad-manual.db", "../envmanager-1-manual.db", "x" + name):
        assert parse_backup_name(other) is None


def test_the_umask_does_not_widen_a_snapshot(tmp_path: Path, database_path: Path) -> None:
    """A permissive umask still gives 0600: the mode is set when the file is created."""
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("CREATE TABLE t (a)")
    previous = os.umask(0)
    try:
        destination = tmp_path / "out.db"
        write_snapshot(database_path, destination)
    finally:
        os.umask(previous)
    assert mode_of(destination) == 0o600
