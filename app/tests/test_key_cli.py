"""``key rotate`` and ``backup restore --key-file`` on a seeded data folder, run as the CLI does."""

import asyncio
import json
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import (
    CryptoService,
    KeyMismatchError,
    generate_master_key,
    parse_master_key,
)
from app.main import create_app
from app.services import key_rotation
from app.services.server_lock import acquire_server_lock
from app.tests.backup_helpers import read_rows
from app.tests.key_helpers import dump_database

runner = CliRunner()
MARKER = "marker.value"


@dataclass
class Setup:
    """A seeded data folder, the key it is sealed with and the folder for key files."""

    folder: Path
    keys: Path
    old_file: Path
    old_encoded: str

    @property
    def database(self) -> Path:
        """The database file."""
        return self.folder / "envmanager.db"

    @property
    def old(self) -> CryptoService:
        """The crypto service of the key the data was created with."""
        return CryptoService(parse_master_key(self.old_encoded))

    def use_key_file(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        """Point the process at a key file, as a restart with a new setting does."""
        monkeypatch.setenv("ENVMANAGER_KEY_FILE", str(path))
        get_settings.cache_clear()


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Setup]:
    """A seeded database whose key is in a file, the way a deployment runs."""
    keys = tmp_path / "keys"
    keys.mkdir()
    old_file = keys / "master.key"
    encoded = generate_master_key()
    old_file.write_text(encoded + "\n", encoding="ascii")
    old_file.chmod(0o600)
    folder = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(folder))
    monkeypatch.setenv("ENVMANAGER_KEY_FILE", str(old_file))
    monkeypatch.delenv("ENVMANAGER_KEY", raising=False)
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    ready = Setup(folder, keys, old_file, encoded)
    with closing(sqlite3.connect(ready.database)) as connection, connection:
        connection.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES ('key_check', ?, '2026-01-01')",
            (ready.old.make_key_check(),),
        )
    yield ready
    get_settings.cache_clear()


def version_texts(database: Path, crypto: CryptoService) -> dict[int, str]:
    """Every version's text, opened with the given key."""
    rows = read_rows(database, "SELECT id, content_encrypted FROM environment_versions")
    return {row[0]: crypto.decrypt_text(row[1]) for row in rows}


def start_application() -> None:
    """Start the application on the configured data folder and stop it again."""

    async def go() -> None:
        application = create_app()
        async with application.router.lifespan_context(application):
            pass

    asyncio.run(go())


def backups(folder: Path) -> list[str]:
    """The names of the backup files."""
    root = folder / "backups"
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def printed_backup(output: str) -> Path:
    """The path a command printed after ``backup created:``."""
    line = next(line for line in output.splitlines() if line.startswith("backup created: "))
    return Path(line.removeprefix("backup created: ").rsplit(" (", 1)[0])


def set_marker(database: Path, value: str) -> None:
    """Store the marker setting a restore must bring back."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, '2026-01-01') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MARKER, value),
        )


def marker(database: Path) -> str | None:
    """The marker setting as stored now."""
    rows = read_rows(database, "SELECT value FROM app_settings WHERE key = ?", (MARKER,))
    return rows[0][0] if rows else None


def rotate(setup: Setup, new_file: Path) -> Result:
    """Run ``key rotate`` towards a key file."""
    return runner.invoke(app, ["key", "rotate", "--new-key-file", str(new_file)])


def test_a_rotation_moves_every_value_to_a_new_key_file_and_the_old_key_is_refused(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = version_texts(setup.database, setup.old)
    assert before
    new_file = setup.keys / "next.key"
    result = rotate(setup, new_file)
    output = result.output
    assert result.exit_code == 0, output

    new_encoded = new_file.read_text(encoding="ascii").strip()
    assert stat.S_IMODE(new_file.stat().st_mode) == 0o600
    assert new_encoded != setup.old_encoded
    assert setup.old_encoded not in output and new_encoded not in output
    for expected in ("key rotated", "Steps left", "ENVMANAGER_KEY_FILE", "--key-file", "(created)"):
        assert expected in output
    assert "OLD key" in output

    names = backups(setup.folder)
    assert len(names) == 1 and names[0].endswith("-rotation.db")
    assert str(setup.folder / "backups" / names[0]) in output
    new = CryptoService(parse_master_key(new_encoded))
    assert version_texts(setup.database, new) == before
    records = read_rows(setup.database, "SELECT trigger, status FROM backups")
    assert records == [("rotation", "success")]
    audit = read_rows(
        setup.database,
        "SELECT actor_type, actor_name, target_type, details FROM audit_entries "
        "WHERE action = 'key.rotate'",
    )
    assert len(audit) == 1 and audit[0][:3] == ("cli", "cli", "key")
    details = json.loads(audit[0][3])
    assert details["backup"] == names[0] and details["versions"] == len(before)
    assert setup.old_encoded not in audit[0][3] and new_encoded not in audit[0][3]
    assert (setup.folder / "server.lock").read_text(encoding="utf-8") == ""

    with pytest.raises(KeyMismatchError):
        start_application()
    setup.use_key_file(monkeypatch, new_file)
    start_application()


def test_the_backup_taken_first_is_sealed_with_the_old_key(setup: Setup) -> None:
    assert rotate(setup, setup.keys / "next.key").exit_code == 0
    (name,) = backups(setup.folder)
    taken = setup.folder / "backups" / name
    rows = read_rows(taken, "SELECT value FROM app_settings WHERE key = 'key_check'")
    setup.old.verify_key_check(rows[0][0])
    assert version_texts(taken, setup.old)


def test_a_rotation_is_refused_while_a_server_holds_the_data_folder(setup: Setup) -> None:
    before = dump_database(setup.database)
    held = acquire_server_lock(setup.folder / "server.lock", "server")
    try:
        result = rotate(setup, setup.keys / "next.key")
    finally:
        held.release()
    output = result.output
    assert result.exit_code == 1 and "Stop the application first" in output
    assert "server pid=" in output
    assert dump_database(setup.database) == before
    assert backups(setup.folder) == [] and not (setup.keys / "next.key").exists()


def test_a_lock_left_behind_by_a_crashed_server_does_not_block_a_rotation(setup: Setup) -> None:
    (setup.folder / "server.lock").write_text("server pid=1 host=gone since=2026\n")
    assert rotate(setup, setup.keys / "next.key").exit_code == 0


@pytest.mark.parametrize("content", ["not a key", "dG9vIHNob3J0", ""])
def test_a_new_key_file_that_holds_no_key_is_refused_before_anything_happens(
    setup: Setup, content: str
) -> None:
    bad = setup.keys / "bad.key"
    bad.write_text(content, encoding="utf-8")
    before = dump_database(setup.database)
    result = rotate(setup, bad)
    assert result.exit_code == 1
    assert "master key" in result.output
    assert dump_database(setup.database) == before
    assert backups(setup.folder) == [] and bad.read_text(encoding="utf-8") == content


def test_the_current_key_is_refused_as_the_new_key(setup: Setup) -> None:
    before = dump_database(setup.database)
    result = rotate(setup, setup.old_file)
    assert result.exit_code == 1 and "in use now" in result.output
    assert dump_database(setup.database) == before and backups(setup.folder) == []


def test_a_new_key_file_in_a_folder_that_does_not_exist_is_refused(setup: Setup) -> None:
    before = dump_database(setup.database)
    result = rotate(setup, setup.keys / "unmounted" / "new.key")
    assert result.exit_code == 1 and "does not exist" in result.output
    assert dump_database(setup.database) == before and backups(setup.folder) == []


def test_a_rotation_with_the_wrong_current_key_is_refused_and_changes_nothing(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["backup", "create"]).exit_code == 0
    wrong = setup.keys / "wrong.key"
    wrong.write_text(generate_master_key(), encoding="ascii")
    setup.use_key_file(monkeypatch, wrong)
    before = dump_database(setup.database)
    kept = backups(setup.folder)
    result = rotate(setup, setup.keys / "next.key")
    assert result.exit_code == 1 and "does not match" in result.output
    assert dump_database(setup.database) == before
    assert backups(setup.folder) == kept and not (setup.keys / "next.key").exists()


def test_a_failure_in_the_middle_changes_nothing_and_the_next_run_reuses_the_key_file(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    async def fail_midway(*args: object) -> int:
        """Fail after the versions were rewritten, noting what other connections still see."""
        seen.append(dump_database(setup.database))
        raise RuntimeError("injected failure with SECRET-TEXT")

    original = key_rotation._rekey_settings
    monkeypatch.setattr(key_rotation, "_rekey_settings", fail_midway)
    new_file = setup.keys / "next.key"
    result = rotate(setup, new_file)
    output = result.output
    assert result.exit_code == 1 and "rolled back" in output
    assert "RuntimeError" in output and "SECRET-TEXT" not in output
    assert "still there" in output
    assert dump_database(setup.database) == seen[0]
    assert new_file.is_file() and stat.S_IMODE(new_file.stat().st_mode) == 0o600
    rotated = read_rows(
        setup.database, "SELECT count(*) FROM audit_entries WHERE action = 'key.rotate'"
    )
    assert rotated == [(0,)]
    assert (setup.folder / "server.lock").read_text(encoding="utf-8") == ""

    key_before = new_file.read_text(encoding="ascii")
    monkeypatch.setattr(key_rotation, "_rekey_settings", original)
    again = rotate(setup, new_file)
    assert again.exit_code == 0, again.output
    assert "(read)" in again.output
    assert new_file.read_text(encoding="ascii") == key_before


def test_a_backup_made_before_a_rotation_needs_the_old_key_to_be_restored(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = version_texts(setup.database, setup.old)
    set_marker(setup.database, "at the backup")
    made = runner.invoke(app, ["backup", "create"])
    assert made.exit_code == 0, made.output
    old_backup = printed_backup(made.output)
    new_file = setup.keys / "next.key"
    assert rotate(setup, new_file).exit_code == 0
    setup.use_key_file(monkeypatch, new_file)
    new = CryptoService(parse_master_key(new_file.read_text(encoding="ascii")))
    set_marker(setup.database, "after the rotation")
    live = dump_database(setup.database, without_audit=True)

    plain = runner.invoke(app, ["backup", "restore", str(old_backup), "--yes"])
    assert plain.exit_code == 1 and "different master key" in plain.output
    assert "--key-file" in plain.output
    assert dump_database(setup.database, without_audit=True) == live

    wrong = setup.keys / "wrong.key"
    wrong.write_text(generate_master_key(), encoding="ascii")
    refused = runner.invoke(
        app, ["backup", "restore", str(old_backup), "--yes", "--key-file", str(wrong)]
    )
    assert refused.exit_code == 1
    assert "not made with the key in the given key file" in refused.output
    assert dump_database(setup.database, without_audit=True) == live

    restored = runner.invoke(
        app, ["backup", "restore", str(old_backup), "--yes", "--key-file", str(setup.old_file)]
    )
    assert restored.exit_code == 0, restored.output
    assert "moved from the older key to the current key" in restored.output
    assert marker(setup.database) == "at the backup"
    assert version_texts(setup.database, new) == before
    rows = read_rows(setup.database, "SELECT value FROM app_settings WHERE key = 'key_check'")
    new.verify_key_check(rows[0][0])
    audit = read_rows(
        setup.database, "SELECT details FROM audit_entries WHERE action = 'backup.restore'"
    )
    assert json.loads(audit[-1][0])["older_key"] is True
    start_application()


def test_a_restore_names_the_current_key_as_an_ordinary_restore(setup: Setup) -> None:
    set_marker(setup.database, "at the backup")
    made = runner.invoke(app, ["backup", "create"])
    backup = printed_backup(made.output)
    set_marker(setup.database, "later")
    restored = runner.invoke(
        app, ["backup", "restore", str(backup), "--yes", "--key-file", str(setup.old_file)]
    )
    assert restored.exit_code == 0, restored.output
    assert "moved from the older key" not in restored.output
    assert marker(setup.database) == "at the backup"


def test_an_unreadable_key_file_for_a_restore_is_refused(setup: Setup) -> None:
    made = runner.invoke(app, ["backup", "create"])
    result = runner.invoke(
        app,
        [
            "backup",
            "restore",
            str(printed_backup(made.output)),
            "--yes",
            "--key-file",
            str(setup.keys / "missing.key"),
        ],
    )
    assert result.exit_code == 1 and "cannot be read" in result.output
