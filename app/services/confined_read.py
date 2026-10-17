"""Reading and resolving files below a deployment root, confined to the allowed paths.

Every path is resolved to its real location now, and the file is opened without following a
final symlink; the opened descriptor is then checked again (regular file, real path inside the
deployment root and the allowed paths), so a symlink swapped in after the first check cannot
lead the read elsewhere.
"""

import errno
import os
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath

from app.services.paths import AllowedPaths, PathValidationError, is_within


class PathProblem(StrEnum):
    """Why a path below a deployment root cannot be used."""

    NOT_RELATIVE = "not_relative"
    ROOT_OUTSIDE = "root_outside"
    LEAVES_ROOT = "leaves_root"
    NOT_A_FILE = "not_a_file"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    NOT_UTF8 = "not_utf8"


class ConfinedReadError(Exception):
    """A path or file cannot be used; ``problem`` says why and the message is safe to show."""

    def __init__(self, problem: PathProblem, message: str) -> None:
        """Hold the reason and its message."""
        super().__init__(message)
        self.problem = problem


def split_relative(raw: str, label: str) -> PurePosixPath:
    """The relative path typed by the user, refused when empty, absolute or containing ``..``."""
    text = raw.strip()
    relative = PurePosixPath(text)
    if not text or "\x00" in text or relative.is_absolute() or ".." in relative.parts:
        raise ConfinedReadError(
            PathProblem.NOT_RELATIVE, f"{label} path must be a relative path inside the deployment"
        )
    return relative


def resolve_below_root(
    allowed: AllowedPaths, deployment_root: str, relative: PurePosixPath, label: str
) -> tuple[Path, Path]:
    """The real deployment root and the real path of ``relative`` below it; the file may be absent.

    Both must lie inside the allowed paths, and the file inside the root. Raises
    ``ConfinedReadError`` otherwise.
    """
    try:
        root = allowed.resolve(deployment_root)
    except PathValidationError as error:
        raise ConfinedReadError(PathProblem.ROOT_OUTSIDE, str(error)) from error
    try:
        real_path = allowed.resolve(root / relative)
    except PathValidationError as error:
        raise ConfinedReadError(PathProblem.LEAVES_ROOT, str(error)) from error
    if not is_within(real_path, root) or real_path == root:
        raise ConfinedReadError(PathProblem.LEAVES_ROOT, f"{label} path leaves the deployment root")
    return root, real_path


def read_confined_text(
    allowed: AllowedPaths,
    deployment_root: str,
    relative_path: str,
    *,
    label: str,
    max_bytes: int,
) -> str:
    """Read the regular UTF-8 file at ``relative_path`` below the deployment root as text.

    ``allowed`` must be freshly loaded by the caller. Raises ``ConfinedReadError`` for every
    failure.
    """
    relative = split_relative(relative_path, label)
    root, real_path = resolve_below_root(allowed, deployment_root, relative, label)
    not_a_file = ConfinedReadError(
        PathProblem.NOT_A_FILE, f"{label} file does not exist or is not a regular file"
    )
    try:
        descriptor = os.open(real_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError, NotADirectoryError, IsADirectoryError:
        raise not_a_file from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise not_a_file from None
        raise ConfinedReadError(PathProblem.UNREADABLE, f"{label} file cannot be read") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise not_a_file
        opened = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not is_within(opened, root) or not allowed.contains(opened):
            raise ConfinedReadError(
                PathProblem.LEAVES_ROOT, f"{label} path leaves the deployment root"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    except OSError as error:
        raise ConfinedReadError(PathProblem.UNREADABLE, f"{label} file cannot be read") from error
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:
        raise ConfinedReadError(PathProblem.TOO_LARGE, f"{label} file is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfinedReadError(
            PathProblem.NOT_UTF8, f"{label} file is not valid UTF-8 text"
        ) from error
