"""Reading, backing up and atomically writing deploy target files without following symlinks.

A target path is resolved to a real directory and a file name first (``locate_target``), then
every file operation opens that directory once with ``O_DIRECTORY | O_NOFOLLOW`` and checks the
real path of the opened descriptor (from ``/proc/self/fd``) against the deployment root and the
allowed paths. Reading, backing up, creating the temporary file and renaming it over the target
all happen relative to that descriptor, so a directory or file swapped for a symlink after the
path check cannot lead a write outside the allowed paths. A target that is a symlink is refused.

Nothing here holds a file's content longer than a call, and no message names a host path or
repeats operating system error text, because the caller may reach a single environment only.
"""

import errno
import grp
import os
import pwd
import secrets
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from app.services.confined_read import ConfinedReadError, split_relative
from app.services.paths import AllowedPaths, PathValidationError, is_within

MAX_TARGET_BYTES = 4 * 1024 * 1024
DEFAULT_FILE_MODE = 0o600
BACKUP_MODE = 0o600
_TEMP_NAME_LIMIT = 100

ChangeOwner = Callable[[int, int, int], None]
Content = bytes | BinaryIO
_COPY_CHUNK = 1024 * 1024


class TargetProblem(StrEnum):
    """Why a target file cannot be read or written."""

    NOT_RELATIVE = "not_relative"
    ROOT_OUTSIDE = "root_outside"
    LEAVES_ROOT = "leaves_root"
    NOT_A_FOLDER = "not_a_folder"
    FOLDER_MISSING = "folder_missing"
    SYMLINK = "symlink"
    NOT_A_FILE = "not_a_file"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    WRITE_FAILED = "write_failed"
    BACKUP_FAILED = "backup_failed"
    OWNER_UNKNOWN = "owner_unknown"
    OWNER_DENIED = "owner_denied"


PROBLEM_MESSAGES = {
    TargetProblem.NOT_RELATIVE: "The target path must be a relative path inside the deployment.",
    TargetProblem.ROOT_OUTSIDE: "The deployment is outside the allowed paths.",
    TargetProblem.LEAVES_ROOT: "The target path leaves the deployment folder.",
    TargetProblem.NOT_A_FOLDER: "The target folder is not a plain folder.",
    TargetProblem.FOLDER_MISSING: "The target folder does not exist.",
    TargetProblem.SYMLINK: "The target is a symbolic link and is not replaced.",
    TargetProblem.NOT_A_FILE: "The target is not a regular file.",
    TargetProblem.UNREADABLE: "The target file cannot be read.",
    TargetProblem.TOO_LARGE: "The target file is too large to compare.",
    TargetProblem.WRITE_FAILED: "The file could not be written.",
    TargetProblem.BACKUP_FAILED: "The backup could not be written, the target was not changed.",
    TargetProblem.OWNER_UNKNOWN: "The configured owner or group does not exist.",
    TargetProblem.OWNER_DENIED: (
        "The owner or group could not be set: the app has no privilege to change ownership. "
        "The target was not changed."
    ),
}


class TargetError(Exception):
    """A target file cannot be used; ``problem`` says why and the message is safe to show."""

    def __init__(self, problem: TargetProblem) -> None:
        """Hold the reason and its fixed message."""
        super().__init__(PROBLEM_MESSAGES[problem])
        self.problem = problem


@dataclass(frozen=True)
class TargetLocation:
    """A target resolved to the real deployment root, the real folder and the file name.

    ``relative`` is the path below the root as configured, which is what results record.
    """

    root: Path
    directory: Path
    name: str
    relative: str


@dataclass(frozen=True)
class Ownership:
    """Numeric owner and group to give a written file; -1 keeps the current value."""

    uid: int
    gid: int


@dataclass(frozen=True)
class BackupRequest:
    """The content of the target before it is replaced and the stamp for the backup name."""

    content: bytes
    stamp: str


def locate_target(allowed: AllowedPaths, deployment_root: str, target_path: str) -> TargetLocation:
    """Resolve the target below the deployment root; the file itself is not followed.

    Only the folder is resolved (symlinks followed, allowed paths loaded by the caller), and it
    must lie inside the real root. The final name stays as configured so a symlink in its place
    is seen later instead of being resolved away. Raises ``TargetError``.
    """
    try:
        relative = split_relative(target_path, "target")
    except ConfinedReadError as error:
        raise TargetError(TargetProblem.NOT_RELATIVE) from error
    try:
        root = allowed.resolve(deployment_root)
    except PathValidationError as error:
        raise TargetError(TargetProblem.ROOT_OUTSIDE) from error
    try:
        directory = allowed.resolve(root / relative.parent)
    except PathValidationError as error:
        raise TargetError(TargetProblem.LEAVES_ROOT) from error
    if not is_within(directory, root) or relative.name in {"", ".", ".."}:
        raise TargetError(TargetProblem.LEAVES_ROOT)
    return TargetLocation(root, directory, relative.name, relative.as_posix())


@contextmanager
def open_directory(location: TargetLocation, allowed: AllowedPaths) -> Iterator[int]:
    """Open the target folder without following a symlink and check where it really is.

    The descriptor's real path must lie inside the deployment root and the allowed paths, which
    catches a symlink swapped in for a folder further up the path. Raises ``TargetError``.
    """
    try:
        descriptor = os.open(
            location.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except FileNotFoundError as error:
        raise TargetError(TargetProblem.FOLDER_MISSING) from error
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise TargetError(TargetProblem.NOT_A_FOLDER) from error
        raise TargetError(TargetProblem.UNREADABLE) from error
    try:
        opened = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not is_within(opened, location.root) or not allowed.contains(opened):
            raise TargetError(TargetProblem.LEAVES_ROOT)
        yield descriptor
    finally:
        os.close(descriptor)


def _read_file(directory_fd: int, name: str) -> bytes | None:
    """The bytes of the regular file in the folder, None when there is none."""
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise TargetError(TargetProblem.SYMLINK) from error
        raise TargetError(TargetProblem.UNREADABLE) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TargetError(TargetProblem.NOT_A_FILE)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_TARGET_BYTES + 1)
    except OSError as error:
        raise TargetError(TargetProblem.UNREADABLE) from error
    finally:
        os.close(descriptor)
    if len(data) > MAX_TARGET_BYTES:
        raise TargetError(TargetProblem.TOO_LARGE)
    return data


def read_target_file(location: TargetLocation, allowed: AllowedPaths) -> bytes | None:
    """The current content of the target, None when the file does not exist.

    A symlink, a folder or another kind of file in its place raises ``TargetError``, as does a
    file larger than ``MAX_TARGET_BYTES``.
    """
    with open_directory(location, allowed) as directory_fd:
        return _read_file(directory_fd, location.name)


def _refuse_unusable_target(directory_fd: int, name: str) -> None:
    """Refuse a target that is a symlink or not a regular file; an absent target is fine."""
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise TargetError(TargetProblem.UNREADABLE) from error
    if stat.S_ISLNK(info.st_mode):
        raise TargetError(TargetProblem.SYMLINK)
    if not stat.S_ISREG(info.st_mode):
        raise TargetError(TargetProblem.NOT_A_FILE)


def _remove(directory_fd: int, name: str) -> None:
    """Remove a file of the folder, ignoring a failure to do so."""
    with suppress(OSError):
        os.unlink(name, dir_fd=directory_fd)


def _create_file(directory_fd: int, name: str, data: Content, mode: int) -> int:
    """Create a new file with the data and mode, and return its open descriptor.

    ``data`` is bytes or a binary stream that is copied in chunks, so a large file is never held
    in memory. The file is created exclusively and without following a symlink, so a file or
    link already at the name makes this fail and is left alone. It is created with mode 0600 and
    given ``mode`` afterwards, so its content is never readable more widely than the final mode
    allows. A failure after the file exists removes it again; the caller closes the descriptor it
    gets.
    """
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            if isinstance(data, bytes):
                handle.write(data)
            else:
                shutil.copyfileobj(data, handle, _COPY_CHUNK)
            handle.flush()
        os.fchmod(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        _remove(directory_fd, name)
        raise
    return descriptor


def _write_backup(directory_fd: int, name: str, backup: BackupRequest) -> str:
    """Write the backup next to the target and return its name; mode 0600, no symlink followed."""
    backup_name = f"{name}.bak-{backup.stamp}-{secrets.token_hex(2)}"
    try:
        descriptor = _create_file(directory_fd, backup_name, backup.content, BACKUP_MODE)
    except OSError as error:
        raise TargetError(TargetProblem.BACKUP_FAILED) from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        _remove(directory_fd, backup_name)
        raise TargetError(TargetProblem.BACKUP_FAILED) from error
    finally:
        os.close(descriptor)
    return backup_name


def _apply_owner(change_owner: ChangeOwner, descriptor: int, ownership: Ownership) -> None:
    """Give the file its owner and group, refusing with a clear problem when that is denied."""
    try:
        change_owner(descriptor, ownership.uid, ownership.gid)
    except PermissionError as error:
        raise TargetError(TargetProblem.OWNER_DENIED) from error
    except OSError as error:
        raise TargetError(TargetProblem.WRITE_FAILED) from error


def _replace_target(
    directory_fd: int,
    name: str,
    data: Content,
    mode: int,
    ownership: Ownership | None,
    change_owner: ChangeOwner,
) -> None:
    """Write a temporary file in the folder and rename it over the target.

    The temporary file gets its owner and mode and is flushed to disk before the rename; any
    failure removes it and leaves the target as it was.
    """
    temp_name = f".{name[:_TEMP_NAME_LIMIT]}.{secrets.token_hex(6)}.tmp"
    try:
        descriptor = _create_file(directory_fd, temp_name, data, 0o600)
    except OSError as error:
        raise TargetError(TargetProblem.WRITE_FAILED) from error
    try:
        try:
            if ownership is not None:
                _apply_owner(change_owner, descriptor, ownership)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except OSError as error:
            raise TargetError(TargetProblem.WRITE_FAILED) from error
        finally:
            os.close(descriptor)
        try:
            os.rename(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except OSError as error:
            raise TargetError(TargetProblem.WRITE_FAILED) from error
    except BaseException:
        _remove(directory_fd, temp_name)
        raise


def write_target_file(
    location: TargetLocation,
    allowed: AllowedPaths,
    data: Content,
    *,
    mode: int = DEFAULT_FILE_MODE,
    ownership: Ownership | None = None,
    backup: BackupRequest | None = None,
    change_owner: ChangeOwner = os.fchown,
) -> str | None:
    """Replace the target with ``data`` atomically and return the name of the backup, if any.

    ``data`` is bytes or a binary stream (read from its current position to the end). The folder
    is opened once; the backup (when asked for), the temporary file and the rename all
    use that descriptor. The temporary file is created exclusively without following links, filled,
    given its owner and mode and flushed to disk before it is renamed over the target, so readers
    see the old or the new file, never a partial one. A failure leaves the target as it was and
    removes the temporary file and the backup. Raises ``TargetError``.
    """
    with open_directory(location, allowed) as directory_fd:
        _refuse_unusable_target(directory_fd, location.name)
        backup_name = _write_backup(directory_fd, location.name, backup) if backup else None
        try:
            _replace_target(directory_fd, location.name, data, mode, ownership, change_owner)
        except BaseException:
            if backup_name is not None:
                _remove(directory_fd, backup_name)
            raise
        with suppress(OSError):
            os.fsync(directory_fd)
    return backup_name


def _account_id(value: str, lookup: Callable[[str], int]) -> int:
    """A numeric id as typed, or the id of the named account."""
    if value.isascii() and value.isdigit():
        return int(value)
    try:
        return lookup(value)
    except KeyError as error:
        raise TargetError(TargetProblem.OWNER_UNKNOWN) from error


def resolve_ownership(owner: str | None, group: str | None) -> Ownership | None:
    """The numeric owner and group for configured names or ids, None when nothing is configured.

    An empty owner or group keeps the file's own value for that part. Raises ``TargetError`` for
    a name that does not exist in this container.
    """
    if not owner and not group:
        return None
    uid = _account_id(owner, lambda name: pwd.getpwnam(name).pw_uid) if owner else -1
    gid = _account_id(group, lambda name: grp.getgrnam(name).gr_gid) if group else -1
    return Ownership(uid, gid)


def parse_mode(mode: str | None) -> int:
    """The permission bits of a stored mode such as ``0640``; the default is 0600."""
    if not mode:
        return DEFAULT_FILE_MODE
    return int(mode, 8) & 0o777
