"""Consistent SQLite snapshots taken with the online backup API, private on disk.

A snapshot is made while the application keeps running: SQLite's online backup copies one
consistent state of the database even while other connections write. The copy is written into a
folder that only the application user can enter (mode 0700), as a file that only it can read
(mode 0600, set when the file is created so there is no moment with wider permissions), under a
temporary name that is renamed once the copy is complete. The copy is switched out of WAL mode, so
it is one self-contained file that can be downloaded or copied elsewhere. It holds the database as
it is: values are encrypted, and the master key never enters the database.

Nothing here puts a path or operating system error text into an exception message, because the
message is stored in the backup record and shown to the superadmin.
"""

import asyncio
import errno
import os
import re
import sqlite3
import stat
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

BACKUP_PREFIX = "envmanager-"
BACKUP_SUFFIX = ".db"
FOLDER_MODE = 0o700
FILE_MODE = 0o600
BUSY_TIMEOUT_SECONDS = 30
STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_NAME_PATTERN = re.compile(r"^envmanager-(\d{8}T\d{12}Z)-([a-z]+(?:-[a-z]+)*)\.db$")


class BackupProblem(StrEnum):
    """Why a snapshot could not be written."""

    FOLDER_UNUSABLE = "folder_unusable"
    DISK_FULL = "disk_full"
    COPY_FAILED = "copy_failed"


PROBLEM_MESSAGES = {
    BackupProblem.FOLDER_UNUSABLE: "The backup folder cannot be used.",
    BackupProblem.DISK_FULL: "There is no space left for the backup.",
    BackupProblem.COPY_FAILED: "The database could not be copied.",
}


class BackupSnapshotError(Exception):
    """A snapshot could not be written; ``problem`` says why and the message is safe to store."""

    def __init__(self, problem: BackupProblem) -> None:
        """Hold the reason and its fixed message."""
        super().__init__(PROBLEM_MESSAGES[problem])
        self.problem = problem


@dataclass(frozen=True)
class ParsedName:
    """What the name of a backup file says: when it was taken and why."""

    moment: datetime
    reason: str


def backup_file_name(reason: str, moment: datetime | None = None) -> str:
    """Build a sortable backup file name that records why the backup was taken."""
    stamp = (moment or datetime.now(UTC)).strftime(STAMP_FORMAT)
    return f"{BACKUP_PREFIX}{stamp}-{reason}{BACKUP_SUFFIX}"


def parse_backup_name(name: str) -> ParsedName | None:
    """The moment and reason in a backup file name, None for any other name."""
    found = _NAME_PATTERN.match(name)
    if found is None:
        return None
    moment = datetime.strptime(found.group(1), STAMP_FORMAT).replace(tzinfo=UTC)
    return ParsedName(moment, found.group(2))


def ensure_backup_folder(folder: Path) -> None:
    """Create the backup folder with mode 0700, or tighten an existing one to it.

    A symlink or a file in its place is refused, so a swapped folder cannot redirect backups.
    """
    try:
        folder.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise BackupSnapshotError(BackupProblem.FOLDER_UNUSABLE)
        if stat.S_IMODE(info.st_mode) != FOLDER_MODE:
            folder.chmod(FOLDER_MODE)
    except OSError as error:
        raise BackupSnapshotError(BackupProblem.FOLDER_UNUSABLE) from error


def _create_private_file(path: Path) -> None:
    """Create an empty file with mode 0600, refusing an existing file or a symlink."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE
    )
    os.close(descriptor)


def copy_database(source: Path, destination: Path, *, read_only: bool = False) -> None:
    """Copy a database into a new private file with SQLite's online backup.

    The copy is one consistent state of the source, taken in a single step, so writers on the
    source are never blocked and never leave a half written transaction in it. ``read_only``
    opens the source so that it cannot be written at all, for a file that is not trusted. The
    copy is not WAL, so it needs no side files. The destination must not exist yet.
    """
    _create_private_file(destination)
    if read_only:
        source_url = f"file:{quote(str(source))}?mode=ro"
        source_connection = sqlite3.connect(source_url, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    else:
        source_connection = sqlite3.connect(source, timeout=BUSY_TIMEOUT_SECONDS)
    with closing(source_connection), closing(sqlite3.connect(destination)) as target_connection:
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.backup(target_connection)
        target_connection.execute("PRAGMA journal_mode=DELETE").fetchall()


def _problem_of(error: OSError | sqlite3.Error) -> BackupProblem:
    """The fixed reason for an error raised while copying."""
    if isinstance(error, OSError):
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            return BackupProblem.DISK_FULL
        if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS, errno.ELOOP, errno.ENOENT}:
            return BackupProblem.FOLDER_UNUSABLE
        return BackupProblem.COPY_FAILED
    if "disk is full" in str(error):
        return BackupProblem.DISK_FULL
    return BackupProblem.COPY_FAILED


def write_snapshot(source: Path, destination: Path) -> None:
    """Snapshot ``source`` into the new private file ``destination``, whole or not at all.

    The copy is written under a temporary name next to the destination and given its final name
    when it is complete, so a file with the final name is always a finished backup. The final name
    is linked, not renamed over, so an existing backup is never replaced. A failure removes the
    temporary file and raises ``BackupSnapshotError`` with a fixed reason.
    """
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        if not source.is_file():
            raise BackupSnapshotError(BackupProblem.COPY_FAILED)
        copy_database(source, temporary)
        os.link(temporary, destination)
        temporary.unlink()
    except BaseException as error:
        with suppress(OSError):
            temporary.unlink()
        if isinstance(error, OSError | sqlite3.Error):
            raise BackupSnapshotError(_problem_of(error)) from error
        raise


async def create_backup(
    source: Path, backup_dir: Path, reason: str, *, moment: datetime | None = None
) -> Path:
    """Snapshot ``source`` into ``backup_dir`` and return the new file path.

    Raises ``BackupSnapshotError`` when the folder cannot be used or the copy fails.
    """
    ensure_backup_folder(backup_dir)
    destination = backup_dir / backup_file_name(reason, moment)
    await asyncio.to_thread(write_snapshot, source, destination)
    return destination
