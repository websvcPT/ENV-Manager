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
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    DistMissingPolicy,
    DistOutcome,
    DriftPolicy,
    DriftState,
    Environment,
    EnvironmentVersion,
    MissingTargetPolicy,
    PostDeployStatus,
    Service,
    User,
)
from app.services.audit import CLI_ACTOR_NAME
from app.services.deploy_trigger import trigger_label
from app.services.paths import AllowedPaths
from app.services.permissions import (
    CAPABILITY_GROUPS,
    AccessScope,
    Capability,
    ItemLevel,
    Visibility,
    compute_visibility,
)
from app.services.projects import LastDeploy, latest_runs
from app.services.services import is_deployment_files
from app.services.structure_lookup import deployment_with_project

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
    ``trigger_label`` names a run that nobody deployed (an import from Compose), else None.
    """

    id: int
    started_at: datetime
    service: str
    who: str
    environments: tuple[tuple[str, int | None], ...]
    status: DeployStatus
    post_deploy: PostDeployStatus
    exit_code: int | None
    trigger_label: str | None = None


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
    already limits to runs in scope. ``deployment_files`` is the row of the deployment's own files
    when they exist and are in scope; they are never one of ``services``. ``compose_files`` are the
    Compose files linked to the deployment (empty for a caller who reaches it only as a parent),
    and ``can_view_compose`` whether the caller may open their text. ``compose_project`` is the
    Compose project name (None when not set, and for a caller who reaches it only as a parent).
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
    dist_outcome: DistOutcome | None = None
    drift_state: DriftState | None = None
    deployment_files: ServiceRow | None = None
    compose_files: tuple[str, ...] = ()
    can_view_compose: bool = False
    compose_project: str | None = None


# From the outcome that holds a deploy back the most to the one that holds it back the least.
_DIST_SEVERITY = (
    DistOutcome.BLOCKED,
    DistOutcome.FAILED,
    DistOutcome.WARNING,
    DistOutcome.PASSED,
    DistOutcome.SKIPPED,
)
_IN_SYNC_AFTER = (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED)


async def load_check_summary(
    db: AsyncSession, service_ids: set[int], visible_environments: frozenset[int] | set[int]
) -> tuple[DistOutcome | None, DriftState | None]:
    """The dist outcome and drift state the newest result of each environment in scope recorded.

    Dry runs count, since they run the same checks. The worst dist outcome wins; the drift state
    is Drift if any file drifted, else Missing if any target was missing, else In sync. A file the
    newest result wrote (or found identical) is in sync whatever the check found before writing.
    Both are None when no result in scope recorded a check.
    """
    if not service_ids or not visible_environments:
        return None, None
    rows = await db.execute(
        select(
            DeployResult.environment_id,
            DeployResult.status,
            DeployResult.dist_outcome,
            DeployResult.drift_state,
        )
        .join(DeployRun, DeployRun.id == DeployResult.run_id)
        .where(
            DeployRun.service_id.in_(service_ids),
            DeployResult.environment_id.in_(visible_environments),
        )
        .order_by(DeployResult.id)
    )
    newest = {environment_id: row for environment_id, *row in rows.all()}
    dists = {dist for _status, dist, _drift in newest.values() if dist is not None}
    drifts = {
        DriftState.IN_SYNC if status in _IN_SYNC_AFTER else drift
        for status, _dist, drift in newest.values()
        if drift is not None
    }
    dist_outcome = next((outcome for outcome in _DIST_SEVERITY if outcome in dists), None)
    drift_order = (DriftState.DRIFT, DriftState.MISSING, DriftState.IN_SYNC)
    drift_state = next((state for state in drift_order if state in drifts), None)
    return dist_outcome, drift_state


async def load_deployment_detail(
    db: AsyncSession,
    scope: AccessScope,
    deployment_id: int,
    allowed: AllowedPaths,
    visibility: Visibility | None = None,
) -> DeploymentDetail | None:
    """The deployment as the caller sees it, or None when it is outside the caller's scope.

    ``visibility`` is the one the request already computed; without it, it is computed here.
    """
    if visibility is None:
        visibility = await compute_visibility(db, scope)
    full = deployment_id in visibility.full[ItemLevel.DEPLOYMENT]
    if not full and deployment_id not in visibility.context[ItemLevel.DEPLOYMENT]:
        return None
    chain = await deployment_with_project(db, deployment_id)
    if chain is None:
        return None
    deployment, project_name = chain

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
    can_deploy = scope.active and scope.group in CAPABILITY_GROUPS[Capability.DEPLOY]
    environment_counts: dict[int, int] = {}
    deployable_services: set[int] = set()
    for service_id, environment_id in (
        await db.execute(
            select(Environment.service_id, Environment.id).where(
                Environment.service_id.in_(service_ids)
            )
        )
    ).all():
        if environment_id in visible_environments:
            environment_counts[service_id] = environment_counts.get(service_id, 0) + 1
            if can_deploy:
                deployable_services.add(service_id)
    history = await load_history(db, service_ids, visible_environments, full)
    dist_outcome, check_drift = await load_check_summary(db, service_ids, visible_environments)
    latest = await latest_runs(db, service_ids)
    service_rows: list[ServiceRow] = []
    deployment_files: ServiceRow | None = None
    for service in services:
        run, actor = latest.get(service.id, (None, None))
        row = ServiceRow(
            id=service.id,
            name=service.name,
            environments=environment_counts.get(service.id, 0),
            last_deploy=LastDeploy.of(run, actor) if run else None,
            post_deploy=run.post_deploy_status if run else None,
            flags=(run.status,) if run and run.status is not DeployStatus.SUCCESS else (),
            can_deploy=service.id in deployable_services,
        )
        if is_deployment_files(service.name):
            deployment_files = row
        else:
            service_rows.append(row)
    every_row = [*service_rows, *([deployment_files] if deployment_files else [])]
    flags = tuple(
        status
        for status in DeployStatus
        if any(status in service_row.flags for service_row in every_row)
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
        can_check_dist=can_deploy,
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
        dist_outcome=dist_outcome,
        drift_state=check_drift,
        deployment_files=deployment_files,
        compose_files=tuple(deployment.compose_files or ()) if full else (),
        compose_project=deployment.compose_project if full else None,
        can_view_compose=(
            full
            and scope.active
            and scope.group in CAPABILITY_GROUPS[Capability.VIEW_COMPOSE_FILES]
        ),
    )


async def load_history(
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
        elif run.trigger is DeployTrigger.CLI:
            who = CLI_ACTOR_NAME
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
                trigger_label=trigger_label(run.trigger),
            )
        )
    return rows
