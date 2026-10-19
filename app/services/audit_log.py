"""The audit log screen: scoped entries, filter options, paging and the rows shown to a caller.

Every query goes through ``Visibility.audit_entry_clause``, so the page, the load more fragment,
a single entry, the filter option lists and the CSV export all see the same entries. Filter
options are the distinct values among the entries the caller may see, never the whole user or
project tables, so a hidden project or user is never named.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditActorType,
    AuditEntry,
    AuditResult,
    Deployment,
    Environment,
    EnvironmentVersion,
    Project,
    Service,
)
from app.services.audit_details import DetailLine, detail_lines, details_json, without_command_text
from app.services.audit_filters import AuditFilters, Cursor
from app.services.permissions import ItemLevel, Visibility

PAGE_SIZE = 50
ACTOR_KINDS = {
    AuditActorType.USER: "user",
    AuditActorType.API: "token",
    AuditActorType.CLI: "cli",
    AuditActorType.SYSTEM: "system",
}
RESULT_LABELS = {
    AuditResult.SUCCESS: "success",
    AuditResult.HALTED: "halted",
    AuditResult.BLOCKED: "blocked",
    AuditResult.FAILURE: "failed",
}
UNKNOWN_ACTOR = "unknown"
SYSTEM_ACTOR = "system"


@dataclass(frozen=True)
class AuditRow:
    """One audit entry as the table and the export show it. Details never hold a value."""

    id: int
    created_at: datetime
    actor: str
    actor_kind: str
    ip_address: str | None
    action: str
    object_type: str | None
    object_name: str | None
    result: AuditResult
    details: tuple[DetailLine, ...]
    details_json: str
    version_ids: tuple[int, ...]
    versions: tuple[str, ...]
    trace_id: str | None

    @property
    def short_trace(self) -> str | None:
        """The trace id shortened for display, or None when the entry carries none."""
        if not self.trace_id:
            return None
        if len(self.trace_id) <= 12:
            return self.trace_id
        return f"{self.trace_id[:4]}…{self.trace_id[-4:]}"


@dataclass(frozen=True)
class ActorOption:
    """One entry of the user or token filter."""

    value: str
    label: str


@dataclass(frozen=True)
class FilterOptions:
    """What the filter selects offer: only values found among the caller's visible entries."""

    projects: tuple[tuple[int, str], ...]
    actors: tuple[ActorOption, ...]
    actions: tuple[str, ...]
    results: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AuditPage:
    """A page of rows, newest first, and the cursor of the next page when there is one."""

    rows: tuple[AuditRow, ...]
    next_cursor: str | None


async def load_filter_options(db: AsyncSession, visibility: Visibility) -> FilterOptions:
    """The filter option lists for what the caller may see.

    Projects come from the entries too, so a project the caller reaches only as the parent of a
    granted file is offered (its name is already shown as context) and one with no entry in
    scope is not.
    """
    clause = visibility.audit_entry_clause()
    project_ids = set(
        (
            await db.execute(
                select(AuditEntry.project_id)
                .where(clause, AuditEntry.project_id.is_not(None))
                .distinct()
            )
        ).scalars()
    )
    projects: list[tuple[int, str]] = []
    if project_ids:
        rows = await db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids)))
        projects = sorted(
            (
                (project_id, name)
                for project_id, name in rows.all()
                if visibility.unrestricted
                or visibility.can_see(ItemLevel.PROJECT, project_id)
                or visibility.is_context_only(ItemLevel.PROJECT, project_id)
            ),
            key=lambda item: item[1].lower(),
        )
    actor_rows = await db.execute(
        select(AuditEntry.actor_type, AuditEntry.actor_name)
        .where(clause, AuditEntry.actor_name.is_not(None))
        .distinct()
    )
    actors = sorted(
        (
            ActorOption(f"{actor_type.value}:{name}", name)
            for actor_type, name in actor_rows.all()
            if name
        ),
        key=lambda option: (option.label.lower(), option.value),
    )
    actions = (
        (await db.execute(select(AuditEntry.action).where(clause).distinct().order_by("action")))
        .scalars()
        .all()
    )
    return FilterOptions(
        projects=tuple(projects),
        actors=tuple(actors),
        actions=tuple(actions),
        results=tuple((result.value, label) for result, label in RESULT_LABELS.items()),
    )


@dataclass(frozen=True)
class _StoredVersion:
    """A stored version with the ids of the file it belongs to and that file's ancestors."""

    label: str
    environment_id: int
    service_id: int
    deployment_id: int
    project_id: int

    def lies_within(self, entry: AuditEntry) -> bool:
        """Whether the version belongs to the item the entry is about, or below it."""
        if entry.environment_id is not None:
            return self.environment_id == entry.environment_id
        if entry.service_id is not None:
            return self.service_id == entry.service_id
        if entry.deployment_id is not None:
            return self.deployment_id == entry.deployment_id
        return entry.project_id is not None and self.project_id == entry.project_id


async def _stored_versions(
    db: AsyncSession, entries: Sequence[AuditEntry]
) -> dict[int, _StoredVersion]:
    """The stored versions behind the version ids of the entries, never their content."""
    wanted = {
        version_id
        for entry in entries
        for version_id in entry.version_ids
        if isinstance(version_id, int)
    }
    if not wanted:
        return {}
    rows = await db.execute(
        select(
            EnvironmentVersion.id,
            EnvironmentVersion.number,
            Environment.id,
            Environment.name,
            Environment.service_id,
            Service.deployment_id,
            Deployment.project_id,
        )
        .join(Environment, Environment.id == EnvironmentVersion.environment_id)
        .join(Service, Service.id == Environment.service_id)
        .join(Deployment, Deployment.id == Service.deployment_id)
        .where(EnvironmentVersion.id.in_(wanted))
    )
    return {
        version_id: _StoredVersion(
            f"{name} v{number}", environment_id, service_id, deployment_id, project_id
        )
        for version_id, number, environment_id, name, service_id, deployment_id, project_id in (
            rows.all()
        )
    }


def _version_labels(entry: AuditEntry, stored: dict[int, _StoredVersion]) -> tuple[str, ...]:
    """``name vN`` for each version id of the entry, or ``#id`` when it cannot be named.

    A version is named only when it lies within the item the entry is about. Versions are not
    numbered for ever (a deleted file frees its ids), so an id that now belongs to another file
    must not name that file, and an entry the caller may read never names a file they may not.
    """
    labels: list[str] = []
    for version_id in entry.version_ids:
        if not isinstance(version_id, int):
            continue
        found = stored.get(version_id)
        labels.append(found.label if found and found.lies_within(entry) else f"#{version_id}")
    return tuple(labels)


def _actor_name(entry: AuditEntry) -> str:
    """The name to show for the actor of an entry."""
    if entry.actor_name:
        if entry.actor_type is AuditActorType.API:
            return entry.actor_name.removeprefix("token ")
        return entry.actor_name
    return SYSTEM_ACTOR if entry.actor_type is AuditActorType.SYSTEM else UNKNOWN_ACTOR


async def _rows(
    db: AsyncSession, entries: Sequence[AuditEntry], *, show_command_text: bool
) -> tuple[AuditRow, ...]:
    """The display rows for entries already filtered to the caller's scope.

    Only the superadmin (``show_command_text``) reads the command text an entry may carry.
    """
    stored = await _stored_versions(db, entries)
    rows: list[AuditRow] = []
    for entry in entries:
        version_ids = tuple(item for item in entry.version_ids if isinstance(item, int))
        details = entry.details if show_command_text else without_command_text(entry.details)
        rows.append(
            AuditRow(
                id=entry.id,
                created_at=entry.created_at,
                actor=_actor_name(entry),
                actor_kind=ACTOR_KINDS[entry.actor_type],
                ip_address=entry.ip_address,
                action=entry.action,
                object_type=entry.target_type,
                object_name=entry.target_name,
                result=entry.result,
                details=detail_lines(details),
                details_json=details_json(details),
                version_ids=version_ids,
                versions=_version_labels(entry, stored),
                trace_id=entry.trace_id,
            )
        )
    return tuple(rows)


async def list_page(
    db: AsyncSession,
    visibility: Visibility,
    filters: AuditFilters,
    *,
    cursor: Cursor | None = None,
    limit: int = PAGE_SIZE,
    up_to_id: int | None = None,
) -> AuditPage:
    """A page of the visible entries that match the filters, newest first, after the cursor.

    ``up_to_id`` leaves out entries written after that id, so a long read (the export) covers
    a fixed set of entries.
    """
    stmt = select(AuditEntry).where(visibility.audit_entry_clause(), *filters.conditions())
    if up_to_id is not None:
        stmt = stmt.where(AuditEntry.id <= up_to_id)
    if cursor is not None:
        stmt = stmt.where(cursor.after_condition())
    stmt = stmt.order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc()).limit(limit + 1)
    entries = list((await db.execute(stmt)).scalars())
    more = len(entries) > limit
    entries = entries[:limit]
    next_cursor = Cursor(entries[-1].created_at, entries[-1].id).encode() if more else None
    rows = await _rows(db, entries, show_command_text=visibility.unrestricted)
    return AuditPage(rows, next_cursor)


async def load_entry(db: AsyncSession, visibility: Visibility, entry_id: int) -> AuditRow | None:
    """One entry as the caller sees it, or None when it does not exist or is outside the scope.

    Both cases look the same, so the answer never says whether an entry exists.
    """
    entry = (
        await db.execute(
            select(AuditEntry).where(AuditEntry.id == entry_id, visibility.audit_entry_clause())
        )
    ).scalar_one_or_none()
    if entry is None:
        return None
    return (await _rows(db, [entry], show_command_text=visibility.unrestricted))[0]


async def count_entries(
    db: AsyncSession,
    visibility: Visibility,
    filters: AuditFilters,
    *,
    up_to_id: int | None = None,
) -> int:
    """How many visible entries match the filters, optionally only up to an entry id."""
    stmt = (
        select(func.count())
        .select_from(AuditEntry)
        .where(visibility.audit_entry_clause(), *filters.conditions())
    )
    if up_to_id is not None:
        stmt = stmt.where(AuditEntry.id <= up_to_id)
    return (await db.execute(stmt)).scalar_one()


async def newest_entry_id(db: AsyncSession) -> int:
    """The id of the newest entry of the whole log, 0 when it is empty."""
    return (await db.execute(select(func.max(AuditEntry.id)))).scalar_one() or 0
