"""API tokens: creation with a one-time secret, listing, revocation and the rules around them.

A token belongs to an admin or superadmin and acts as its owner, narrowed by at most one
restriction (a project, deployment or service the owner reaches). The secret is generated here,
returned to the caller once and never stored: only its SHA-256 hash and a short prefix that lets
a person recognise the token in the list are kept. Neither the secret nor its hash reaches an
audit entry or a log line.

A token is usable until it is revoked or expires. It also stops while its owner is disabled or a
viewer, but disabling an owner or moving them to viewer revokes their tokens for good, so
enabling or promoting the user never brings an old token back.
"""

import hashlib
import secrets
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditResult,
    Deployment,
    Environment,
    Project,
    RestrictionLevel,
    Service,
    User,
    UserGroup,
)
from app.models.types import utc_now
from app.services.audit import AuditActor, record_actor_audit
from app.services.permissions import (
    AccessScope,
    Capability,
    ItemLevel,
    Visibility,
    compute_visibility,
    load_items,
    token_is_usable,
)

OPERATION_DEPLOY = "deploy"
OPERATION_READ_LAST_DEPLOYMENT = "read_last_deployment"

BEARER_PREFIX = "envm_"
SECRET_RANDOM_BYTES = 24
PREFIX_LENGTH = len(BEARER_PREFIX) + 8
MAX_NAME_LENGTH = 100
MAX_SECRET_LENGTH = 200
ALL_PROJECTS = "All projects"
HIDDEN_ITEM = "An item you cannot access"
LABELS_SHOWN = 3

REASON_REQUESTED = "requested"
REASON_USER_DISABLED = "user_disabled"
REASON_MOVED_TO_VIEWER = "moved_to_viewer"


@dataclass(frozen=True)
class Operation:
    """One thing a token may be allowed to do, as the create dialog lists it."""

    key: str
    label: str
    description: str


OPERATIONS = (
    Operation(OPERATION_DEPLOY, "Deploy", "Deploy a service."),
    Operation(
        OPERATION_READ_LAST_DEPLOYMENT,
        "Read last deployment",
        "Read the last deploy result of a service.",
    ),
)
_OPERATION_LABELS = {operation.key: operation.label for operation in OPERATIONS}


class TokenStatus(StrEnum):
    """How an API token stands: usable, or why it is not."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    STOPPED = "stopped"


class TokenError(Exception):
    """A refused token operation. ``field`` names the form field a message belongs to."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class RestrictionTarget:
    """The item a token is restricted to: a level and the id of a project, deployment or service."""

    level: RestrictionLevel
    item_id: int

    @property
    def key(self) -> str:
        """The form value of the target, such as ``service:7``."""
        return f"{self.level.value}:{self.item_id}"

    @classmethod
    def parse(cls, value: str) -> RestrictionTarget | None:
        """The target a form value names, or None when it is not ``level:id`` of a choice level."""
        level_name, _, raw_id = value.strip().partition(":")
        try:
            level = RestrictionLevel(level_name)
        except ValueError:
            return None
        if level is RestrictionLevel.ENVIRONMENT or not raw_id.isascii() or not raw_id.isdigit():
            return None
        if len(raw_id) > 15:
            return None
        return cls(level, int(raw_id))


@dataclass(frozen=True)
class RestrictionChoice:
    """One item the restriction select offers."""

    key: str
    label: str


@dataclass(frozen=True)
class RestrictionChoices:
    """The items an owner may restrict a token to, grouped by level."""

    projects: tuple[RestrictionChoice, ...]
    deployments: tuple[RestrictionChoice, ...]
    services: tuple[RestrictionChoice, ...]

    @property
    def keys(self) -> frozenset[str]:
        """The form values that are acceptable."""
        groups = (self.projects, self.deployments, self.services)
        return frozenset(choice.key for group in groups for choice in group)


@dataclass(frozen=True)
class CreatedToken:
    """A new token and its secret. The secret exists only in this value."""

    token: ApiToken
    secret: str
    service_id: int | None


@dataclass(frozen=True)
class TokenRow:
    """One token of the list. ``revocable`` is True while revoking still changes something."""

    id: int
    name: str
    prefix: str
    owner_id: int
    owner_name: str
    owner_email: str
    operations: tuple[str, ...]
    restriction: str
    created_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    expires_at: datetime | None
    status: TokenStatus
    reason: str | None
    revocable: bool


def generate_secret() -> str:
    """A new token secret: a recognisable prefix and 192 bits from the system's secure source."""
    return BEARER_PREFIX + secrets.token_hex(SECRET_RANDOM_BYTES)


def hash_secret(secret: str) -> str:
    """The SHA-256 hex digest that identifies a token. The secret itself has high entropy."""
    return hashlib.sha256(secret.encode()).hexdigest()


async def find_token_by_secret(db: AsyncSession, secret: str) -> ApiToken | None:
    """The token a presented secret belongs to, or None. Usability is checked separately."""
    if not secret or len(secret) > MAX_SECRET_LENGTH:
        return None
    return (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_secret(secret)))
    ).scalar_one_or_none()


def operation_labels(keys: Iterable[str]) -> tuple[str, ...]:
    """The display labels of operation keys, in the order the dialog lists them."""
    chosen = set(keys)
    return tuple(operation.label for operation in OPERATIONS if operation.key in chosen)


def token_status(
    token: ApiToken, owner: User, *, now: datetime | None = None
) -> tuple[TokenStatus, str | None]:
    """The status of a token and, for a stopped one, why: revoked, expired, stopped or active."""
    moment = now or utc_now()
    if token.revoked_at is not None:
        return TokenStatus.REVOKED, None
    if token.expires_at is not None and token.expires_at <= moment:
        return TokenStatus.EXPIRED, None
    if not token_is_usable(token, owner, now=moment):
        reason = "User disabled" if not owner.is_active else "Viewers cannot use tokens"
        return TokenStatus.STOPPED, reason
    return TokenStatus.ACTIVE, None


def is_live(token: ApiToken, *, now: datetime | None = None) -> bool:
    """Whether the token can still become usable: not revoked and not expired."""
    moment = now or utc_now()
    return token.revoked_at is None and (token.expires_at is None or token.expires_at > moment)


def usage_example(base_url: str, operations: Collection[str], service_id: int | None) -> str:
    """Example requests for the operations of a token, with the secret read from ``ENVM_TOKEN``."""
    base = base_url.rstrip("/")
    service = str(service_id) if service_id is not None else "SERVICE_ID"
    blocks: list[str] = []
    if OPERATION_DEPLOY in operations:
        blocks.append(
            f"curl -X POST {base}/api/v1/services/{service}/deploy \\\n"
            '  -H "Authorization: Bearer $ENVM_TOKEN" \\\n'
            '  -H "Content-Type: application/json" \\\n'
            "  -d '{\"dry_run\": true}'"
        )
    if OPERATION_READ_LAST_DEPLOYMENT in operations:
        blocks.append(
            f"curl {base}/api/v1/services/{service}/deployments/last \\\n"
            '  -H "Authorization: Bearer $ENVM_TOKEN"'
        )
    return "\n\n".join(blocks)


async def restriction_choices(db: AsyncSession, scope: AccessScope) -> RestrictionChoices:
    """The projects, deployments and services that hold a file the owner may deploy.

    A restriction narrows what the owner reaches, so an item is offered when at least one of its
    files is within the owner's reach, which also serves owners with grants on single files.
    """
    projects: set[int] = set()
    deployments: set[int] = set()
    services: set[int] = set()
    for item in await load_items(db, ItemLevel.ENVIRONMENT):
        if scope.can(Capability.DEPLOY, item):
            projects.add(item.project_id)
            if item.deployment_id is not None:
                deployments.add(item.deployment_id)
            if item.service_id is not None:
                services.add(item.service_id)
    project_names = dict(
        (await db.execute(select(Project.id, Project.name).where(Project.id.in_(projects)))).all()
    )
    deployment_labels = {
        deployment_id: f"{project} / {name}"
        for deployment_id, project, name in (
            await db.execute(
                select(Deployment.id, Project.name, Deployment.name)
                .join(Project, Project.id == Deployment.project_id)
                .where(Deployment.id.in_(deployments))
            )
        ).all()
    }
    service_labels = {
        service_id: f"{project} / {deployment} / {name}"
        for service_id, project, deployment, name in (
            await db.execute(
                select(Service.id, Project.name, Deployment.name, Service.name)
                .join(Deployment, Deployment.id == Service.deployment_id)
                .join(Project, Project.id == Deployment.project_id)
                .where(Service.id.in_(services))
            )
        ).all()
    }

    def choices(level: RestrictionLevel, labels: dict[int, str]) -> tuple[RestrictionChoice, ...]:
        ordered = sorted(labels.items(), key=lambda pair: (pair[1].lower(), pair[0]))
        return tuple(
            RestrictionChoice(f"{level.value}:{item_id}", label) for item_id, label in ordered
        )

    return RestrictionChoices(
        projects=choices(RestrictionLevel.PROJECT, project_names),
        deployments=choices(RestrictionLevel.DEPLOYMENT, deployment_labels),
        services=choices(RestrictionLevel.SERVICE, service_labels),
    )


def _clean_name(name: str) -> str:
    """The trimmed name, or a refusal naming the problem."""
    name = name.strip()
    if not name:
        raise TokenError("Enter a name.", "name")
    if len(name) > MAX_NAME_LENGTH or not name.isprintable():
        raise TokenError("Use 1 to 100 printable characters.", "name")
    return name


def _clean_operations(operations: Iterable[str]) -> list[str]:
    """The chosen operation keys in listing order, or a refusal."""
    chosen = set(operations)
    known = {operation.key for operation in OPERATIONS}
    if not chosen:
        raise TokenError("Choose at least one operation.", "operations")
    if not chosen <= known:
        raise TokenError("Choose from the listed operations.", "operations")
    return [operation.key for operation in OPERATIONS if operation.key in chosen]


def _expiry_moment(value: str, *, today: date) -> datetime | None:
    """The moment a token expires: the end of the chosen day in UTC, None for no expiry.

    The date must be today or later, so a token is never created already expired.
    """
    value = value.strip()
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise TokenError("Enter a valid date.", "expires") from None
    if day < today:
        raise TokenError("The expiry date must be today or later.", "expires")
    return datetime.combine(day, time(23, 59, 59), tzinfo=UTC)


async def _owned_names_taken(db: AsyncSession, owner: User, name: str) -> bool:
    """Whether the owner already has a token with this name that is not revoked."""
    return (
        await db.execute(
            select(ApiToken.id).where(
                ApiToken.owner_id == owner.id,
                ApiToken.revoked_at.is_(None),
                func.lower(ApiToken.name) == name.lower(),
            )
        )
    ).first() is not None


async def create_token(
    db: AsyncSession,
    owner: User,
    scope: AccessScope,
    *,
    name: str,
    operations: Iterable[str],
    restriction: str,
    expires: str,
    actor: AuditActor,
) -> CreatedToken:
    """Create a token for its owner and audit it; the secret is returned, never stored.

    The owner must be allowed to hold tokens (admin or superadmin). The name is unique among the
    owner's tokens that are not revoked, ignoring case, because history and the audit log label
    token actions with it. The restriction must be an item the owner can restrict to.
    """
    if not scope.can(Capability.MANAGE_OWN_TOKENS):
        raise TokenError("Your group cannot create API tokens.")
    name = _clean_name(name)
    chosen = _clean_operations(operations)
    expires_at = _expiry_moment(expires, today=utc_now().date())
    target: RestrictionTarget | None = None
    if restriction.strip():
        target = RestrictionTarget.parse(restriction)
        choices = await restriction_choices(db, scope)
        if target is None or target.key not in choices.keys:
            raise TokenError("Choose an item from the list.", "restriction")
    if await _owned_names_taken(db, owner, name):
        raise TokenError("You already have an active token with this name.", "name")
    secret = generate_secret()
    token = ApiToken(
        owner_id=owner.id,
        name=name,
        token_hash=hash_secret(secret),
        prefix=secret[:PREFIX_LENGTH],
        operations=chosen,
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    label: str | None = None
    if target is not None:
        db.add(
            ApiTokenRestriction(
                token_id=token.id,
                level=target.level,
                project_id=target.item_id if target.level is RestrictionLevel.PROJECT else None,
                deployment_id=(
                    target.item_id if target.level is RestrictionLevel.DEPLOYMENT else None
                ),
                service_id=target.item_id if target.level is RestrictionLevel.SERVICE else None,
            )
        )
        await db.flush()
        label = (await _restriction_labels(db, [token.id], None))[token.id]
    await record_actor_audit(
        db,
        actor,
        action="token.create",
        result=AuditResult.SUCCESS,
        target_type="token",
        target_id=token.id,
        target_name=token.name,
        details={
            "operations": chosen,
            "restriction": label,
            "expires": expires_at.date().isoformat() if expires_at else None,
        },
    )
    await db.commit()
    service_id = target.item_id if target and target.level is RestrictionLevel.SERVICE else None
    return CreatedToken(token, secret, service_id)


def _restricted_item_id(row: ApiTokenRestriction) -> int:
    """The id of the item a restriction row points at, whatever its level."""
    ids = {
        RestrictionLevel.PROJECT: row.project_id,
        RestrictionLevel.DEPLOYMENT: row.deployment_id,
        RestrictionLevel.SERVICE: row.service_id,
        RestrictionLevel.ENVIRONMENT: row.environment_id,
    }
    item_id = ids[row.level]
    if item_id is None:
        raise ValueError(f"{row.level.value} restriction has no id")
    return item_id


def _label_text(labels: list[str]) -> str:
    """The restriction column text: no restriction, the items, or the first few and a count."""
    if not labels:
        return ALL_PROJECTS
    ordered = sorted(labels, key=str.lower)
    shown = ", ".join(ordered[:LABELS_SHOWN])
    rest = len(ordered) - LABELS_SHOWN
    return f"{shown} and {rest} more" if rest > 0 else shown


async def _restriction_labels(
    db: AsyncSession, token_ids: Collection[int], visibility: Visibility | None
) -> dict[int, str]:
    """The restriction text of each token.

    With a ``visibility`` (the viewer is not the superadmin) an item the viewer cannot see is
    named as an inaccessible item, never by its name, because a restriction can outlive the
    owner's access to what it points at.
    """
    rows = (
        (
            await db.execute(
                select(ApiTokenRestriction).where(ApiTokenRestriction.token_id.in_(token_ids))
            )
        )
        .scalars()
        .all()
    )
    names: dict[tuple[RestrictionLevel, int], tuple[str, ItemLevel]] = {}
    project_ids = {row.project_id for row in rows if row.project_id is not None}
    deployment_ids = {row.deployment_id for row in rows if row.deployment_id is not None}
    service_ids = {row.service_id for row in rows if row.service_id is not None}
    environment_ids = {row.environment_id for row in rows if row.environment_id is not None}
    for project_id, name in (
        await db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids)))
    ).all():
        names[(RestrictionLevel.PROJECT, project_id)] = (name, ItemLevel.PROJECT)
    for deployment_id, project, name in (
        await db.execute(
            select(Deployment.id, Project.name, Deployment.name)
            .join(Project, Project.id == Deployment.project_id)
            .where(Deployment.id.in_(deployment_ids))
        )
    ).all():
        names[(RestrictionLevel.DEPLOYMENT, deployment_id)] = (
            f"{project} / {name}",
            ItemLevel.DEPLOYMENT,
        )
    for service_id, project, deployment, name in (
        await db.execute(
            select(Service.id, Project.name, Deployment.name, Service.name)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .join(Project, Project.id == Deployment.project_id)
            .where(Service.id.in_(service_ids))
        )
    ).all():
        names[(RestrictionLevel.SERVICE, service_id)] = (
            f"{project} / {deployment} / {name}",
            ItemLevel.SERVICE,
        )
    for environment_id, project, deployment, service, name in (
        await db.execute(
            select(Environment.id, Project.name, Deployment.name, Service.name, Environment.name)
            .join(Service, Service.id == Environment.service_id)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .join(Project, Project.id == Deployment.project_id)
            .where(Environment.id.in_(environment_ids))
        )
    ).all():
        names[(RestrictionLevel.ENVIRONMENT, environment_id)] = (
            f"{project} / {deployment} / {service} / {name}",
            ItemLevel.ENVIRONMENT,
        )
    labels: dict[int, list[str]] = {token_id: [] for token_id in token_ids}
    for row in rows:
        item_id = _restricted_item_id(row)
        found = names.get((row.level, item_id))
        if found is None:
            continue
        label, level = found
        shown = (
            visibility is None
            or visibility.can_see(level, item_id)
            or visibility.is_context_only(level, item_id)
        )
        labels[row.token_id].append(label if shown else HIDDEN_ITEM)
    return {token_id: _label_text(found) for token_id, found in labels.items()}


async def list_tokens(
    db: AsyncSession, viewer: User, scope: AccessScope, *, now: datetime | None = None
) -> list[TokenRow]:
    """The tokens the viewer manages, newest first: their own, or every token for a superadmin."""
    moment = now or utc_now()
    stmt = select(ApiToken, User).join(User, User.id == ApiToken.owner_id)
    if viewer.group is not UserGroup.SUPERADMIN:
        stmt = stmt.where(ApiToken.owner_id == viewer.id)
    found = (await db.execute(stmt.order_by(ApiToken.id.desc()))).all()
    visibility = (
        None if viewer.group is UserGroup.SUPERADMIN else await compute_visibility(db, scope)
    )
    labels = await _restriction_labels(db, [token.id for token, _ in found], visibility)
    rows: list[TokenRow] = []
    for token, owner in found:
        status, reason = token_status(token, owner, now=moment)
        rows.append(
            TokenRow(
                id=token.id,
                name=token.name,
                prefix=token.prefix,
                owner_id=owner.id,
                owner_name=owner.name,
                owner_email=owner.email,
                operations=operation_labels(token.operations),
                restriction=labels[token.id],
                created_at=token.created_at,
                last_used_at=token.last_used_at,
                last_used_ip=token.last_used_ip,
                expires_at=token.expires_at,
                status=status,
                reason=reason,
                revocable=status in (TokenStatus.ACTIVE, TokenStatus.STOPPED),
            )
        )
    return rows


async def get_manageable_token(db: AsyncSession, viewer: User, token_id: int) -> ApiToken | None:
    """The token if the viewer may manage it (their own, or any for a superadmin), else None.

    Another admin's token answers like a missing one, so its existence is not revealed.
    """
    token = await db.get(ApiToken, token_id)
    if token is None:
        return None
    if token.owner_id == viewer.id or viewer.group is UserGroup.SUPERADMIN:
        return token
    return None


def _revoke_row(token: ApiToken, now: datetime) -> None:
    """Mark the token revoked, which takes effect on its next use."""
    token.revoked_at = now


async def _audit_revoke(
    db: AsyncSession, actor: AuditActor, token: ApiToken, owner: User | None, reason: str
) -> None:
    """Add the audit entry of one revoked token."""
    await record_actor_audit(
        db,
        actor,
        action="token.revoke",
        result=AuditResult.SUCCESS,
        target_type="token",
        target_id=token.id,
        target_name=token.name,
        details={"owner": owner.name if owner else None, "reason": reason},
    )


async def revoke_token(
    db: AsyncSession, token: ApiToken, actor: AuditActor, *, reason: str = REASON_REQUESTED
) -> bool:
    """Revoke a token and audit it; returns False when it was already revoked.

    Scopes are loaded for every call, so a revoked token is refused on its next use.
    """
    if token.revoked_at is not None:
        return False
    _revoke_row(token, utc_now())
    await _audit_revoke(db, actor, token, await db.get(User, token.owner_id), reason)
    await db.commit()
    return True


async def revoke_user_tokens(
    db: AsyncSession, user: User, actor: AuditActor, *, reason: str
) -> int:
    """Revoke every token of a user that is not revoked yet, one audit entry per token.

    The caller commits with the change that made it necessary (disabling the user or moving
    them to viewer). Returns how many tokens were revoked.
    """
    live = (
        (
            await db.execute(
                select(ApiToken)
                .where(ApiToken.owner_id == user.id, ApiToken.revoked_at.is_(None))
                .order_by(ApiToken.id)
            )
        )
        .scalars()
        .all()
    )
    now = utc_now()
    for token in live:
        _revoke_row(token, now)
        await _audit_revoke(db, actor, token, user, reason)
    return len(live)
