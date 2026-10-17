"""The project detail screen: header figures, deployments table, activity and members.

Everything is computed for one caller scope. A project the caller reaches only through a
deployment or environment is shown with the granted names; items outside the scope are never
counted or named.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessGrant,
    AuditEntry,
    Deployment,
    DeployStatus,
    Environment,
    GrantLevel,
    Project,
    Service,
    User,
    UserGroup,
)
from app.services.permissions import AccessScope, Capability, ItemLevel, compute_visibility
from app.services.projects import LastDeploy, latest_runs, member_counts

ACTIVITY_LIMIT = 50


@dataclass(frozen=True)
class DeploymentRow:
    """One deployment of the project as the caller sees it.

    ``root_path`` is None for a deployment the caller reaches only as the parent of a granted
    environment, which is shown by name only. ``flags`` holds the distinct non-success outcomes
    of the latest run of the services in scope.
    """

    id: int
    name: str
    root_path: str | None
    files: int
    last_deploy: LastDeploy | None
    flags: tuple[DeployStatus, ...]


@dataclass(frozen=True)
class ProjectDetail:
    """A project with the figures and rows the detail tabs share.

    ``limited_to`` names the granted deployments and environments when the caller reaches the
    project only partly, and ``members`` is None unless the caller may see who has access.
    ``attention`` counts the deployments that carry at least one flag.
    """

    id: int
    name: str
    deployments: tuple[DeploymentRow, ...]
    attention: int
    members: int | None
    limited_to: tuple[str, ...]
    partial: bool
    all_granted_are_deployments: bool


@dataclass(frozen=True)
class ActivityRow:
    """One audit entry of the project, as shown on the Activity tab."""

    created_at: datetime
    actor: str
    action: str
    target: str
    result: str


@dataclass(frozen=True)
class MemberAccess:
    """One user with access to the project and where that access comes from.

    ``direct`` is True for a grant on the project itself. Otherwise ``sources`` lists the
    (name, level) of the deployments and environments inside the project that carry a grant,
    and a superadmin has neither (access comes from the group).
    """

    name: str
    group: UserGroup
    direct: bool
    sources: tuple[tuple[str, str], ...]


async def load_project_detail(
    db: AsyncSession, scope: AccessScope, project_id: int
) -> ProjectDetail | None:
    """The project as the caller sees it, or None when it is outside the caller's scope."""
    visibility = await compute_visibility(db, scope)
    full_projects = visibility.full[ItemLevel.PROJECT]
    if project_id not in full_projects | visibility.context[ItemLevel.PROJECT]:
        return None
    project = await db.get(Project, project_id)
    if project is None:
        return None
    full_deployments = visibility.full[ItemLevel.DEPLOYMENT]
    shown_deployments = full_deployments | visibility.context[ItemLevel.DEPLOYMENT]
    deployments = list(
        (
            await db.execute(
                select(Deployment)
                .where(Deployment.project_id == project_id, Deployment.id.in_(shown_deployments))
                .order_by(Deployment.name)
            )
        ).scalars()
    )
    shown_services = visibility.full[ItemLevel.SERVICE] | visibility.context[ItemLevel.SERVICE]
    service_deployment = dict(
        (
            await db.execute(
                select(Service.id, Service.deployment_id).where(
                    Service.id.in_(shown_services),
                    Service.deployment_id.in_([deployment.id for deployment in deployments]),
                )
            )
        ).all()
    )
    latest = await latest_runs(db, set(service_deployment))
    environments = (
        await db.execute(
            select(Environment.id, Environment.name, Service.deployment_id)
            .join(Service, Service.id == Environment.service_id)
            .where(
                Environment.id.in_(visibility.full[ItemLevel.ENVIRONMENT]),
                Service.deployment_id.in_([deployment.id for deployment in deployments]),
            )
            .order_by(Environment.name)
        )
    ).all()

    rows: list[DeploymentRow] = []
    for deployment in deployments:
        runs = [
            (run, actor)
            for service_id, (run, actor) in latest.items()
            if service_deployment[service_id] == deployment.id
        ]
        newest = max(runs, key=lambda item: (item[0].started_at, item[0].id), default=None)
        flags = tuple(
            status
            for status in DeployStatus
            if status is not DeployStatus.SUCCESS and any(run.status is status for run, _ in runs)
        )
        rows.append(
            DeploymentRow(
                id=deployment.id,
                name=deployment.name,
                root_path=deployment.root_path if deployment.id in full_deployments else None,
                files=sum(1 for _id, _name, owner in environments if owner == deployment.id),
                last_deploy=(
                    LastDeploy(newest[0].status, newest[0].started_at, newest[1])
                    if newest
                    else None
                ),
                flags=flags,
            )
        )

    partial = project_id not in full_projects
    limited: list[str] = []
    if partial:
        limited = [
            deployment.name for deployment in deployments if deployment.id in full_deployments
        ]
        deployment_only = len(limited)
        limited += [name for _id, name, owner in environments if owner not in full_deployments]
    else:
        deployment_only = 0
    members = None
    if scope.can(Capability.MANAGE_GRANTS):
        members = (await member_counts(db)).get(project_id, 0)
    return ProjectDetail(
        id=project.id,
        name=project.name,
        deployments=tuple(rows),
        attention=sum(1 for row in rows if row.flags),
        members=members,
        limited_to=tuple(sorted(limited)),
        partial=partial,
        all_granted_are_deployments=deployment_only == len(limited),
    )


async def list_activity(
    db: AsyncSession, scope: AccessScope, project_id: int, *, limit: int = ACTIVITY_LIMIT
) -> list[ActivityRow]:
    """The newest audit entries of the project that lie inside the caller's scope."""
    visibility = await compute_visibility(db, scope)
    entries = (
        await db.execute(
            select(AuditEntry)
            .where(AuditEntry.project_id == project_id)
            .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
        )
    ).scalars()
    rows: list[ActivityRow] = []
    for entry in entries:
        if not visibility.includes_audit_entry(
            project_id=entry.project_id,
            deployment_id=entry.deployment_id,
            service_id=entry.service_id,
            environment_id=entry.environment_id,
        ):
            continue
        rows.append(
            ActivityRow(
                created_at=entry.created_at,
                actor=entry.actor_name or "system",
                action=entry.action,
                target=entry.target_name or "",
                result=entry.result.value,
            )
        )
        if len(rows) == limit:
            break
    return rows


async def list_members(db: AsyncSession, project_id: int) -> list[MemberAccess]:
    """Active users with access to the project, direct or inherited, ordered by name.

    A grant on the project is direct. A grant on a deployment or environment inside it makes the
    user a member with that item as the source. Superadmins have access through their group.
    """
    deployments = dict(
        (
            await db.execute(
                select(Deployment.id, Deployment.name).where(Deployment.project_id == project_id)
            )
        ).all()
    )
    environments = dict(
        (
            await db.execute(
                select(Environment.id, Environment.name)
                .join(Service, Service.id == Environment.service_id)
                .join(Deployment, Deployment.id == Service.deployment_id)
                .where(Deployment.project_id == project_id)
            )
        ).all()
    )
    users = (
        (
            await db.execute(
                select(User).where(User.is_active.is_(True)).order_by(User.name, User.id)
            )
        )
        .scalars()
        .all()
    )
    grants = (await db.execute(select(AccessGrant))).scalars().all()
    direct: set[int] = set()
    sources: dict[int, list[tuple[str, str]]] = {}
    for grant in grants:
        if grant.level is GrantLevel.PROJECT and grant.project_id == project_id:
            direct.add(grant.user_id)
        elif grant.level is GrantLevel.DEPLOYMENT and grant.deployment_id in deployments:
            sources.setdefault(grant.user_id, []).append(
                (deployments[grant.deployment_id], "Deployment")
            )
        elif grant.level is GrantLevel.ENVIRONMENT and grant.environment_id in environments:
            sources.setdefault(grant.user_id, []).append(
                (environments[grant.environment_id], "Environment")
            )
    members: list[MemberAccess] = []
    for user in users:
        if user.group is UserGroup.SUPERADMIN:
            members.append(MemberAccess(user.name, user.group, False, ()))
        elif user.id in direct:
            members.append(MemberAccess(user.name, user.group, True, ()))
        elif user.id in sources:
            ordered = tuple(sorted(sources[user.id]))
            members.append(MemberAccess(user.name, user.group, False, ordered))
    return members
