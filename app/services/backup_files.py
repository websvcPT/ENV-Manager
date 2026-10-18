"""Reading, listing and removing the backup files in the backup folder, without following links.

Every operation takes a file name that must look like one of our own backup names (so it cannot
hold a path separator or climb out of the folder), opens the folder once with
``O_DIRECTORY | O_NOFOLLOW`` and works relative to that descriptor. A symlink in place of a backup
file, or in place of the folder, is never followed: a swapped file cannot make a download or a
prune reach another place.
"""

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.services.backup import parse_backup_name

CHUNK_BYTES = 256 * 1024


class BackupFileMissingError(Exception):
    """The backup file is not there, is not a plain file, or its name is not one of ours."""


@dataclass(frozen=True)
class OpenedBackup:
    """An open backup file: the descriptor (the caller closes it) and its size in bytes."""

    descriptor: int
    size: int


@contextmanager
def _open_folder(backup_dir: Path) -> Iterator[int]:
    """The backup folder opened as a directory descriptor, closed at the end of the block."""
    try:
        descriptor = os.open(
            backup_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise BackupFileMissingError from error
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _require_name(name: str) -> None:
    """Refuse a name that is not the name of one of our backup files."""
    if parse_backup_name(name) is None:
        raise BackupFileMissingError


def backup_file_size(backup_dir: Path, name: str) -> int | None:
    """The size of the backup file, None when it is missing, not a plain file or not ours."""
    try:
        _require_name(name)
        with _open_folder(backup_dir) as folder:
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except BackupFileMissingError, OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def list_backup_files(backup_dir: Path) -> dict[str, int]:
    """Our own backup files in the folder with their sizes; anything else is ignored."""
    found: dict[str, int] = {}
    try:
        with os.scandir(backup_dir) as entries:
            for entry in entries:
                if parse_backup_name(entry.name) is None:
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                found[entry.name] = entry.stat(follow_symlinks=False).st_size
    except OSError:
        return {}
    return found


def open_backup_file(backup_dir: Path, name: str) -> OpenedBackup:
    """Open a backup file for reading. Raises ``BackupFileMissingError`` when it cannot be."""
    _require_name(name)
    with _open_folder(backup_dir) as folder:
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder
            )
        except OSError as error:
            raise BackupFileMissingError from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BackupFileMissingError
    except BaseException:
        os.close(descriptor)
        raise
    return OpenedBackup(descriptor, info.st_size)


async def stream_backup(opened: OpenedBackup) -> AsyncIterator[bytes]:
    """The content of an opened backup in chunks, read off the event loop; closes the file."""
    try:
        while chunk := await asyncio.to_thread(os.read, opened.descriptor, CHUNK_BYTES):
            yield chunk
    finally:
        os.close(opened.descriptor)


def remove_backup_file(backup_dir: Path, name: str) -> bool:
    """Remove one backup file; True when it is gone afterwards, False when it could not be removed.

    A file that is already absent counts as removed. Only the file itself is touched, never the
    target of a link at its name.
    """
    try:
        _require_name(name)
        with _open_folder(backup_dir) as folder:
            try:
                os.unlink(name, dir_fd=folder)
            except FileNotFoundError:
                return True
    except BackupFileMissingError, OSError:
        return False
    return True
