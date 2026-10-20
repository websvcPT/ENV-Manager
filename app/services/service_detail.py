"""The service screen: header, environments in scope, post-deploy panel and deploy history.

Everything is computed for one caller scope. A service the caller reaches only as the parent of
a granted environment is shown by name, with only the environments in scope; the deployment
root path is never part of what is loaded for a caller who has no access to the deployment.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeployResult,
    DeployResultStatus,
    DeployRun,
    Environment,
    EnvironmentVersion,
    FileType,
    HookType,
    PostDeployStatus,
    ServiceHook,
)
from app.services.deployment_detail import HistoryRow, load_history
from app.services.paths import AllowedPaths, load_allowed_paths
from app.services.permissions import (
    CAPABILITY_GROUPS,
    AccessScope,
    Capability,
    ItemLevel,
    ItemRef,
    Visibility,
    compute_visibility,
)
from app.services.structure_lookup import ServiceChain, service_with_deployment

# Environment status keys, shared with the badge vocabulary.
STATUS_PENDING = "pending"
STATUS_DEPLOYED = "deployed"
STATUS_NEVER = "never"
_FAILURE_STATUS = {
    DeployResultStatus.BLOCKED: "blocked",
    DeployResultStatus.HALTED: "halted",
    DeployResultStatus.FAILED: "failed",
}
_WRITTEN = (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED)


@dataclass(frozen=True)
class ServiceHeader:
    """A service in the caller's view: names, ancestry and what the caller may see of it.

    ``root_path`` is None unless the caller has access to the whole deployment.
    ``visible_environment_ids`` are the environments of the service in scope and
    ``hidden_environments`` counts the others. ``can_view_output`` and ``can_view_command`` follow
    the group only: an environment grant does not cover the parent service, yet it still lets the
    caller see the post-deploy outcome of the deploys it can see. ``outside_allowed`` is True when
    the deployment root no longer lies inside the allowed paths (whoever asks; the root itself is
    not part of what the caller gets).
    """

    id: int
    name: str
    deployment_id: int
    deployment_name: str
    project_id: int
    project_name: str
    root_path: str | None
    deployment_full: bool
    visible_environment_ids: frozenset[int]
    hidden_environments: int
    can_view_output: bool
    can_view_command: bool
    outside_allowed: bool = False


@dataclass(frozen=True)
class EnvironmentRow:
    """One environment of the service in the caller's scope.

    ``current`` is the newest saved version number and ``deployed`` the version of the newest
    successful write, None when nothing was written yet. ``status`` is a badge key: pending,
    deployed, never, blocked, halted or failed.
    """

    id: int
    name: str
    target_path: str
    file_type: FileType
    dist_linked: bool
    current: int | None
    deployed: int | None
    status: str
    can_edit: bool
    can_check_dist: bool


@dataclass(frozen=True)
class HookSettings:
    """The stored post-deploy command of a service, as only the superadmin sees it."""

    commands: str
    working_directory: str | None
    timeout_seconds: int
    run_only_if_changed: bool


@dataclass(frozen=True)
class PostDeployRun:
    """The post-deploy outcome of the latest real run in the caller's scope."""

    started_at: datetime
    who: str
    status: PostDeployStatus
    exit_code: int | None
    output: str | None
    reason: str | None
    trigger_label: str | None = None


@dataclass(frozen=True)
class PostDeployPanel:
    """The post-deploy panel: the last run and, for the superadmin, the stored command."""

    last: PostDeployRun | None
    hook: HookSettings | None


async def load_service_header(
    db: AsyncSession,
    scope: AccessScope,
    service_id: int,
    visibility: Visibility | None = None,
    allowed: AllowedPaths | None = None,
    *,
    chain: ServiceChain | None = None,
) -> ServiceHeader | None:
    """The service as the caller sees it, or None when it is outside the caller's scope.

    ``visibility`` and ``allowed`` are the ones the request already holds, and ``chain`` the
    service with its deployment and project name when the caller read them already; whatever is
    missing is loaded here.
    """
    if visibility is None:
        visibility = await compute_visibility(db, scope)
    if (
        service_id not in visibility.full[ItemLevel.SERVICE]
        and service_id not in visibility.context[ItemLevel.SERVICE]
    ):
        return None
    if chain is None:
        chain = await service_with_deployment(db, service_id)
    if chain is None:
        return None
    if allowed is None:
        allowed = await load_allowed_paths(db)
    service, deployment, project_name = chain
    environment_ids = set(
        (await db.execute(select(Environment.id).where(Environment.service_id == service_id)))
        .scalars()
        .all()
    )
    visible = frozenset(environment_ids & visibility.full[ItemLevel.ENVIRONMENT])
    full = deployment.id in visibility.full[ItemLevel.DEPLOYMENT]
    return ServiceHeader(
        id=service.id,
        name=service.name,
        deployment_id=deployment.id,
        deployment_name=deployment.name,
        project_id=deployment.project_id,
        project_name=project_name,
        root_path=deployment.root_path if full else None,
        deployment_full=full,
        visible_environment_ids=visible,
        hidden_environments=len(environment_ids) - len(visible),
        can_view_output=(
            scope.active and scope.group in CAPABILITY_GROUPS[Capability.VIEW_POST_DEPLOY_OUTPUT]
        ),
        can_view_command=(
            scope.active and scope.group in CAPABILITY_GROUPS[Capability.VIEW_POST_DEPLOY_COMMAND]
        ),
        outside_allowed=not allowed.contains(deployment.root_path),
    )


def _environment_status(
    current: int | None, latest: tuple[DeployResultStatus, int | None] | None
) -> str:
    """The badge key of an environment from its newest version and newest real result.

    A version saved after the newest attempt is pending whatever that attempt did; otherwise a
    blocked, halted or failed attempt is reported, and a written one reads as deployed. Drift
    is not part of this status.
    """
    if latest is None:
        return STATUS_NEVER
    result_status, attempted = latest
    if attempted is not None and current is not None and current > attempted:
        return STATUS_PENDING
    return _FAILURE_STATUS.get(result_status, STATUS_DEPLOYED)


async def load_environment_rows(
    db: AsyncSession, scope: AccessScope, header: ServiceHeader
) -> tuple[EnvironmentRow, ...]:
    """The environments of the service in the caller's scope with versions and status."""
    ids = header.visible_environment_ids
    if not ids:
        return ()
    environments = (
        (
            await db.execute(
                select(Environment).where(Environment.id.in_(ids)).order_by(Environment.name)
            )
        )
        .scalars()
        .all()
    )
    current = dict(
        (
            await db.execute(
                select(EnvironmentVersion.environment_id, func.max(EnvironmentVersion.number))
                .where(EnvironmentVersion.environment_id.in_(ids))
                .group_by(EnvironmentVersion.environment_id)
            )
        ).all()
    )
    latest: dict[int, tuple[DeployResultStatus, int | None]] = {}
    deployed: dict[int, int | None] = {}
    for environment_id, status, number in (
        await db.execute(
            select(DeployResult.environment_id, DeployResult.status, EnvironmentVersion.number)
            .join(DeployRun, DeployRun.id == DeployResult.run_id)
            .join(Environment, Environment.id == DeployResult.environment_id)
            .outerjoin(EnvironmentVersion, EnvironmentVersion.id == DeployResult.version_id)
            .where(
                DeployResult.target_path == Environment.target_path,
                DeployResult.environment_id.in_(ids),
                DeployRun.dry_run.is_(False),
                DeployResult.status != DeployResultStatus.WOULD_WRITE,
            )
            .order_by(DeployRun.started_at, DeployResult.id)
        )
    ).all():
        if environment_id is None:
            continue
        latest[environment_id] = (status, number)
        if status in _WRITTEN:
            deployed[environment_id] = number
    rows: list[EnvironmentRow] = []
    for environment in environments:
        item = ItemRef(
            level=ItemLevel.ENVIRONMENT,
            project_id=header.project_id,
            deployment_id=header.deployment_id,
            service_id=header.id,
            environment_id=environment.id,
        )
        rows.append(
            EnvironmentRow(
                id=environment.id,
                name=environment.name,
                target_path=environment.target_path,
                file_type=environment.file_type,
                dist_linked=bool(environment.dist_path),
                current=current.get(environment.id),
                deployed=deployed.get(environment.id),
                status=_environment_status(current.get(environment.id), latest.get(environment.id)),
                can_edit=scope.can(Capability.EDIT_VALUES, item),
                can_check_dist=scope.can(Capability.DEPLOY, item),
            )
        )
    return tuple(rows)


async def load_service_history(db: AsyncSession, header: ServiceHeader) -> tuple[HistoryRow, ...]:
    """The newest real runs of the service, limited to the results in the caller's scope."""
    rows = await load_history(
        db, {header.id}, header.visible_environment_ids, header.deployment_full
    )
    return tuple(rows)


async def load_post_deploy(
    db: AsyncSession, header: ServiceHeader, history: tuple[HistoryRow, ...]
) -> PostDeployPanel:
    """The latest visible run's post-deploy outcome and, for the superadmin, the stored hook."""
    last: PostDeployRun | None = None
    if history:
        newest = history[0]
        run = await db.get(DeployRun, newest.id)
        if run is not None:
            last = PostDeployRun(
                started_at=run.started_at,
                who=newest.who,
                status=run.post_deploy_status,
                exit_code=run.post_deploy_exit_code,
                output=run.post_deploy_output if header.can_view_output else None,
                reason=run.post_deploy_reason,
                trigger_label=newest.trigger_label,
            )
    hook: HookSettings | None = None
    if header.can_view_command:
        stored = (
            await db.execute(
                select(ServiceHook).where(
                    ServiceHook.service_id == header.id, ServiceHook.type == HookType.POST_DEPLOY
                )
            )
        ).scalar_one_or_none()
        if stored is not None:
            hook = HookSettings(
                commands=stored.commands,
                working_directory=stored.working_directory,
                timeout_seconds=stored.timeout_seconds,
                run_only_if_changed=stored.run_only_if_changed,
            )
    return PostDeployPanel(last=last, hook=hook)
