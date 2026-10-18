"""The allowed path list of the Settings screen: status rows, live checks, add and remove.

Only the superadmin edits the list. An entry is validated when it is added (absolute, normalized,
a folder that exists inside a host mount, never the filesystem root or the data volume) and stored
as its real path. Removing an entry deletes nothing on disk: deployments that no other entry
covers are flagged as outside the allowed paths until the path is allowed again. Every change is
audited and announced through the settings notification seam.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AllowedPath, AuditResult, Deployment
from app.services.audit import AuditActor, record_actor_audit
from app.services.paths import (
    AllowedPaths,
    PathStatus,
    PathValidationError,
    host_mount_of,
    is_within,
    path_status,
    resolve_real_path,
    validate_allowed_entry,
)
from app.services.settings_events import SettingsChanged, SettingsNotifier, announce

ACTION_ADD = "allowed_path.add"
ACTION_REMOVE = "allowed_path.remove"
TARGET_TYPE = "allowed_path"
AREA = "allowed_paths"
DUPLICATE_MESSAGE = "This path is already in the list."
MISSING_NOTE = "The folder no longer exists."
MOVED_NOTE = "The path now resolves elsewhere, so it is ignored until it is added again."


class AllowedPathError(Exception):
    """An entry was refused or cannot be found; the message is safe to show."""


STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_MOVED = "moved"


@dataclass(frozen=True)
class AllowedPathRow:
    """One entry with its present status.

    ``state`` is ``ok``, ``missing`` (the folder is gone) or ``moved`` (the path now resolves
    elsewhere and is ignored); ``mounted`` and ``writable`` only mean something when it is ``ok``.
    ``deployments`` counts the deployments under the entry and ``note`` explains a missing or
    moved entry.
    """

    id: int
    path: str
    state: str
    mounted: bool
    writable: bool
    deployments: int
    note: str | None


@dataclass(frozen=True)
class CheckLine:
    """One line of the live check of a typed path: a passed check or the reason it failed."""

    ok: bool
    text: str


@dataclass(frozen=True)
class EntryCheck:
    """The outcome of checking a typed entry. ``real_path`` is set only when it may be added."""

    lines: tuple[CheckLine, ...]
    real_path: Path | None = None

    @property
    def ok(self) -> bool:
        """Whether the entry may be added."""
        return self.real_path is not None

    @property
    def message(self) -> str | None:
        """The first failed line, shown as the field error."""
        return next((line.text for line in self.lines if not line.ok), None)


def _sentence(reason: str) -> str:
    """A refusal reason as a sentence."""
    return reason[0].upper() + reason[1:] + "."


def check_entry(
    raw: str,
    *,
    data_path: Path,
    existing: Iterable[str] = (),
    mount_points: Iterable[Path] | None = None,
) -> EntryCheck:
    """Check a typed entry the way the add action does and describe each check.

    An empty input has no lines, which shows the neutral hint. A path already in the list is
    refused. An entry on a read-only mount is accepted and said to be read-only.
    """
    if not raw.strip():
        return EntryCheck(lines=())
    points = None if mount_points is None else list(mount_points)
    try:
        status = validate_allowed_entry(raw, data_path=data_path, mount_points=points)
    except PathValidationError as error:
        return EntryCheck(lines=(CheckLine(False, _sentence(str(error))),))
    if str(status.path) in set(existing):
        return EntryCheck(lines=(CheckLine(False, DUPLICATE_MESSAGE),))
    mount = host_mount_of(status.path, points)
    access = "writable" if status.writable else "read-only"
    return EntryCheck(
        lines=(
            CheckLine(True, "Absolute and normalized"),
            CheckLine(True, "Exists"),
            CheckLine(True, f"Inside the mount {mount}, {access}"),
        ),
        real_path=status.path,
    )


async def stored_paths(db: AsyncSession) -> list[AllowedPath]:
    """Every entry, in path order."""
    return list((await db.execute(select(AllowedPath).order_by(AllowedPath.path))).scalars())


async def _deployment_roots(db: AsyncSession) -> list[Path]:
    """The root path of every deployment."""
    return [Path(root) for root in (await db.execute(select(Deployment.root_path))).scalars()]


def _row(
    entry: AllowedPath, roots: list[Path], mount_points: Iterable[Path] | None
) -> AllowedPathRow:
    """The status row of an entry."""
    path = Path(entry.path)
    count = sum(1 for root in roots if is_within(root, path))
    try:
        resolved = resolve_real_path(path)
    except PathValidationError:
        return AllowedPathRow(
            entry.id, entry.path, STATE_MISSING, False, False, count, MISSING_NOTE
        )
    if not resolved.exists() or not resolved.is_dir():
        return AllowedPathRow(
            entry.id, entry.path, STATE_MISSING, False, False, count, MISSING_NOTE
        )
    if resolved != path:
        return AllowedPathRow(entry.id, entry.path, STATE_MOVED, False, False, count, MOVED_NOTE)
    status: PathStatus = path_status(resolved, mount_points)
    return AllowedPathRow(
        entry.id, entry.path, STATE_OK, status.mounted, status.writable, count, None
    )


async def list_allowed_paths(
    db: AsyncSession, *, mount_points: Iterable[Path] | None = None
) -> list[AllowedPathRow]:
    """Every entry with its mounted and writable status and the deployments under it."""
    points = None if mount_points is None else list(mount_points)
    roots = await _deployment_roots(db)
    return [_row(entry, roots, points) for entry in await stored_paths(db)]


async def add_allowed_path(
    db: AsyncSession,
    raw: str,
    *,
    actor: AuditActor,
    data_path: Path,
    notifier: SettingsNotifier,
    mount_points: Iterable[Path] | None = None,
) -> AllowedPath:
    """Validate and store an entry as its real path, audit it and announce it.

    Raises ``AllowedPathError`` with the first failed check. A concurrent add of the same path
    is refused like a duplicate.
    """
    entries = await stored_paths(db)
    check = check_entry(
        raw,
        data_path=data_path,
        existing=[entry.path for entry in entries],
        mount_points=mount_points,
    )
    if check.real_path is None:
        raise AllowedPathError(check.message or "Enter an absolute path.")
    status = path_status(check.real_path, None if mount_points is None else list(mount_points))
    entry = AllowedPath(
        path=str(check.real_path), created_by_id=actor.user.id if actor.user else None
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise AllowedPathError(DUPLICATE_MESSAGE) from error
    await record_actor_audit(
        db,
        actor,
        action=ACTION_ADD,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_id=entry.id,
        target_name=entry.path,
        details={"path": entry.path, "writable": status.writable},
    )
    await db.commit()
    await announce(notifier, SettingsChanged(AREA, "added", entry.path, actor.label))
    return entry


async def uncovered_deployments(
    db: AsyncSession, entry: AllowedPath, others: Iterable[AllowedPath]
) -> int:
    """How many deployments lose their allowed path when ``entry`` is removed.

    A deployment that another entry still covers stays allowed, so only the ones that no other
    usable entry holds are counted.
    """
    remaining = AllowedPaths([other.path for other in others if other.id != entry.id])
    roots = await _deployment_roots(db)
    return sum(
        1 for root in roots if is_within(root, Path(entry.path)) and not remaining.contains(root)
    )


async def get_entry(db: AsyncSession, path_id: int) -> AllowedPath | None:
    """The entry with this id, None when it is gone."""
    return await db.get(AllowedPath, path_id)


async def remove_allowed_path(
    db: AsyncSession, path_id: int, *, actor: AuditActor, notifier: SettingsNotifier
) -> AllowedPath:
    """Remove an entry, audit it with the number of deployments it leaves outside the list.

    Nothing on disk is touched. Raises ``AllowedPathError`` when the entry is already gone.
    """
    entry = await get_entry(db, path_id)
    if entry is None:
        raise AllowedPathError("This path is no longer in the list.")
    flagged = await uncovered_deployments(db, entry, await stored_paths(db))
    path = entry.path
    entry_id = entry.id
    await db.delete(entry)
    await record_actor_audit(
        db,
        actor,
        action=ACTION_REMOVE,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_id=entry_id,
        target_name=path,
        details={"path": path, "deployments_outside": flagged},
    )
    await db.commit()
    await announce(notifier, SettingsChanged(AREA, "removed", path, actor.label))
    return entry
