"""The deployment detail screen: header figures, services, deploy history and policies.

Everything is computed for one caller scope. A deployment the caller reaches only as the parent
of a granted environment is shown by name, with only the services and environments that lead to
the grant; its path and policies are never part of what is loaded for such a caller.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    ApiToken,
    Deployment,
    DeployResult,
    DeployRun,
    DeployStatus,
    DistMissingPolicy,
    DriftPolicy,
    Environment,
    EnvironmentVersion,
    MissingTargetPolicy,
    PostDeployStatus,
    Project,
    Service,
    User,
)
from app.services.paths import AllowedPaths
from app.services.permissions import (
    CAPABILITY_GROUPS,
    AccessScope,
    Capability,
    ItemLevel,
    compute_visibility,
    load_items,
)
from app.services.projects import LastDeploy, latest_runs

HISTORY_LIMIT = 50


@dataclass(frozen=True)
class ServiceRow:
    """One service of the deployment as the caller sees it.

    ``environments`` counts only the environments in the caller's scope. ``post_deploy`` is the
    outcome of the latest real run and ``flags`` the non-success outcome of that run.
    ``can_deploy`` is True when the caller may deploy at least one environment of the service.
    """

    id: int
    name: str
    environments: int
    last_deploy: LastDeploy | None
    post_deploy: PostDeployStatus | None
    flags: tuple[DeployStatus, ...]
    can_deploy: bool


@dataclass(frozen=True)
class HistoryRow:
    """One real deploy run across the services in scope.

    ``environments`` holds (environment name, version number) for the results in scope; the
    version is None when it was deleted. ``exit_code`` is the post-deploy exit code, if any.
    """

    id: int
    started_at: datetime
    service: str
    who: str
    environments: tuple[tuple[str, int | None], ...]
    status: DeployStatus
    post_deploy: PostDeployStatus
    exit_code: int | None


@dataclass(frozen=True)
class DeploymentPolicies:
    """The deploy policies and file defaults of a deployment."""

    dist_missing: DistMissingPolicy
    drift: DriftPolicy
    missing_target: MissingTargetPolicy
    file_mode: str
    file_owner: str | None
    file_group: str | None


@dataclass(frozen=True)
class DeploymentDetail:
    """A deployment with the figures and rows of its detail screen.

    ``root_path`` and ``policies`` are None for a caller who reaches the deployment only as the
    parent of a granted environment (``full`` is False). ``outside_allowed`` is True when the
    root no longer lies inside the allowed paths. ``last_run`` is the newest real run in scope.
    ``can_check_dist`` is True for the groups that may deploy; dist checks follow that capability.
    ``can_view_output`` is True for the groups that may see post-deploy output, which the history
    already limits to runs in scope.
    """

    id: int
    name: str
    project_id: int
    project_name: str
    root_path: str | None
    full: bool
    outside_allowed: bool
    services: tuple[ServiceRow, ...]
    history: tuple[HistoryRow, ...]
    last_run: HistoryRow | None
    can_check_dist: bool
    can_view_output: bool
    flags: tuple[DeployStatus, ...]
    policies: DeploymentPolicies | None


async def load_deployment_detail(
    db: AsyncSession, scope: AccessScope, deployment_id: int, allowed: AllowedPaths
) -> DeploymentDetail | None:
    """The deployment as the caller sees it, or None when it is outside the caller's scope."""
    visibility = await compute_visibility(db, scope)
    full = deployment_id in visibility.full[ItemLevel.DEPLOYMENT]
    if not full and deployment_id not in visibility.context[ItemLevel.DEPLOYMENT]:
        return None
    row = (
        await db.execute(
            select(Deployment, Project.name)
            .join(Project, Project.id == Deployment.project_id)
            .where(Deployment.id == deployment_id)
        )
    ).one_or_none()
    if row is None:
        return None
    deployment, project_name = row

    shown_services = visibility.full[ItemLevel.SERVICE] | visibility.context[ItemLevel.SERVICE]
    services = (
        (
            await db.execute(
                select(Service)
                .where(Service.deployment_id == deployment_id, Service.id.in_(shown_services))
                .order_by(Service.name)
            )
        )
        .scalars()
        .all()
    )
    service_ids = {service.id for service in services}
    visible_environments = visibility.full[ItemLevel.ENVIRONMENT]
    environment_counts: dict[int, int] = {}
    for service_id, environment_id in (
        await db.execute(
            select(Environment.service_id, Environment.id).where(
                Environment.service_id.in_(service_ids)
            )
        )
    ).all():
        if environment_id in visible_environments:
            environment_counts[service_id] = environment_counts.get(service_id, 0) + 1

    deployable_services = {
        item.service_id
        for item in await load_items(db, ItemLevel.ENVIRONMENT)
        if item.deployment_id == deployment_id and scope.can(Capability.DEPLOY, item)
    }
    history = await _history(db, service_ids, visible_environments, full)
    latest = await latest_runs(db, service_ids)
    service_rows: list[ServiceRow] = []
    for service in services:
        run, actor = latest.get(service.id, (None, None))
        service_rows.append(
            ServiceRow(
                id=service.id,
                name=service.name,
                environments=environment_counts.get(service.id, 0),
                last_deploy=LastDeploy(run.status, run.started_at, actor) if run else None,
                post_deploy=run.post_deploy_status if run else None,
                flags=(run.status,) if run and run.status is not DeployStatus.SUCCESS else (),
                can_deploy=service.id in deployable_services,
            )
        )
    flags = tuple(
        status
        for status in DeployStatus
        if any(status in service_row.flags for service_row in service_rows)
    )
    return DeploymentDetail(
        id=deployment.id,
        name=deployment.name,
        project_id=deployment.project_id,
        project_name=project_name,
        root_path=deployment.root_path if full else None,
        full=full,
        outside_allowed=full and not allowed.contains(deployment.root_path),
        services=tuple(service_rows),
        history=tuple(history),
        last_run=history[0] if history else None,
        can_check_dist=scope.active and scope.group in CAPABILITY_GROUPS[Capability.DEPLOY],
        can_view_output=(
            scope.active and scope.group in CAPABILITY_GROUPS[Capability.VIEW_POST_DEPLOY_OUTPUT]
        ),
        flags=flags,
        policies=(
            DeploymentPolicies(
                dist_missing=deployment.dist_missing_policy,
                drift=deployment.drift_policy,
                missing_target=deployment.missing_target_policy,
                file_mode=deployment.file_mode,
                file_owner=deployment.file_owner,
                file_group=deployment.file_group,
            )
            if full
            else None
        ),
    )


async def _history(
    db: AsyncSession,
    service_ids: set[int],
    visible_environments: frozenset[int] | set[int],
    full: bool,
) -> list[HistoryRow]:
    """The newest real runs of the services, with the results in scope.

    A run whose results all lie outside the caller's scope is left out, so a scoped caller never
    sees runs that touched only files they cannot see.
    """
    if not service_ids:
        return []
    owner = aliased(User)
    runs = (
        await db.execute(
            select(DeployRun, User.name, ApiToken.name, owner.name)
            .outerjoin(User, User.id == DeployRun.triggered_by_user_id)
            .outerjoin(ApiToken, ApiToken.id == DeployRun.triggered_by_token_id)
            .outerjoin(owner, owner.id == ApiToken.owner_id)
            .where(DeployRun.service_id.in_(service_ids), DeployRun.dry_run.is_(False))
            .order_by(DeployRun.started_at.desc(), DeployRun.id.desc())
            .limit(HISTORY_LIMIT)
        )
    ).all()
    results: dict[int, list[tuple[int | None, str, int | None]]] = {}
    for run_id, environment_id, name, number in (
        await db.execute(
            select(
                DeployResult.run_id,
                DeployResult.environment_id,
                DeployResult.environment_name,
                EnvironmentVersion.number,
            )
            .outerjoin(EnvironmentVersion, EnvironmentVersion.id == DeployResult.version_id)
            .where(DeployResult.run_id.in_([run.id for run, *_ in runs]))
            .order_by(DeployResult.id)
        )
    ).all():
        results.setdefault(run_id, []).append((environment_id, name, number))

    rows: list[HistoryRow] = []
    for run, user_name, token_name, owner_name in runs:
        every = results.get(run.id, [])
        shown = tuple(
            (name, number)
            for environment_id, name, number in every
            if environment_id in visible_environments or (environment_id is None and full)
        )
        if every and not shown:
            continue
        if user_name:
            who = user_name
        elif token_name:
            who = f"token {token_name} ({owner_name})" if owner_name else f"token {token_name}"
        else:
            who = "unknown"
        rows.append(
            HistoryRow(
                id=run.id,
                started_at=run.started_at,
                service=run.service_name,
                who=who,
                environments=shown,
                status=run.status,
                post_deploy=run.post_deploy_status,
                exit_code=run.post_deploy_exit_code,
            )
        )
    return rows
