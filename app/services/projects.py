"""Projects: the list shown to a user, and superadmin-only create, rename and delete."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessGrant,
    ApiToken,
    ApiTokenRestriction,
    AuditActorType,
    AuditResult,
    Deployment,
    DeployRun,
    DeployStatus,
    Environment,
    GrantLevel,
    Project,
    Service,
    User,
    UserGroup,
)
from app.services.audit import record_audit
from app.services.permissions import AccessScope, Capability, ItemLevel, compute_visibility

MAX_PROJECT_NAME_LENGTH = 100
PROJECT_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
NAME_RULE = "Lowercase letters, numbers and dashes."


class ProjectError(Exception):
    """Base class for refused project operations."""


class InvalidProjectNameError(ProjectError):
    """The name is empty, too long or uses characters outside the allowed set."""


class ProjectExistsError(ProjectError):
    """A project with that name already exists."""


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
    """The trimmed name when it is acceptable, else ``InvalidProjectNameError``."""
    name = name.strip()
    if not name:
        raise InvalidProjectNameError("Enter a project name.")
    if len(name) > MAX_PROJECT_NAME_LENGTH or PROJECT_NAME_PATTERN.fullmatch(name) is None:
        raise InvalidProjectNameError(
            "Use 1 to 100 lowercase letters, numbers and dashes, starting with a letter or number."
        )
    return name


async def _ensure_name_free(db: AsyncSession, name: str, *, ignore_id: int | None = None) -> None:
    """Refuse a name another project already has."""
    stmt = select(Project.id).where(Project.name == name)
    if ignore_id is not None:
        stmt = stmt.where(Project.id != ignore_id)
    if (await db.execute(stmt)).first() is not None:
        raise ProjectExistsError(f"A project named {name} already exists.")


async def create_project(
    db: AsyncSession, *, name: str, actor: User, ip_address: str | None = None
) -> Project:
    """Create a project and audit it."""
    name = validate_project_name(name)
    await _ensure_name_free(db, name)
    project = Project(name=name)
    db.add(project)
    await db.flush()
    await record_audit(
        db,
        action="project.create",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
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
    await record_audit(
        db,
        action="project.rename",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
        target_type="project",
        target_id=project.id,
        target_name=name,
        project_id=project.id,
        details={"previous_name": previous},
    )
    await db.commit()
    return project


async def _restricting_token_names(db: AsyncSession, project: Project) -> list[str]:
    """Names of the API tokens restricted to the project or anything below it."""
    deployment_ids = select(Deployment.id).where(Deployment.project_id == project.id)
    service_ids = select(Service.id).where(Service.deployment_id.in_(deployment_ids))
    environment_ids = select(Environment.id).where(Environment.service_id.in_(service_ids))
    stmt = (
        select(ApiToken.name)
        .join(ApiTokenRestriction, ApiTokenRestriction.token_id == ApiToken.id)
        .where(
            (ApiTokenRestriction.project_id == project.id)
            | ApiTokenRestriction.deployment_id.in_(deployment_ids)
            | ApiTokenRestriction.service_id.in_(service_ids)
            | ApiTokenRestriction.environment_id.in_(environment_ids)
        )
        .distinct()
        .order_by(ApiToken.name)
    )
    return list((await db.execute(stmt)).scalars())


async def delete_project(
    db: AsyncSession, project: Project, *, actor: User, ip_address: str | None = None
) -> None:
    """Delete a project with everything under it, and audit it.

    Refused while an API token is restricted to the project or something inside it: dropping
    that restriction would widen the token, so the token must be changed or revoked first.
    """
    tokens = await _restricting_token_names(db, project)
    if tokens:
        await record_audit(
            db,
            action="project.delete",
            result=AuditResult.BLOCKED,
            actor_type=AuditActorType.USER,
            actor_user=actor,
            ip_address=ip_address,
            target_type="project",
            target_id=project.id,
            target_name=project.name,
            project_id=project.id,
            details={"reason": "restricted_tokens", "tokens": tokens},
        )
        await db.commit()
        raise ProjectInUseError(
            "API tokens are restricted to this project or its contents: "
            + ", ".join(tokens)
            + ". Change or revoke them first."
        )
    deployments = (
        await db.execute(
            select(func.count()).select_from(Deployment).where(Deployment.project_id == project.id)
        )
    ).scalar_one()
    await record_audit(
        db,
        action="project.delete",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
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
    return {run.service_id: (run, name) for run, name in rows if run.service_id is not None}


async def member_counts(db: AsyncSession) -> dict[int, int]:
    """Active non-superadmin users with access to each project, by project id.

    A user counts for a project when a grant covers it or anything inside it.
    """
    deployment_project = dict(
        (await db.execute(select(Deployment.id, Deployment.project_id))).all()
    )
    environment_project = dict(
        (
            await db.execute(
                select(Environment.id, Deployment.project_id)
                .join(Service, Service.id == Environment.service_id)
                .join(Deployment, Deployment.id == Service.deployment_id)
            )
        ).all()
    )
    grants = (
        await db.execute(
            select(AccessGrant)
            .join(User, User.id == AccessGrant.user_id)
            .where(User.is_active.is_(True), User.group != UserGroup.SUPERADMIN)
        )
    ).scalars()
    members: dict[int, set[int]] = {}
    for grant in grants:
        project_id = {
            GrantLevel.PROJECT: grant.project_id,
            GrantLevel.DEPLOYMENT: deployment_project.get(grant.deployment_id or 0),
            GrantLevel.ENVIRONMENT: environment_project.get(grant.environment_id or 0),
        }[grant.level]
        if project_id is not None:
            members.setdefault(project_id, set()).add(grant.user_id)
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
    stmt = select(Project).where(Project.id.in_(shown_projects)).order_by(Project.name)
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
