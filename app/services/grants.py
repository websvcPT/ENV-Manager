"""Access grants: the tree to choose from, a user's grants and audited changes to them.

A grant gives a user a project, a deployment or specific environments and, through the tree,
everything added below later. Services are never a grant level. Only users of the admin and
viewer groups hold grants; superadmins reach everything.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessGrant,
    AuditResult,
    Deployment,
    Environment,
    GrantLevel,
    Project,
    Service,
    User,
    UserGroup,
)
from app.services.audit import AuditActor, record_actor_audit

MAX_ID_DIGITS = 15
SUPERADMIN_REFUSAL = "Superadmins reach every project, so they hold no grants."


class GrantError(Exception):
    """A refused grant operation. ``field`` names the form field the message belongs to."""

    def __init__(self, message: str, field: str | None = "grants") -> None:
        super().__init__(message)
        self.field = field


class StaleGrantsError(GrantError):
    """The user's grants changed since the page that asked for the change was loaded."""


@dataclass(frozen=True, order=True)
class GrantTarget:
    """One grantable item: a level and the id of the project, deployment or environment."""

    level: GrantLevel
    item_id: int

    @property
    def key(self) -> str:
        """The form value of the target, such as ``environment:7``."""
        return f"{self.level.value}:{self.item_id}"

    @classmethod
    def parse(cls, value: str) -> GrantTarget | None:
        """The target a form value names, or None when it is not of the form ``level:id``."""
        level_name, _, raw_id = value.strip().partition(":")
        try:
            level = GrantLevel(level_name)
        except ValueError:
            return None
        if not (raw_id.isascii() and raw_id.isdigit()) or len(raw_id) > MAX_ID_DIGITS:
            return None
        item_id = int(raw_id)
        return cls(level, item_id) if item_id > 0 else None


@dataclass(frozen=True)
class GrantEnvironment:
    """An environment in the grant tree."""

    id: int
    name: str


@dataclass(frozen=True)
class GrantService:
    """A service in the grant tree: context for its environments, not itself grantable."""

    id: int
    name: str
    environments: tuple[GrantEnvironment, ...]


@dataclass(frozen=True)
class GrantDeployment:
    """A deployment in the grant tree."""

    id: int
    name: str
    services: tuple[GrantService, ...]


@dataclass(frozen=True)
class GrantProject:
    """A project in the grant tree."""

    id: int
    name: str
    deployments: tuple[GrantDeployment, ...]


@dataclass(frozen=True)
class GrantChange:
    """What a change of a user's grants added and removed, in a stable order."""

    added: tuple[GrantTarget, ...]
    removed: tuple[GrantTarget, ...]

    @property
    def changed(self) -> bool:
        """Whether anything was added or removed."""
        return bool(self.added or self.removed)


def parse_targets(values: Iterable[str]) -> set[GrantTarget]:
    """The targets named by form values, or ``GrantError`` when one does not parse."""
    targets: set[GrantTarget] = set()
    for value in values:
        target = GrantTarget.parse(value)
        if target is None:
            raise GrantError("A selected item is not valid. Reload the page and choose again.")
        targets.add(target)
    return targets


async def load_grant_tree(
    db: AsyncSession, project_id: int | None = None
) -> tuple[GrantProject, ...]:
    """Every project (or only ``project_id``) with its deployments, services and environments.

    Projects, deployments, services and environments are read in four queries whatever the size
    of the tree. Projects and their children are ordered by name.
    """
    project_query = select(Project.id, Project.name)
    deployment_query = select(Deployment.id, Deployment.project_id, Deployment.name)
    service_query = select(Service.id, Service.deployment_id, Service.name)
    environment_query = select(Environment.id, Environment.service_id, Environment.name)
    if project_id is not None:
        project_query = project_query.where(Project.id == project_id)
        deployment_query = deployment_query.where(Deployment.project_id == project_id)
        service_query = service_query.join(
            Deployment, Deployment.id == Service.deployment_id
        ).where(Deployment.project_id == project_id)
        environment_query = (
            environment_query.join(Service, Service.id == Environment.service_id)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .where(Deployment.project_id == project_id)
        )
    projects = (await db.execute(project_query)).all()
    deployments = (await db.execute(deployment_query)).all()
    services = (await db.execute(service_query)).all()
    environments = (await db.execute(environment_query)).all()
    by_service: dict[int, list[GrantEnvironment]] = {}
    for environment_id, service_id, name in environments:
        by_service.setdefault(service_id, []).append(GrantEnvironment(environment_id, name))
    by_deployment: dict[int, list[GrantService]] = {}
    for service_id, deployment_id, name in services:
        ordered = tuple(sorted(by_service.get(service_id, []), key=lambda item: item.name.lower()))
        by_deployment.setdefault(deployment_id, []).append(GrantService(service_id, name, ordered))
    by_project: dict[int, list[GrantDeployment]] = {}
    for deployment_id, project_id, name in deployments:
        ordered_services = tuple(
            sorted(by_deployment.get(deployment_id, []), key=lambda item: item.name.lower())
        )
        by_project.setdefault(project_id, []).append(
            GrantDeployment(deployment_id, name, ordered_services)
        )
    return tuple(
        GrantProject(
            project_id,
            name,
            tuple(sorted(by_project.get(project_id, []), key=lambda item: item.name.lower())),
        )
        for project_id, name in sorted(projects, key=lambda row: row[1].lower())
    )


def target_of(grant: AccessGrant) -> GrantTarget:
    """The target a stored grant row points at."""
    ids = {
        GrantLevel.PROJECT: grant.project_id,
        GrantLevel.DEPLOYMENT: grant.deployment_id,
        GrantLevel.ENVIRONMENT: grant.environment_id,
    }
    item_id = ids[grant.level]
    if item_id is None:
        raise ValueError(f"{grant.level.value} grant has no item")
    return GrantTarget(grant.level, item_id)


async def list_grants(db: AsyncSession, user_id: int) -> frozenset[GrantTarget]:
    """The direct grants of a user."""
    rows = (await db.execute(select(AccessGrant).where(AccessGrant.user_id == user_id))).scalars()
    return frozenset(target_of(grant) for grant in rows)


async def describe_targets(
    db: AsyncSession, targets: Collection[GrantTarget]
) -> dict[GrantTarget, str]:
    """A readable path for every target that still exists, such as ``rentals/prod/mysql/.env.db``.

    Targets that do not exist are left out, which is how callers detect a deleted item.
    """
    wanted = {level: {t.item_id for t in targets if t.level is level} for level in GrantLevel}
    labels: dict[GrantTarget, str] = {}
    if wanted[GrantLevel.PROJECT]:
        project_rows = await db.execute(
            select(Project.id, Project.name).where(Project.id.in_(wanted[GrantLevel.PROJECT]))
        )
        for item_id, name in project_rows:
            labels[GrantTarget(GrantLevel.PROJECT, item_id)] = name
    if wanted[GrantLevel.DEPLOYMENT]:
        deployment_rows = await db.execute(
            select(Deployment.id, Project.name, Deployment.name)
            .join(Project, Project.id == Deployment.project_id)
            .where(Deployment.id.in_(wanted[GrantLevel.DEPLOYMENT]))
        )
        for item_id, project, name in deployment_rows:
            labels[GrantTarget(GrantLevel.DEPLOYMENT, item_id)] = f"{project}/{name}"
    if wanted[GrantLevel.ENVIRONMENT]:
        environment_rows = await db.execute(
            select(Environment.id, Project.name, Deployment.name, Service.name, Environment.name)
            .join(Service, Service.id == Environment.service_id)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .join(Project, Project.id == Deployment.project_id)
            .where(Environment.id.in_(wanted[GrantLevel.ENVIRONMENT]))
        )
        for item_id, project, deployment, service, name in environment_rows:
            labels[GrantTarget(GrantLevel.ENVIRONMENT, item_id)] = (
                f"{project}/{deployment}/{service}/{name}"
            )
    return labels


async def require_existing(
    db: AsyncSession, targets: Collection[GrantTarget]
) -> dict[GrantTarget, str]:
    """The paths of the targets, or ``GrantError`` when one of the items no longer exists."""
    labels = await describe_targets(db, targets)
    if len(labels) != len(targets):
        raise GrantError("A selected item no longer exists. Reload the page and choose again.")
    return labels


async def record_grant_change(
    db: AsyncSession,
    actor: AuditActor,
    user: User,
    action: str,
    target: GrantTarget,
    path: str,
) -> None:
    """Audit one added or removed grant.

    The entry carries no item scope, so only the superadmin sees it.
    """
    await record_actor_audit(
        db,
        actor,
        action=action,
        result=AuditResult.SUCCESS,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"level": target.level.value, "item": path},
    )


def add_grant_row(db: AsyncSession, user: User, target: GrantTarget) -> None:
    """Stage the grant row of a target for the user."""
    column = {f"{target.level.value}_id": target.item_id}
    db.add(AccessGrant(user_id=user.id, level=target.level, **column))


async def set_user_grants(
    db: AsyncSession,
    user: User,
    desired: Collection[GrantTarget],
    *,
    actor: AuditActor,
    base: Collection[GrantTarget] | None = None,
    held: Collection[GrantTarget] | None = None,
) -> GrantChange:
    """Make the user's direct grants exactly ``desired``, auditing every grant added or removed.

    ``base`` is the set the person edited from; when the stored set differs from it the change
    is refused so a concurrent edit is never undone silently. ``held`` is the stored set when
    the caller read it a moment ago in the same request; without it it is read here. Superadmins
    hold no grants. The change is effective at once because scopes are loaded per request.
    """
    if user.group is UserGroup.SUPERADMIN:
        raise GrantError(SUPERADMIN_REFUSAL)
    wanted = set(desired)
    current = set(held) if held is not None else set(await list_grants(db, user.id))
    if base is not None and set(base) != current:
        raise StaleGrantsError("This user's grants changed since the page was loaded.")
    added = sorted(wanted - current)
    removed = sorted(current - wanted)
    if not added and not removed:
        return GrantChange((), ())
    labels = await require_existing(db, added)
    removed_labels = await describe_targets(db, removed)
    for target in added:
        add_grant_row(db, user, target)
        await record_grant_change(db, actor, user, "grant.add", target, labels[target])
    if removed:
        stored = (
            await db.execute(select(AccessGrant).where(AccessGrant.user_id == user.id))
        ).scalars()
        for grant in stored:
            target = target_of(grant)
            if target in removed:
                await db.delete(grant)
                path = removed_labels.get(target, target.key)
                await record_grant_change(db, actor, user, "grant.remove", target, path)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise StaleGrantsError("This user's grants changed since the page was loaded.") from error
    return GrantChange(tuple(added), tuple(removed))


async def toggle_user_grant(
    db: AsyncSession,
    user: User,
    target: GrantTarget,
    *,
    was: bool,
    actor: AuditActor,
    held: Collection[GrantTarget] | None = None,
) -> GrantChange:
    """Flip one direct grant of a user, auditing the grant added or removed.

    ``was`` is whether the person saw the grant as held; when the stored state differs the flip
    is refused, so a cell on an old page never undoes a change made meanwhile. Everything else
    goes through ``set_user_grants``, so a concurrent change to any other grant of the user is
    caught too. ``held`` is the stored set when the caller read it a moment ago in the same
    request; without it it is read here.
    """
    if user.group is UserGroup.SUPERADMIN:
        raise GrantError(SUPERADMIN_REFUSAL)
    current = set(held) if held is not None else set(await list_grants(db, user.id))
    if (target in current) != was:
        raise StaleGrantsError("This grant changed since the page was loaded.")
    return await set_user_grants(
        db, user, current ^ {target}, actor=actor, base=current, held=current
    )


async def projects_reached(
    db: AsyncSession, user_ids: Collection[int] | None = None
) -> dict[int, set[int]]:
    """The ids of the projects each user's grants reach, by user id.

    A grant reaches a project when it is on the project or on anything inside it. With
    ``user_ids`` only the grants of those users are read, in the same three queries.
    """
    only = [AccessGrant.user_id.in_(user_ids)] if user_ids is not None else []
    on_project = select(AccessGrant.user_id, AccessGrant.project_id).where(
        AccessGrant.project_id.is_not(None), *only
    )
    on_deployment = (
        select(AccessGrant.user_id, Deployment.project_id)
        .join(Deployment, Deployment.id == AccessGrant.deployment_id)
        .where(*only)
    )
    on_environment = (
        select(AccessGrant.user_id, Deployment.project_id)
        .join(Environment, Environment.id == AccessGrant.environment_id)
        .join(Service, Service.id == Environment.service_id)
        .join(Deployment, Deployment.id == Service.deployment_id)
        .where(*only)
    )
    reached: dict[int, set[int]] = {}
    for query in (on_project, on_deployment, on_environment):
        for user_id, project_id in (await db.execute(query)).all():
            if project_id is not None:
                reached.setdefault(user_id, set()).add(project_id)
    return reached
