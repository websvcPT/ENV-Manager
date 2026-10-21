"""Directory listings for the path picker, limited to the allowed paths.

Nothing outside the allowed paths is ever listed: the starting point is validated against the
list, and every child is resolved and dropped when its real location leaves the list.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from app.services.paths import AllowedPaths, PathValidationError

ROOT_CHILD_LIMIT = 50
DIRECTORY_LIMIT = 200


@dataclass(frozen=True)
class BrowseEntry:
    """One directory in the picker list. ``is_root`` marks an allowed path entry itself."""

    path: str
    is_root: bool = False


@dataclass(frozen=True)
class BrowseView:
    """What the picker shows: the allowed roots with their children, or one directory.

    ``current`` is the directory being browsed (None at the top). ``parent`` is the directory one
    level up when it is still inside the allowed paths; inside a root it is None and the way up
    is the top list.
    """

    current: str | None
    parent: str | None
    entries: tuple[BrowseEntry, ...]
    truncated: bool = False


def _child_directories(
    directory: Path, allowed: AllowedPaths, limit: int
) -> tuple[list[Path], bool]:
    """Real child directories that stay inside the allowed paths, sorted alphabetically ignoring
    case, with a cut-off flag."""
    try:
        with os.scandir(directory) as scanner:
            names = sorted(
                (entry.name for entry in scanner if entry.is_dir(follow_symlinks=False)),
                key=lambda name: (name.casefold(), name),
            )
    except OSError:
        return [], False
    children: list[Path] = []
    for name in names:
        child = directory / name
        if allowed.contains(child):
            children.append(child)
    return children[:limit], len(children) > limit


def browse_top(allowed: AllowedPaths) -> BrowseView:
    """The allowed roots, each followed by its first child directories."""
    entries: list[BrowseEntry] = []
    truncated = False
    for root in allowed.usable_roots():
        entries.append(BrowseEntry(str(root), is_root=True))
        children, cut = _child_directories(root, allowed, ROOT_CHILD_LIMIT)
        entries.extend(BrowseEntry(str(child)) for child in children)
        truncated = truncated or cut
    return BrowseView(current=None, parent=None, entries=tuple(entries), truncated=truncated)


def browse_directory(allowed: AllowedPaths, raw: str) -> BrowseView:
    """The child directories of ``raw``, which must resolve inside the allowed paths."""
    real_path = allowed.resolve(raw)
    if not real_path.is_dir():
        raise PathValidationError("not a directory")
    children, truncated = _child_directories(real_path, allowed, DIRECTORY_LIMIT)
    parent = real_path.parent
    return BrowseView(
        current=str(real_path),
        parent=str(parent) if parent != real_path and allowed.contains(parent) else None,
        entries=tuple(BrowseEntry(str(child)) for child in children),
        truncated=truncated,
    )
