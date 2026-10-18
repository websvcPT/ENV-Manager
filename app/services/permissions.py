"""Permission and scope engine.

The group sets what a user can do (capability matrix), access grants set where. Effective
access is the union of the user's grants, evaluated against the live structure tree so items
added later are covered by project and deployment grants. An API token acts as its owner and can
only be narrowed further by its restrictions, never widened. Nothing here is cached: scopes are
loaded from the database for each request, so a revoked grant, a disabled owner or a changed
group applies at once.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, and_, false, null, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccessGrant,
    ApiToken,
    ApiTokenRestriction,
    AuditEntry,
    Deployment,
    Environment,
    GrantLevel,
    Project,
    RestrictionLevel,
    Service,
    User,
    UserGroup,
)
from app.models.types import utc_now

ItemLevel = RestrictionLevel


class Capability(StrEnum):
    """Things a group may be allowed to do. Restore is part of editing, dist checks of deploy."""

    MANAGE_USERS = "manage_users"
    MANAGE_GRANTS = "manage_grants"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_STRUCTURE = "manage_structure"
    EDIT_VALUES = "edit_values"
    DEPLOY = "deploy"
    VIEW = "view"
    REVEAL_SECRETS = "reveal_secrets"
    VIEW_POST_DEPLOY_STATUS = "view_post_deploy_status"
    VIEW_POST_DEPLOY_OUTPUT = "view_post_deploy_output"
    VIEW_POST_DEPLOY_COMMAND = "view_post_deploy_command"
    VIEW_AUDIT = "view_audit"
    MANAGE_OWN_TOKENS = "manage_own_tokens"


class Target(StrEnum):
    """What a capability is checked against besides the group."""

    NONE = "none"
    ITEM = "item"
    ENVIRONMENT = "environment"


_ALL_GROUPS = frozenset(UserGroup)
_ADMINS = frozenset({UserGroup.SUPERADMIN, UserGroup.ADMIN})
_SUPERADMIN = frozenset({UserGroup.SUPERADMIN})

CAPABILITY_GROUPS: dict[Capability, frozenset[UserGroup]] = {
    Capability.MANAGE_USERS: _SUPERADMIN,
    Capability.MANAGE_GRANTS: _SUPERADMIN,
    Capability.MANAGE_SETTINGS: _SUPERADMIN,
    Capability.MANAGE_STRUCTURE: _SUPERADMIN,
    Capability.EDIT_VALUES: _ADMINS,
    Capability.DEPLOY: _ADMINS,
    Capability.VIEW: _ALL_GROUPS,
    Capability.REVEAL_SECRETS: _ADMINS,
    Capability.VIEW_POST_DEPLOY_STATUS: _ALL_GROUPS,
    Capability.VIEW_POST_DEPLOY_OUTPUT: _ADMINS,
    Capability.VIEW_POST_DEPLOY_COMMAND: _SUPERADMIN,
    Capability.VIEW_AUDIT: _ADMINS,
    Capability.MANAGE_OWN_TOKENS: _ADMINS,
}

CAPABILITY_TARGETS: dict[Capability, Target] = {
    Capability.MANAGE_USERS: Target.NONE,
    Capability.MANAGE_GRANTS: Target.NONE,
    Capability.MANAGE_SETTINGS: Target.NONE,
    Capability.MANAGE_STRUCTURE: Target.NONE,
    Capability.EDIT_VALUES: Target.ENVIRONMENT,
    Capability.DEPLOY: Target.ENVIRONMENT,
    Capability.VIEW: Target.ITEM,
    Capability.REVEAL_SECRETS: Target.ENVIRONMENT,
    Capability.VIEW_POST_DEPLOY_STATUS: Target.ITEM,
    Capability.VIEW_POST_DEPLOY_OUTPUT: Target.ITEM,
    Capability.VIEW_POST_DEPLOY_COMMAND: Target.NONE,
    Capability.VIEW_AUDIT: Target.NONE,
    Capability.MANAGE_OWN_TOKENS: Target.NONE,
}


@dataclass(frozen=True)
class ItemRef:
    """A structure item with the ids of its ancestors. Levels below the item are None."""

    level: ItemLevel
    project_id: int
    deployment_id: int | None = None
    service_id: int | None = None
    environment_id: int | None = None

    @property
    def item_id(self) -> int:
        """The id of the item itself."""
        ids = {
            ItemLevel.PROJECT: self.project_id,
            ItemLevel.DEPLOYMENT: self.deployment_id,
            ItemLevel.SERVICE: self.service_id,
            ItemLevel.ENVIRONMENT: self.environment_id,
        }
        item_id = ids[self.level]
        if item_id is None:
            raise ValueError(f"{self.level.value} reference has no id")
        return item_id


@dataclass(frozen=True)
class TargetSet:
    """A set of tree targets. An item is covered when it or one of its ancestors is a target."""

    unrestricted: bool = False
    project_ids: frozenset[int] = frozenset()
    deployment_ids: frozenset[int] = frozenset()
    service_ids: frozenset[int] = frozenset()
    environment_ids: frozenset[int] = frozenset()

    def covers(self, item: ItemRef) -> bool:
        """Whether the item is at or below one of the targets."""
        if self.unrestricted:
            return True
        return (
            item.project_id in self.project_ids
            or (item.deployment_id is not None and item.deployment_id in self.deployment_ids)
            or (item.service_id is not None and item.service_id in self.service_ids)
            or (item.environment_id is not None and item.environment_id in self.environment_ids)
        )


@dataclass(frozen=True)
class AccessScope:
    """What one caller may reach: a user session, or an API token acting as its owner."""

    user_id: int
    group: UserGroup
    grants: TargetSet
    narrowing: TargetSet | None = None
    token_id: int | None = None
    active: bool = True

    @property
    def is_token(self) -> bool:
        """Whether the caller is an API token."""
        return self.token_id is not None

    def covers(self, item: ItemRef) -> bool:
        """Whether the item is in the caller's scope: owner grants intersected with narrowing."""
        if not self.active or not self.grants.covers(item):
            return False
        return self.narrowing is None or self.narrowing.covers(item)

    def can(self, capability: Capability, item: ItemRef | None = None) -> bool:
        """Whether the group allows the capability and, where it needs one, the item is in scope.

        Environment capabilities need an environment item. Capabilities without a target are
        refused for tokens, which only ever act on items.
        """
        if not self.active or self.group not in CAPABILITY_GROUPS[capability]:
            return False
        target = CAPABILITY_TARGETS[capability]
        if target is Target.NONE:
            return not self.is_token
        if item is None:
            raise ValueError(f"{capability.value} needs an item")
        if target is Target.ENVIRONMENT and item.level is not ItemLevel.ENVIRONMENT:
            raise ValueError(f"{capability.value} applies to environments")
        return self.covers(item)


DENIED_TARGETS = TargetSet()


@dataclass(frozen=True)
class Visibility:
    """Ids a caller may see per level: in scope, and ancestors shown as context (names only).

    ``actor_user_id`` is the user whose own audit entries are always visible (an admin sees what
    they or their own API tokens did, even outside their items), or None when the caller has no
    audit log of their own.
    """

    unrestricted: bool
    full: dict[ItemLevel, frozenset[int]]
    context: dict[ItemLevel, frozenset[int]]
    actor_user_id: int | None = None

    def can_see(self, level: ItemLevel, item_id: int) -> bool:
        """Whether the item is in scope."""
        return item_id in self.full[level]

    def is_context_only(self, level: ItemLevel, item_id: int) -> bool:
        """Whether the item is only an ancestor of something in scope."""
        return item_id in self.context[level]

    def includes_audit_entry(
        self,
        *,
        actor_user_id: int | None,
        project_id: int | None,
        deployment_id: int | None,
        service_id: int | None,
        environment_id: int | None,
    ) -> bool:
        """Whether an audit entry with this actor and these scope ids is visible.

        An entry the caller made (their user, or one of their own tokens, which record the owner
        as the user) is always visible. For other entries the most specific scope id decides.
        Entries without scope ids and entries for items that no longer exist are visible only
        without restriction.
        """
        if self.unrestricted:
            return True
        if self.actor_user_id is not None and actor_user_id == self.actor_user_id:
            return True
        for level, item_id in (
            (ItemLevel.ENVIRONMENT, environment_id),
            (ItemLevel.SERVICE, service_id),
            (ItemLevel.DEPLOYMENT, deployment_id),
            (ItemLevel.PROJECT, project_id),
        ):
            if item_id is not None:
                return self.can_see(level, item_id)
        return False

    def audit_entry_clause(self) -> ColumnElement[bool]:
        """The SQL form of ``includes_audit_entry`` for queries over ``AuditEntry``.

        The same rule: the caller's own entries are always visible, otherwise the most specific
        scope id of the entry decides, and an entry with no scope id or one whose item no longer
        exists is visible only without restriction.
        """
        if self.unrestricted:
            return true()
        no_environment = AuditEntry.environment_id.is_(None)
        no_service = AuditEntry.service_id.is_(None)
        no_deployment = AuditEntry.deployment_id.is_(None)
        own = (
            AuditEntry.actor_user_id == self.actor_user_id
            if self.actor_user_id is not None
            else false()
        )
        return or_(
            own,
            and_(
                AuditEntry.environment_id.is_not(None),
                AuditEntry.environment_id.in_(self.full[ItemLevel.ENVIRONMENT]),
            ),
            and_(
                no_environment,
                AuditEntry.service_id.is_not(None),
                AuditEntry.service_id.in_(self.full[ItemLevel.SERVICE]),
            ),
            and_(
                no_environment,
                no_service,
                AuditEntry.deployment_id.is_not(None),
                AuditEntry.deployment_id.in_(self.full[ItemLevel.DEPLOYMENT]),
            ),
            and_(
                no_environment,
                no_service,
                no_deployment,
                AuditEntry.project_id.is_not(None),
                AuditEntry.project_id.in_(self.full[ItemLevel.PROJECT]),
            ),
        )


def _item_query(level: ItemLevel) -> tuple[Any, Any]:
    """A select of (project, deployment, service, environment) ids and the level's id column."""
    if level is ItemLevel.PROJECT:
        return select(Project.id, null(), null(), null()), Project.id
    if level is ItemLevel.DEPLOYMENT:
        return select(Deployment.project_id, Deployment.id, null(), null()), Deployment.id
    if level is ItemLevel.SERVICE:
        service_stmt = select(Deployment.project_id, Deployment.id, Service.id, null()).join(
            Deployment, Deployment.id == Service.deployment_id
        )
        return service_stmt, Service.id
    environment_stmt = (
        select(Deployment.project_id, Deployment.id, Service.id, Environment.id)
        .join(Service, Service.id == Environment.service_id)
        .join(Deployment, Deployment.id == Service.deployment_id)
    )
    return environment_stmt, Environment.id


async def load_items(
    db: AsyncSession, level: ItemLevel, item_ids: set[int] | None = None
) -> list[ItemRef]:
    """References with ancestry for every item of a level, or only the given ids."""
    stmt, id_column = _item_query(level)
    if item_ids is not None:
        stmt = stmt.where(id_column.in_(item_ids))
    rows = (await db.execute(stmt)).all()
    return [
        ItemRef(
            level=level,
            project_id=project_id,
            deployment_id=deployment_id,
            service_id=service_id,
            environment_id=environment_id,
        )
        for project_id, deployment_id, service_id, environment_id in rows
    ]


async def load_item(db: AsyncSession, level: ItemLevel, item_id: int) -> ItemRef | None:
    """The reference for one item, or None when it does not exist."""
    items = await load_items(db, level, {item_id})
    return items[0] if items else None


def token_is_usable(token: ApiToken, owner: User, *, now: datetime | None = None) -> bool:
    """Whether the token may be used: not revoked or expired, owner active and not a viewer."""
    moment = now or utc_now()
    return (
        token.revoked_at is None
        and (token.expires_at is None or token.expires_at > moment)
        and owner.is_active
        and owner.group is not UserGroup.VIEWER
    )


async def _owner_targets(db: AsyncSession, user: User) -> TargetSet:
    """The targets a user's grants cover. Superadmins are unrestricted."""
    if user.group is UserGroup.SUPERADMIN:
        return TargetSet(unrestricted=True)
    grants = (
        (await db.execute(select(AccessGrant).where(AccessGrant.user_id == user.id)))
        .scalars()
        .all()
    )
    return TargetSet(
        project_ids=frozenset(
            g.project_id for g in grants if g.level is GrantLevel.PROJECT and g.project_id
        ),
        deployment_ids=frozenset(
            g.deployment_id for g in grants if g.level is GrantLevel.DEPLOYMENT and g.deployment_id
        ),
        environment_ids=frozenset(
            g.environment_id
            for g in grants
            if g.level is GrantLevel.ENVIRONMENT and g.environment_id
        ),
    )


def cli_scope() -> AccessScope:
    """The scope of the command line tool: an unrestricted superadmin that is no user row.

    The command line runs inside the container with access to the same data, so it is trusted
    like the superadmin. The user id 0 matches no user; what the CLI writes is audited with the
    actor ``cli``.
    """
    return AccessScope(0, UserGroup.SUPERADMIN, TargetSet(unrestricted=True))


async def load_user_scope(db: AsyncSession, user: User) -> AccessScope:
    """The scope of a signed-in user. A disabled user gets a scope that allows nothing."""
    if not user.is_active:
        return AccessScope(user.id, user.group, DENIED_TARGETS, active=False)
    return AccessScope(user.id, user.group, await _owner_targets(db, user))


async def load_token_scope(
    db: AsyncSession, token: ApiToken, *, now: datetime | None = None
) -> AccessScope | None:
    """The scope of an API token, or None when it must not be used.

    The token gets its owner's current grants intersected with its restrictions. With no
    restriction rows it is limited only by the owner.
    """
    owner = await db.get(User, token.owner_id)
    if owner is None or not token_is_usable(token, owner, now=now):
        return None
    rows = (
        (
            await db.execute(
                select(ApiTokenRestriction).where(ApiTokenRestriction.token_id == token.id)
            )
        )
        .scalars()
        .all()
    )
    narrowing: TargetSet | None = None
    if rows:
        narrowing = TargetSet(
            project_ids=frozenset(
                r.project_id for r in rows if r.level is RestrictionLevel.PROJECT and r.project_id
            ),
            deployment_ids=frozenset(
                r.deployment_id
                for r in rows
                if r.level is RestrictionLevel.DEPLOYMENT and r.deployment_id
            ),
            service_ids=frozenset(
                r.service_id for r in rows if r.level is RestrictionLevel.SERVICE and r.service_id
            ),
            environment_ids=frozenset(
                r.environment_id
                for r in rows
                if r.level is RestrictionLevel.ENVIRONMENT and r.environment_id
            ),
        )
    return AccessScope(
        owner.id, owner.group, await _owner_targets(db, owner), narrowing, token_id=token.id
    )


async def compute_visibility(db: AsyncSession, scope: AccessScope) -> Visibility:
    """Everything the caller sees: items in scope plus their ancestors as context only."""
    full: dict[ItemLevel, set[int]] = {level: set() for level in ItemLevel}
    context: dict[ItemLevel, set[int]] = {level: set() for level in ItemLevel}
    for level in ItemLevel:
        for item in await load_items(db, level):
            if not scope.covers(item):
                continue
            full[level].add(item.item_id)
            if item.deployment_id is not None and level is not ItemLevel.DEPLOYMENT:
                context[ItemLevel.DEPLOYMENT].add(item.deployment_id)
            if item.service_id is not None and level is not ItemLevel.SERVICE:
                context[ItemLevel.SERVICE].add(item.service_id)
            if level is not ItemLevel.PROJECT:
                context[ItemLevel.PROJECT].add(item.project_id)
    has_audit_log = (
        scope.active
        and not scope.is_token
        and scope.group in CAPABILITY_GROUPS[Capability.VIEW_AUDIT]
    )
    return Visibility(
        unrestricted=scope.active and scope.grants.unrestricted and scope.narrowing is None,
        full={level: frozenset(ids) for level, ids in full.items()},
        context={level: frozenset(context[level] - full[level]) for level in ItemLevel},
        actor_user_id=scope.user_id if has_audit_log else None,
    )


async def permitted_environment_ids(
    db: AsyncSession,
    scope: AccessScope,
    capability: Capability,
    *,
    service_id: int | None = None,
) -> list[int]:
    """Ids of the environments the caller may use the capability on, optionally of one service.

    A deploy runs for one service: it is allowed when this list is not empty and covers only
    the environments in scope.
    """
    environments = await load_items(db, ItemLevel.ENVIRONMENT)
    return sorted(
        item.item_id
        for item in environments
        if (service_id is None or item.service_id == service_id) and scope.can(capability, item)
    )
