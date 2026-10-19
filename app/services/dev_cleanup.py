"""Removal of the scratch users and projects that browser specs leave in a development database.

The browser specs create users whose email starts with ``e2e-u-`` and projects whose name starts
with ``e2e-``. They disable or delete what they create when they finish, but a run that stops
early leaves rows behind, and users cannot be deleted in the application, so the development
database grows with every run.

The rule is the prefix together with an age: only rows created before the cutoff are removed,
whatever their state. A spec may still be using a scratch user while it is active, or disabled on
purpose, so the state says nothing about whether a row is finished with; a run never lasts as
long as the cutoff, so an old row never belongs to a running spec. Seeded users and projects do
not carry the prefixes and are never matched.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, User
from app.models.types import utc_now
from app.services.audit import AuditActor
from app.services.projects import ProjectInUseError, delete_project_as

SCRATCH_USER_PREFIX = "e2e-u-"
SCRATCH_PROJECT_PREFIX = "e2e-"
MIN_SCRATCH_AGE = timedelta(hours=1)


@dataclass(frozen=True)
class CleanupResult:
    """What a cleanup removed and what it left.

    The ``recent`` counts are scratch rows too new to remove. ``projects_in_use`` names the old
    scratch projects that were kept because a usable API token is restricted to them or to
    something inside them.
    """

    users_removed: int
    users_recent: int
    projects_removed: int
    projects_recent: int
    projects_in_use: tuple[str, ...]


def _scratch_user() -> ColumnElement[bool]:
    """The condition that an email starts with the scratch prefix, ignoring case."""
    return func.lower(User.email).startswith(SCRATCH_USER_PREFIX, autoescape=True)


def _scratch_project() -> ColumnElement[bool]:
    """The condition that a project name starts with the scratch prefix, ignoring case."""
    return func.lower(Project.name).startswith(SCRATCH_PROJECT_PREFIX, autoescape=True)


async def _remove_users(db: AsyncSession, cutoff: datetime) -> int:
    """Delete the old scratch users and return how many went.

    Their sessions, pending sign-ins, grants, tokens (with the restrictions of those tokens) and
    seen addresses go with them through the foreign keys. Audit entries, deploy runs, backups and
    version authors only lose the link to the user: the copied names and the history stay.
    """
    users = (
        (await db.execute(select(User).where(_scratch_user(), User.created_at < cutoff)))
        .scalars()
        .all()
    )
    for user in users:
        await db.delete(user)
    await db.commit()
    return len(users)


async def _remove_projects(db: AsyncSession, cutoff: datetime) -> tuple[int, tuple[str, ...]]:
    """Delete the old scratch projects as the command line and return how many went.

    Each goes through ``delete_project_as``, so the guard against restricted API tokens applies:
    a project it refuses is kept, audited as a blocked delete, and named in the second value.
    """
    projects = (
        (
            await db.execute(
                select(Project)
                .where(_scratch_project(), Project.created_at < cutoff)
                .order_by(Project.id)
            )
        )
        .scalars()
        .all()
    )
    removed = 0
    in_use: list[str] = []
    for project in projects:
        name = project.name
        try:
            await delete_project_as(db, project, AuditActor.cli())
        except ProjectInUseError:
            in_use.append(name)
        else:
            removed += 1
    return removed, tuple(in_use)


async def _count_recent(
    db: AsyncSession,
    model: type[User] | type[Project],
    scratch: ColumnElement[bool],
    cutoff: datetime,
) -> int:
    """How many scratch rows of the model are too new to remove."""
    count = select(func.count()).select_from(model).where(scratch, model.created_at >= cutoff)
    return int((await db.execute(count)).scalar_one())


async def remove_scratch_data(
    db: AsyncSession, *, now: datetime | None = None, min_age: timedelta = MIN_SCRATCH_AGE
) -> CleanupResult:
    """Remove the scratch users and projects created before ``now`` minus ``min_age``.

    Users go first, so the tokens they own no longer keep a scratch project from being deleted.
    """
    cutoff = (now or utc_now()) - min_age
    users_removed = await _remove_users(db, cutoff)
    projects_removed, projects_in_use = await _remove_projects(db, cutoff)
    return CleanupResult(
        users_removed=users_removed,
        users_recent=await _count_recent(db, User, _scratch_user(), cutoff),
        projects_removed=projects_removed,
        projects_recent=await _count_recent(db, Project, _scratch_project(), cutoff),
        projects_in_use=projects_in_use,
    )
