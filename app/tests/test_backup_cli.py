"""The backup commands of the command line tool: create and restore, and their refusals."""

import asyncio
import shutil
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_engine, create_session_factory
from app.models import AppSetting, AuditActorType, AuditEntry, Backup, BackupStatus, BackupTrigger
from app.services.app_settings import BackupSettings, save_section
from app.services.audit import AuditActor
from app.services.backup_lock import hold_backup_lock
from app.tests.backup_helpers import audit_entries, backup_records, read_rows

runner = CliRunner()
MARKER = "marker.value"
Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
def data_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A seeded database in a data folder the command line tool is pointed at."""
    folder = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(folder))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    yield folder
    get_settings.cache_clear()


def run_database[T](folder: Path, operation: Callable[[Factory], Awaitable[T]]) -> T:
    """Run a coroutine function on the CLI database, with a session and a session factory."""

    async def go() -> T:
        engine = create_engine(folder / "envmanager.db")
        try:
            factory = create_session_factory(engine)
            return await operation(factory)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def set_marker(folder: Path, value: str) -> None:
    """Write the marker setting a restore must bring back."""

    async def go(factory: Factory) -> None:
        async with factory() as db:
            row = await db.get(AppSetting, MARKER)
            if row is None:
                db.add(AppSetting(key=MARKER, value=value))
            else:
                row.value = value
            await db.commit()

    run_database(folder, go)


def marker(folder: Path) -> str | None:
    """The marker setting as stored now."""
    rows = read_rows(
        folder / "envmanager.db", "SELECT value FROM app_settings WHERE key = ?", (MARKER,)
    )
    return rows[0][0] if rows else None


def created_file(output: str) -> Path:
    """The backup path that ``backup create`` printed."""
    line = next(line for line in output.splitlines() if line.startswith("backup created: "))
    return Path(line.removeprefix("backup created: ").rsplit(" (", 1)[0])


def test_backup_create_writes_a_private_backup_and_records_it(data_path: Path) -> None:
    """A file in the backup folder (0600, folder 0700), a record and an audit entry of the CLI."""
    result = runner.invoke(app, ["backup", "create"])
    assert result.exit_code == 0, result.output
    path = created_file(result.output)
    assert path.parent == data_path / "backups" and path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert read_rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert read_rows(path, "SELECT count(*) FROM users")[0][0] >= 8
    assert read_rows(path, "SELECT count(*) FROM app_settings WHERE key = 'key_check'") == [(1,)]
    assert "KB" in result.output or "MB" in result.output

    async def read(factory: Factory) -> tuple[list[Backup], list[AuditEntry]]:
        return await backup_records(factory), await audit_entries(factory, "backup.create")

    records, entries = run_database(data_path, read)
    assert [(r.trigger, r.status, r.filename) for r in records] == [
        (BackupTrigger.CLI, BackupStatus.SUCCESS, path.name)
    ]
    (entry,) = [item for item in entries if item.target_name == path.name]
    assert (entry.actor_type, entry.actor_name) == (AuditActorType.CLI, "cli")


def test_backup_create_exits_one_when_the_extra_copy_fails(data_path: Path, tmp_path: Path) -> None:
    """The backup exists, the command says the copy failed and exits 1 so a cron job notices."""
    outside = tmp_path / "unallowed"
    outside.mkdir()

    async def configure(factory: Factory) -> None:
        async with factory() as db:
            await save_section(
                db, "backup", BackupSettings(extra_destination=str(outside)), actor=AuditActor.cli()
            )
            await db.commit()

    run_database(data_path, configure)
    result = runner.invoke(app, ["backup", "create"])
    assert result.exit_code == 1
    assert "not copied to the extra destination" in result.output
    assert created_file(result.output).exists()
    assert list(outside.iterdir()) == []


def test_backup_create_is_refused_while_another_backup_runs(data_path: Path) -> None:
    """The lock is shared with the web server: the command exits 1 at once."""
    with hold_backup_lock(data_path / "locks"):
        result = runner.invoke(app, ["backup", "create"])
    assert result.exit_code == 1 and "another backup or restore is running" in result.output
    assert not (data_path / "backups").exists() or not list((data_path / "backups").iterdir())


def test_backup_restore_by_file_name_replaces_the_data(data_path: Path) -> None:
    """A bare name is looked up in the backup folder; data, audit and safety backup follow."""
    set_marker(data_path, "before")
    path = created_file(runner.invoke(app, ["backup", "create"]).output)
    set_marker(data_path, "after")
    result = runner.invoke(app, ["backup", "restore", path.name, "--yes"])
    assert result.exit_code == 0, result.output
    assert marker(data_path) == "before"
    assert f"restored {path.name}" in result.output
    assert "safety backup of the replaced data" in result.output
    assert "session(s) ended" in result.output
    safety = list((data_path / "backups").glob("*-pre-restore.db"))
    assert len(safety) == 1
    assert read_rows(safety[0], "SELECT value FROM app_settings WHERE key = ?", (MARKER,)) == [
        ("after",)
    ]

    async def read(factory: Factory) -> list[AuditEntry]:
        return await audit_entries(factory, "backup.restore")

    (entry,) = run_database(data_path, read)
    assert (entry.actor_name, entry.target_name) == ("cli", path.name)


def test_backup_restore_accepts_a_path_outside_the_backup_folder(
    data_path: Path, tmp_path: Path
) -> None:
    """A backup copied or downloaded elsewhere restores by its path (the recovery case)."""
    set_marker(data_path, "before")
    path = created_file(runner.invoke(app, ["backup", "create"]).output)
    downloaded = tmp_path / "downloaded" / "saved.db"
    downloaded.parent.mkdir()
    shutil.copy(path, downloaded)
    set_marker(data_path, "after")
    result = runner.invoke(app, ["backup", "restore", str(downloaded), "--yes"])
    assert result.exit_code == 0, result.output
    assert marker(data_path) == "before"


def test_backup_restore_asks_first_and_names_the_backup(data_path: Path) -> None:
    """Without --yes the question names the file and warns; no answers nothing changes."""
    set_marker(data_path, "before")
    path = created_file(runner.invoke(app, ["backup", "create"]).output)
    set_marker(data_path, "after")
    declined = runner.invoke(app, ["backup", "restore", path.name], input="n\n")
    assert declined.exit_code == 1
    assert path.name in declined.output and "lost" in declined.output
    assert "safety backup" in declined.output and "every session ends" in declined.output
    assert marker(data_path) == "after"
    accepted = runner.invoke(app, ["backup", "restore", path.name], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert marker(data_path) == "before"


def test_backup_restore_refuses_a_backup_of_another_key(data_path: Path, tmp_path: Path) -> None:
    """The message names the key; nothing is replaced and no safety backup is made."""
    path = created_file(runner.invoke(app, ["backup", "create"]).output)
    other = CryptoService(parse_master_key(generate_master_key()))
    sealed = tmp_path / "other-key.db"
    shutil.copy(path, sealed)
    with closing(sqlite3.connect(sealed)) as connection, connection:
        connection.execute(
            "UPDATE app_settings SET value = ? WHERE key = 'key_check'", (other.make_key_check(),)
        )
    set_marker(data_path, "current")
    result = runner.invoke(app, ["backup", "restore", str(sealed), "--yes"])
    assert result.exit_code == 1
    assert "different master key" in result.output
    assert marker(data_path) == "current"
    assert not list((data_path / "backups").glob("*-pre-restore.db"))


def test_backup_restore_refuses_files_that_are_not_backups(data_path: Path, tmp_path: Path) -> None:
    """A text file, a foreign database and a missing path all exit 1 with a clear message."""
    notes = tmp_path / "notes.txt"
    notes.write_text("not a database\n" * 50)
    foreign = tmp_path / "foreign.db"
    with closing(sqlite3.connect(foreign)) as connection, connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    set_marker(data_path, "current")
    for target, message in (
        (notes, "not a backup made by ENV Manager"),
        (foreign, "not a backup made by ENV Manager"),
        (tmp_path / "missing.db", "cannot be read"),
    ):
        result = runner.invoke(app, ["backup", "restore", str(target), "--yes"])
        assert result.exit_code == 1, result.output
        assert message in result.output
    assert marker(data_path) == "current"


def test_backup_commands_are_listed(data_path: Path) -> None:
    """The help shows both commands under backup."""
    result = runner.invoke(app, ["backup", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output and "restore" in result.output
