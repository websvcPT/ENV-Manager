"""Projects: the list shown to a user, and superadmin-only create, rename and delete."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditResult,
    Deployment,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    Project,
    Service,
    User,
    UserGroup,
)
from app.services.audit import CLI_ACTOR_NAME, AuditActor, record_actor_audit
from app.services.grants import projects_reached
from app.services.permissions import AccessScope, Capability, ItemLevel, compute_visibility
from app.services.token_guard import guard_delete, restrictions_below

MIN_PROJECT_NAME_LENGTH = 3
MAX_PROJECT_NAME_LENGTH = 100
# ASCII letters only: SQLite folds the case of ASCII letters alone, so the unique index on
# ``lower(name)`` and the check in code agree on what a duplicate is.
PROJECT_NAME_PATTERN = re.compile(r"[A-Za-z0-9 ._-]+")
REPEATED_SPACES = re.compile(r" {2,}")
NAME_RULE = (
    f"{MIN_PROJECT_NAME_LENGTH} to {MAX_PROJECT_NAME_LENGTH} characters with at least one letter "
    "or number. Letters, numbers, spaces and the characters . - _ are allowed."
)
NAME_MESSAGE = (
    f"Use {MIN_PROJECT_NAME_LENGTH} to {MAX_PROJECT_NAME_LENGTH} characters with at least one "
    "letter or number: letters, numbers, spaces and the characters . - _"
)


class ProjectError(Exception):
    """Base class for refused project operations."""


class InvalidProjectNameError(ProjectError):
    """The name is empty, too long or uses characters outside the allowed set."""


class ProjectExistsError(ProjectError):
    """A project with that name, ignoring case, already exists."""


class ProjectInUseError(ProjectError):
    """API tokens are restricted to the project or something inside it."""


@dataclass(frozen=True)
class LastDeploy:
    """The most recent deploy run over a project's services in the caller's scope."""

    status: DeployStatus
    started_at: datetime
    actor: str | None


@dataclass(frozen=True)
class ProjectRow:
    """One line of the projects list.

    ``limited_to`` holds the names of the granted deployments or environments for a project
    the caller reaches only partly, and is empty for a project in full scope. ``members`` is
    None unless the caller may see who has access.
    """

    id: int
    name: str
    deployments: int
    last_deploy: LastDeploy | None
    attention: int
    members: int | None
    limited_to: tuple[str, ...]


def validate_project_name(name: str) -> str:
    """The display name when it is acceptable, else ``InvalidProjectNameError``.

    A project name is a display name: letters in any case, numbers, spaces and ``. - _``, 3 to
    100 characters once trimmed and with repeated spaces made one, and at least one letter or
    number, so a name of dots and dashes is refused. The rule applies when a name is created or
    changed; names stored before it are not touched.
    """
    name = REPEATED_SPACES.sub(" ", name.strip())
    if not name:
        raise InvalidProjectNameError("Enter a project name.")
    if (
        not MIN_PROJECT_NAME_LENGTH <= len(name) <= MAX_PROJECT_NAME_LENGTH
        or PROJECT_NAME_PATTERN.fullmatch(name) is None
        or not any(character.isalnum() for character in name)
    ):
        raise InvalidProjectNameError(NAME_MESSAGE)
    return name


async def _ensure_name_free(db: AsyncSession, name: str, *, ignore_id: int | None = None) -> None:
    """Refuse a name another project already has, whatever the case."""
    stmt = select(Project.id).where(func.lower(Project.name) == name.lower())
    if ignore_id is not None:
        stmt = stmt.where(Project.id != ignore_id)
    if (await db.execute(stmt)).first() is not None:
        raise ProjectExistsError(f"A project named {name} already exists.")


async def _flush_unique(db: AsyncSession, name: str) -> None:
    """Flush pending changes, turning a lost race on the unique name into the same refusal."""
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise ProjectExistsError(f"A project named {name} already exists.") from error


async def create_project(
    db: AsyncSession, *, name: str, actor: User, ip_address: str | None = None
) -> Project:
    """Create a project and audit it."""
    name = validate_project_name(name)
    await _ensure_name_free(db, name)
    project = Project(name=name)
    db.add(project)
    await _flush_unique(db, name)
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action="project.create",
        result=AuditResult.SUCCESS,
        target_type="project",
        target_id=project.id,
        target_name=project.name,
        project_id=project.id,
    )
    await db.commit()
    return project


async def rename_project(
    db: AsyncSession, project: Project, *, name: str, actor: User, ip_address: str | None = None
) -> Project:
    """Rename a project and audit it. Renaming to the current name changes nothing."""
    name = validate_project_name(name)
    if name == project.name:
        return project
    await _ensure_name_free(db, name, ignore_id=project.id)
    previous = project.name
    project.name = name
    await _flush_unique(db, name)
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action="project.rename",
        result=AuditResult.SUCCESS,
        target_type="project",
        target_id=project.id,
        target_name=name,
        project_id=project.id,
        details={"previous_name": previous},
    )
    await db.commit()
    return project


async def delete_project(
    db: AsyncSession, project: Project, *, actor: User, ip_address: str | None = None
) -> None:
    """Delete a project with everything under it as a signed-in user, and audit it.

    See ``delete_project_as`` for the refusal and what is removed.
    """
    await delete_project_as(db, project, AuditActor.for_user(actor, ip_address))


async def delete_project_as(db: AsyncSession, project: Project, who: AuditActor) -> None:
    """Delete a project with everything under it, and audit it as ``who``.

    Refused while a usable API token is restricted to the project or something inside it:
    dropping that restriction would widen the token, so the token must be revoked first. A token
    that is revoked or expired can never be used again, so its restriction rows are removed with
    the project instead of keeping the project from being deleted.
    """
    tokens = await guard_delete(
        db,
        who,
        await restrictions_below(db, project_id=project.id),
        action="project.delete",
        target_type="project",
        target_id=project.id,
        target_name=project.name,
        project_id=project.id,
    )
    if tokens:
        raise ProjectInUseError(
            "API tokens are restricted to this project or its contents: "
            + ", ".join(tokens)
            + ". Revoke them first."
        )
    deployments = (
        await db.execute(
            select(func.count()).select_from(Deployment).where(Deployment.project_id == project.id)
        )
    ).scalar_one()
    await record_actor_audit(
        db,
        who,
        action="project.delete",
        result=AuditResult.SUCCESS,
        target_type="project",
        target_id=project.id,
        target_name=project.name,
        project_id=project.id,
        details={"deployments": deployments},
    )
    await db.delete(project)
    await db.commit()


async def latest_runs(
    db: AsyncSession, service_ids: set[int]
) -> dict[int, tuple[DeployRun, str | None]]:
    """The newest real (not dry run) deploy run of each service with the name of who ran it."""
    if not service_ids:
        return {}
    ranked = (
        select(
            DeployRun.id.label("run_id"),
            func.row_number()
            .over(
                partition_by=DeployRun.service_id,
                order_by=(DeployRun.started_at.desc(), DeployRun.id.desc()),
            )
            .label("position"),
        )
        .where(DeployRun.service_id.in_(service_ids), DeployRun.dry_run.is_(False))
        .subquery()
    )
    rows = (
        await db.execute(
            select(DeployRun, User.name)
            .join(ranked, ranked.c.run_id == DeployRun.id)
            .outerjoin(User, User.id == DeployRun.triggered_by_user_id)
            .where(ranked.c.position == 1)
        )
    ).all()
    return {
        run.service_id: (run, CLI_ACTOR_NAME if run.trigger is DeployTrigger.CLI else name)
        for run, name in rows
        if run.service_id is not None
    }


async def member_counts(db: AsyncSession) -> dict[int, int]:
    """Active non-superadmin users with access to each project, by project id.

    A user counts for a project when a grant covers it or anything inside it.
    """
    eligible = set(
        (
            await db.execute(
                select(User.id).where(User.is_active.is_(True), User.group != UserGroup.SUPERADMIN)
            )
        ).scalars()
    )
    members: dict[int, set[int]] = {}
    for user_id, project_ids in (await projects_reached(db)).items():
        if user_id in eligible:
            for project_id in project_ids:
                members.setdefault(project_id, set()).add(user_id)
    return {project_id: len(users) for project_id, users in members.items()}


async def list_project_rows(
    db: AsyncSession, scope: AccessScope, *, query: str = ""
) -> list[ProjectRow]:
    """The projects the caller may see, ordered by name, optionally filtered by name.

    A project the caller reaches only through a deployment or environment appears with the
    names of what is granted; nothing outside the scope is counted or named.
    """
    visibility = await compute_visibility(db, scope)
    shown_projects = visibility.full[ItemLevel.PROJECT] | visibility.context[ItemLevel.PROJECT]
    if not shown_projects:
        return []
    stmt = (
        select(Project)
        .where(Project.id.in_(shown_projects))
        .order_by(func.lower(Project.name), Project.id)
    )
    needle = query.strip().lower()
    if needle:
        stmt = stmt.where(func.lower(Project.name).contains(needle, autoescape=True))
    projects = list((await db.execute(stmt)).scalars())

    full_deployments = visibility.full[ItemLevel.DEPLOYMENT]
    shown_deployments = full_deployments | visibility.context[ItemLevel.DEPLOYMENT]
    deployment_rows = (
        await db.execute(
            select(Deployment.id, Deployment.project_id, Deployment.name).where(
                Deployment.id.in_(shown_deployments)
            )
        )
    ).all()
    shown_services = visibility.full[ItemLevel.SERVICE] | visibility.context[ItemLevel.SERVICE]
    service_deployment = dict(
        (
            await db.execute(
                select(Service.id, Service.deployment_id).where(Service.id.in_(shown_services))
            )
        ).all()
    )
    latest = await latest_runs(db, set(service_deployment))
    environment_rows = (
        await db.execute(
            select(Environment.id, Environment.name, Deployment.project_id, Deployment.id)
            .join(Service, Service.id == Environment.service_id)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .where(Environment.id.in_(visibility.full[ItemLevel.ENVIRONMENT]))
        )
    ).all()
    show_members = scope.can(Capability.MANAGE_GRANTS)
    members = await member_counts(db) if show_members else {}

    rows: list[ProjectRow] = []
    for project in projects:
        in_full_scope = project.id in visibility.full[ItemLevel.PROJECT]
        limited: list[str] = []
        if not in_full_scope:
            limited = [
                name
                for deployment_id, project_id, name in deployment_rows
                if project_id == project.id and deployment_id in full_deployments
            ]
            limited += [
                name
                for _id, name, project_id, deployment_id in environment_rows
                if project_id == project.id and deployment_id not in full_deployments
            ]
        deployment_ids = {
            deployment_id
            for deployment_id, project_id, _name in deployment_rows
            if project_id == project.id
        }
        runs = [
            (run, actor)
            for service_id, (run, actor) in latest.items()
            if service_deployment[service_id] in deployment_ids
        ]
        newest = max(runs, key=lambda item: (item[0].started_at, item[0].id), default=None)
        last_deploy = None
        if newest is not None:
            last_deploy = LastDeploy(newest[0].status, newest[0].started_at, newest[1])
        attention = {
            service_deployment[run.service_id]
            for run, _actor in runs
            if run.service_id is not None and run.status is not DeployStatus.SUCCESS
        }
        rows.append(
            ProjectRow(
                id=project.id,
                name=project.name,
                deployments=len(deployment_ids),
                last_deploy=last_deploy,
                attention=len(attention),
                members=members.get(project.id, 0) if show_members else None,
                limited_to=tuple(sorted(limited)),
            )
        )
    return rows
