"""User account lifecycle: creation with initial grants, listing, group changes, password and
two-factor resets and enable or disable.

Every web and command line action on an account goes through here, so the same rules and audit
entries apply to both: unique names and emails, the last active superadmin guard, sessions ended
on disable.
"""

from collections.abc import Collection

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import generate_password, hash_password
from app.models import (
    AuditActorType,
    AuditResult,
    LoginCounter,
    LoginCounterKind,
    User,
    UserGroup,
)
from app.services import totp
from app.services.audit import AuditActor, record_audit
from app.services.grants import (
    GrantTarget,
    add_grant_row,
    record_grant_change,
    require_existing,
)
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    UserChange,
    UserChanged,
    announce_event,
)
from app.services.pending_logins import discard_user_pending_logins
from app.services.sessions import revoke_user_sessions
from app.services.tokens import REASON_MOVED_TO_VIEWER, REASON_USER_DISABLED, revoke_user_tokens

MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 320


class UserError(Exception):
    """Base class for refused user operations. Messages never contain passwords.

    ``field`` names the form field a message belongs to, when there is one.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class InvalidUserError(UserError):
    """The given name or email is not acceptable."""


class UserExistsError(UserError):
    """A user with that name or email address already exists."""


class UserNotFoundError(UserError):
    """No user has that name."""


class LastSuperadminError(UserError):
    """The operation would leave the system without an active superadmin."""


class TwoFactorNotOnError(UserError):
    """The user has no two-factor sign-in to reset."""


def _actor_name(actor_type: AuditActorType) -> str | None:
    """The audit actor name for non-user actors."""
    return "cli" if actor_type is AuditActorType.CLI else None


async def find_user_by_name(db: AsyncSession, name: str) -> User | None:
    """The user whose name matches, ignoring case."""
    return (
        await db.execute(select(User).where(func.lower(User.name) == name.strip().lower()))
    ).scalar_one_or_none()


async def find_user_by_email(db: AsyncSession, email: str) -> User | None:
    """The user whose email matches, ignoring case the way the unique index does."""
    return (
        await db.execute(select(User).where(func.lower(User.email) == func.lower(email.strip())))
    ).scalar_one_or_none()


async def get_user_by_name(db: AsyncSession, name: str) -> User:
    """The user whose name matches, ignoring case, or ``UserNotFoundError``."""
    user = await find_user_by_name(db, name)
    if user is None:
        raise UserNotFoundError(f"no user named {name.strip()!r}")
    return user


async def list_users(db: AsyncSession) -> list[User]:
    """Every user ordered by name."""
    return list((await db.execute(select(User).order_by(func.lower(User.name)))).scalars())


def validate_identity(name: str, email: str) -> tuple[str, str]:
    """The trimmed name and email when both are acceptable, else ``InvalidUserError``."""
    name = name.strip()
    email = email.strip()
    if not name:
        raise InvalidUserError("Enter a name.", "name")
    if len(name) > MAX_NAME_LENGTH or any(char.isspace() for char in name):
        raise InvalidUserError("Use 1 to 100 characters without spaces.", "name")
    if "@" in name:
        raise InvalidUserError("The name cannot contain @, it is used for email sign-in.", "name")
    local, _, domain = email.partition("@")
    if not email:
        raise InvalidUserError("Enter an email address.", "email")
    if not local or "." not in domain or len(email) > MAX_EMAIL_LENGTH or " " in email:
        raise InvalidUserError("Enter a valid email address.", "email")
    return name, email


async def create_user(
    db: AsyncSession,
    *,
    name: str,
    email: str,
    group: UserGroup,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
    grants: Collection[GrantTarget] = (),
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> tuple[User, str]:
    """Create an active user with a generated password and initial grants, and audit it all.

    Superadmins reach everything, so grants given for one are not stored. Every chosen item is
    checked before anything is written, and the user, grants and audit entries share one commit.
    Returns the user and the plaintext password, which exists only in the return value.
    """
    name, email = validate_identity(name, email)
    if await find_user_by_name(db, name) is not None:
        raise UserExistsError(f"A user named {name} already exists.", "name")
    if await find_user_by_email(db, email) is not None:
        raise UserExistsError("Another user already has this email address.", "email")
    chosen = [] if group is UserGroup.SUPERADMIN else sorted(set(grants))
    labels = await require_existing(db, chosen)
    actor = AuditActor(actor_type, user=actor_user, ip_address=ip_address)
    password = generate_password()
    user = User(name=name, email=email, password_hash=hash_password(password), group=group)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise UserExistsError(
            "A user with this name or email address already exists.", "email"
        ) from error
    await record_audit(
        db,
        action="user.create",
        result=AuditResult.SUCCESS,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=_actor_name(actor_type),
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"group": group.value, "grants": len(chosen)},
    )
    for target in chosen:
        add_grant_row(db, user, target)
        await record_grant_change(db, actor, user, "grant.add", target, labels[target])
    await db.commit()
    await announce_event(
        notifier, UserChanged(UserChange.CREATED, user.name, actor.label, group.value)
    )
    return user, password


async def reset_password(
    db: AsyncSession,
    user: User,
    *,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> str:
    """Give the user a new generated password, end their sessions and clear their lockout.

    Returns the plaintext password, which exists only in the return value.
    """
    password = generate_password()
    user.password_hash = hash_password(password)
    ended = await revoke_user_sessions(db, user.id)
    await db.execute(
        delete(LoginCounter).where(
            LoginCounter.kind == LoginCounterKind.USER,
            LoginCounter.subject.in_([user.name.lower(), user.email.lower()]),
        )
    )
    await record_audit(
        db,
        action="user.password_reset",
        result=AuditResult.SUCCESS,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=_actor_name(actor_type),
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"sessions_ended": ended},
    )
    await db.commit()
    await announce_event(
        notifier,
        UserChanged(
            UserChange.PASSWORD_RESET, user.name, AuditActor(actor_type, user=actor_user).label
        ),
    )
    return password


async def reset_two_factor(
    db: AsyncSession,
    user: User,
    *,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> int:
    """Turn off a user's two-factor sign-in without their code, for a lost authenticator.

    The secret is discarded, the user's sessions end and so do their sign-ins that were waiting
    for a code, all in one commit with one audit entry (counts only). They sign in with the
    password alone until they turn it on again. Returns the number of sessions that were ended.
    """
    if not user.totp_enabled:
        raise TwoFactorNotOnError(f"{user.name} has no two-factor sign-in to reset.")
    totp.disable(user)
    ended = await revoke_user_sessions(db, user.id)
    waiting = await discard_user_pending_logins(db, user.id)
    await record_audit(
        db,
        action="user.totp_reset",
        result=AuditResult.SUCCESS,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=_actor_name(actor_type),
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"sessions_ended": ended, "pending_logins_ended": waiting},
    )
    await db.commit()
    await announce_event(
        notifier,
        UserChanged(
            UserChange.TWO_FACTOR_RESET, user.name, AuditActor(actor_type, user=actor_user).label
        ),
    )
    return ended


async def _other_active_superadmins(db: AsyncSession, user: User) -> int:
    """How many active superadmins exist besides the given user."""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(
                    User.group == UserGroup.SUPERADMIN,
                    User.is_active.is_(True),
                    User.id != user.id,
                )
            )
        ).scalar_one()
    )


async def is_last_active_superadmin(db: AsyncSession, user: User) -> bool:
    """Whether the user is the only active superadmin, who can be neither disabled nor demoted."""
    return (
        user.is_active
        and user.group is UserGroup.SUPERADMIN
        and await _other_active_superadmins(db, user) == 0
    )


async def _record_blocked(
    db: AsyncSession,
    user: User,
    action: str,
    details: dict[str, object],
    *,
    actor_type: AuditActorType,
    actor_user: User | None,
    ip_address: str | None,
) -> None:
    """Audit and commit a refused account change."""
    await record_audit(
        db,
        action=action,
        result=AuditResult.BLOCKED,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=_actor_name(actor_type),
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details=details,
    )
    await db.commit()


async def set_user_active(
    db: AsyncSession,
    user: User,
    active: bool,
    *,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
) -> int:
    """Enable or disable a user, audit it, and end the sessions of a disabled user.

    Disabling the last active superadmin is refused and the refusal is audited. Disabling a user
    also revokes all of their API tokens for good, each with its own audit entry, so enabling the
    account later never brings an old token back. Returns the number of sessions that were ended.
    """
    action = "user.enable" if active else "user.disable"
    if not active and await is_last_active_superadmin(db, user):
        await _record_blocked(
            db,
            user,
            action,
            {"reason": "last_active_superadmin"},
            actor_type=actor_type,
            actor_user=actor_user,
            ip_address=ip_address,
        )
        raise LastSuperadminError("The last active superadmin cannot be disabled.")
    user.is_active = active
    ended = 0
    details: dict[str, object] = {}
    if not active:
        ended = await revoke_user_sessions(db, user.id)
        actor = AuditActor(actor_type, user=actor_user, ip_address=ip_address)
        revoked = await revoke_user_tokens(db, user, actor, reason=REASON_USER_DISABLED)
        details["tokens_revoked"] = revoked
    await record_audit(
        db,
        action=action,
        result=AuditResult.SUCCESS,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=_actor_name(actor_type),
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"sessions_ended": ended, **details},
    )
    await db.commit()
    if not active:
        await announce_event(
            notifier,
            UserChanged(
                UserChange.DISABLED, user.name, AuditActor(actor_type, user=actor_user).label
            ),
        )
    return ended


async def set_user_group(
    db: AsyncSession,
    user: User,
    group: UserGroup,
    *,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
) -> bool:
    """Move a user to another group and audit it; returns whether the group changed.

    Demoting the last active superadmin is refused and the refusal is audited. The change
    applies to the user's next request, because scopes are loaded per request. Moving a user to
    viewer revokes all of their API tokens for good, each with its own audit entry, so promoting
    them again never brings an old token back. Grants are kept, but superadmins ignore them.
    """
    previous = user.group
    if group is previous:
        return False
    if await is_last_active_superadmin(db, user):
        await _record_blocked(
            db,
            user,
            "user.group_change",
            {"reason": "last_active_superadmin", "from": previous.value, "to": group.value},
            actor_type=actor_type,
            actor_user=actor_user,
            ip_address=ip_address,
        )
        raise LastSuperadminError("The last active superadmin cannot be demoted.")
    user.group = group
    details: dict[str, object] = {"from": previous.value, "to": group.value}
    if group is UserGroup.VIEWER:
        actor = AuditActor(actor_type, user=actor_user, ip_address=ip_address)
        details["tokens_revoked"] = await revoke_user_tokens(
            db, user, actor, reason=REASON_MOVED_TO_VIEWER
        )
    await record_audit(
        db,
        action="user.group_change",
        result=AuditResult.SUCCESS,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=_actor_name(actor_type),
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details=details,
    )
    await db.commit()
    return True
