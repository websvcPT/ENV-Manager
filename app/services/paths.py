"""Allowed host paths: normalization, real path resolution, containment and mount status.

An allowed path permits itself and all its descendants. Every comparison is made on resolved
real paths (symlinks followed) and on whole path components, so ``/opt/xxx10`` is not inside
``/opt/xxx1`` and a symlink cannot lead out of the list. Callers load the list fresh from the
database and check again right before touching the filesystem or running a command.
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AllowedPath

ROOT = Path("/")
MOUNTINFO = Path("/proc/self/mountinfo")
# Kernel and device mounts exist in every container and say nothing about host bind mounts.
SYSTEM_MOUNT_ROOTS = (Path("/proc"), Path("/sys"), Path("/dev"))
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


class PathValidationError(ValueError):
    """A path cannot be used; the message is safe to show to the user."""


class PathNotAllowedError(PathValidationError):
    """A path resolves outside every allowed path."""


@dataclass(frozen=True)
class PathStatus:
    """Resolved real path of an allowed path entry with its mounted and writable status."""

    path: Path
    mounted: bool
    writable: bool


def normalize_path(raw: str) -> Path:
    """Return the absolute, lexically normalized form of ``raw`` or refuse it.

    Relative paths, NUL bytes and ``..`` components are refused instead of being collapsed, so
    a traversal attempt is an error rather than a silently different path.
    """
    text = raw.strip()
    if not text or "\x00" in text:
        raise PathValidationError("path must not be empty or contain NUL bytes")
    if not text.startswith("/"):
        raise PathValidationError("path must be absolute")
    parts = PurePosixPath(text).parts[1:]
    if ".." in parts:
        raise PathValidationError("path must not contain '..' components")
    return Path("/", *parts)


def resolve_real_path(path: str | Path) -> Path:
    """Follow every symlink and return the real absolute path, which need not exist.

    ``..`` components are resolved against the real filesystem, after symlinks are followed.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        raise PathValidationError("path must be absolute")
    try:
        return Path(os.path.realpath(candidate, strict=os.path.ALLOW_MISSING))
    except (OSError, RuntimeError) as error:
        raise PathValidationError("path cannot be resolved") from error


def is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or a descendant of it, comparing whole components."""
    return path == root or root in path.parents


def read_mount_points(mountinfo: Path = MOUNTINFO) -> list[Path]:
    """Mount points of this process as listed by the kernel, empty when unreadable."""
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    mount_points: list[Path] = []
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            unescaped = _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[4])
            mount_points.append(Path(unescaped))
    return mount_points


def _is_host_mount(real_path: Path, mount_points: Iterable[Path]) -> bool:
    """Whether ``real_path`` lies inside a mount other than the container root and system mounts."""
    for mount_point in mount_points:
        if mount_point == ROOT:
            continue
        if any(is_within(mount_point, system) for system in SYSTEM_MOUNT_ROOTS):
            continue
        if is_within(real_path, mount_point):
            return True
    return False


def path_status(real_path: Path, mount_points: Iterable[Path] | None = None) -> PathStatus:
    """Mounted and writable status of an existing real path."""
    points = read_mount_points() if mount_points is None else mount_points
    writable = os.access(real_path, os.W_OK | os.X_OK)
    return PathStatus(path=real_path, mounted=_is_host_mount(real_path, points), writable=writable)


def validate_allowed_entry(
    raw: str, *, data_path: Path, mount_points: Iterable[Path] | None = None
) -> PathStatus:
    """Validate a path before it is added to the allowed list and return its status.

    The entry must be absolute, exist as a directory and lie inside a mount. ``/`` and the
    application data volume (or any path that contains it) are refused. The returned path is the
    real path, which is what should be stored.
    """
    real_path = resolve_real_path(normalize_path(raw))
    if real_path == ROOT:
        raise PathValidationError("the filesystem root cannot be an allowed path")
    data_real = resolve_real_path(data_path)
    if is_within(real_path, data_real) or is_within(data_real, real_path):
        raise PathValidationError("the application data volume cannot be an allowed path")
    if not real_path.exists():
        raise PathValidationError("path does not exist")
    if not real_path.is_dir():
        raise PathValidationError("path is not a directory")
    status = path_status(real_path, mount_points)
    if not status.mounted:
        raise PathValidationError("path is not inside a mounted host directory")
    return status


class AllowedPaths:
    """The allowed path list, with checks that resolve every path at call time."""

    def __init__(self, roots: Iterable[str | Path]) -> None:
        """Hold the stored allowed path entries, which are real paths when they were saved."""
        self._roots = tuple(Path(root) for root in roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        """The stored entries."""
        return self._roots

    def usable_roots(self) -> list[Path]:
        """Entries that still resolve to themselves.

        An entry that has since become a symlink elsewhere would widen the list silently, so it
        is ignored until an administrator saves it again.
        """
        usable: list[Path] = []
        for root in self._roots:
            try:
                if resolve_real_path(root) == root:
                    usable.append(root)
            except PathValidationError:
                continue
        return usable

    def resolve(self, path: str | Path) -> Path:
        """Return the real path of ``path`` or raise when it is outside the allowed paths."""
        real_path = resolve_real_path(path)
        if not any(is_within(real_path, root) for root in self.usable_roots()):
            raise PathNotAllowedError(f"{path} is outside the allowed paths")
        return real_path

    def contains(self, path: str | Path) -> bool:
        """Whether ``path`` resolves inside the allowed paths."""
        try:
            self.resolve(path)
        except PathValidationError:
            return False
        return True


async def load_allowed_paths(db: AsyncSession) -> AllowedPaths:
    """Load the stored list; call it again at execution time rather than caching the result."""
    rows = await db.execute(select(AllowedPath.path).order_by(AllowedPath.path))
    return AllowedPaths(rows.scalars())
