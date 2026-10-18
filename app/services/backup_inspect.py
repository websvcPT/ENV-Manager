"""Checking that a file is a backup this application can restore, before anything is replaced.

The check runs on a private copy of the candidate, never on the file the person named, so the
file cannot change between the check and the restore. A backup is accepted only if it is an
intact SQLite database that this application made (its migration record names a revision this
version knows) and whose key check record opens with the master key in use now. A backup sealed
with another key is refused with a plain message: restoring it would leave every stored value
unreadable. The copy is opened read-only with an untrusted schema, and only fixed queries run.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from alembic.script import ScriptDirectory

from app.core.crypto import CryptoError, CryptoService, KeyMismatchError
from app.core.migrations import build_config
from app.models.app_setting import KEY_CHECK_SETTING

SQLITE_HEADER = b"SQLite format 3\x00"


class RestoreProblem(StrEnum):
    """Why a backup is refused or a restore did not happen."""

    UNREADABLE = "unreadable"
    NOT_A_BACKUP = "not_a_backup"
    DAMAGED = "damaged"
    WRONG_KEY = "wrong_key"
    NEWER = "newer"
    BUSY = "busy"
    SAFETY_FAILED = "safety_failed"
    FAILED = "failed"


RESTORE_MESSAGES = {
    RestoreProblem.UNREADABLE: "The backup file cannot be read.",
    RestoreProblem.NOT_A_BACKUP: "This file is not a backup made by ENV Manager.",
    RestoreProblem.DAMAGED: "The backup failed its integrity check and cannot be restored.",
    RestoreProblem.WRONG_KEY: (
        "This backup was made with a different master key than the one this application "
        "runs with, so its values could not be read after a restore. Nothing was changed."
    ),
    RestoreProblem.NEWER: (
        "This backup was made by a newer version of ENV Manager. Update the application first."
    ),
    RestoreProblem.BUSY: "Another backup or restore is running. Try again in a moment.",
    RestoreProblem.SAFETY_FAILED: (
        "A safety backup of the current data could not be made, so nothing was restored."
    ),
    RestoreProblem.FAILED: "The restore failed and the current data was not changed.",
}


class RestoreRefusedError(Exception):
    """A restore did not happen; ``problem`` says why and the message is safe to show."""

    def __init__(self, problem: RestoreProblem) -> None:
        """Hold the reason and its fixed message."""
        super().__init__(RESTORE_MESSAGES[problem])
        self.problem = problem


@dataclass(frozen=True)
class BackupInspection:
    """What a checked backup is: its migration revision and whether it is older than this app."""

    revision: str
    needs_upgrade: bool


def _read_only(path: Path) -> sqlite3.Connection:
    """The file opened read-only, with an untrusted schema and no way to write."""
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _check_revision(revision: str) -> bool:
    """Whether the backup is older than this version; refuses an unknown revision as newer."""
    script = ScriptDirectory.from_config(build_config())
    try:
        known = script.get_revision(revision) is not None
    except Exception:
        known = False
    if not known:
        raise RestoreRefusedError(RestoreProblem.NEWER)
    return revision != script.get_current_head()


def inspect_backup(path: Path, crypto: CryptoService) -> BackupInspection:
    """Check a private copy of a backup and describe it, or raise ``RestoreRefusedError``."""
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as error:
        raise RestoreRefusedError(RestoreProblem.UNREADABLE) from error
    if header != SQLITE_HEADER:
        raise RestoreRefusedError(RestoreProblem.NOT_A_BACKUP)
    try:
        with closing(_read_only(path)) as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RestoreRefusedError(RestoreProblem.DAMAGED)
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"alembic_version", "app_settings"} <= tables:
                raise RestoreRefusedError(RestoreProblem.NOT_A_BACKUP)
            revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            record = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (KEY_CHECK_SETTING,)
            ).fetchone()
    except sqlite3.DatabaseError as error:
        raise RestoreRefusedError(RestoreProblem.NOT_A_BACKUP) from error
    if len(revisions) != 1 or record is None:
        raise RestoreRefusedError(RestoreProblem.NOT_A_BACKUP)
    try:
        crypto.verify_key_check(record[0])
    except KeyMismatchError as error:
        raise RestoreRefusedError(RestoreProblem.WRONG_KEY) from error
    except CryptoError as error:
        raise RestoreRefusedError(RestoreProblem.NOT_A_BACKUP) from error
    revision = str(revisions[0][0])
    return BackupInspection(revision, _check_revision(revision))
