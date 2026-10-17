"""User account lifecycle: creation, listing, password resets and enable or disable."""

from sqlalchemy import delete, func, select
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
from app.services.audit import record_audit
from app.services.sessions import revoke_user_sessions

MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 320


class UserError(Exception):
    """Base class for refused user operations. Messages never contain passwords."""


class InvalidUserError(UserError):
    """The given name or email is not acceptable."""


class UserExistsError(UserError):
    """A user with that name already exists."""


class UserNotFoundError(UserError):
    """No user has that name."""


class LastSuperadminError(UserError):
    """The operation would leave the system without an active superadmin."""


def _actor_name(actor_type: AuditActorType) -> str | None:
    """The audit actor name for non-user actors."""
    return "cli" if actor_type is AuditActorType.CLI else None


async def find_user_by_name(db: AsyncSession, name: str) -> User | None:
    """The user whose name matches, ignoring case."""
    return (
        await db.execute(select(User).where(func.lower(User.name) == name.strip().lower()))
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


async def create_user(
    db: AsyncSession,
    *,
    name: str,
    email: str,
    group: UserGroup,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
) -> tuple[User, str]:
    """Create an active user with a generated password and audit it.

    Returns the user and the plaintext password, which exists only in the return value.
    """
    name = name.strip()
    email = email.strip()
    if not name or len(name) > MAX_NAME_LENGTH or any(char.isspace() for char in name):
        raise InvalidUserError("name must be 1 to 100 characters without whitespace")
    if "@" in name:
        raise InvalidUserError("name must not contain '@', it is reserved for email sign-in")
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > MAX_EMAIL_LENGTH or " " in email:
        raise InvalidUserError("email address is not valid")
    if await find_user_by_name(db, name) is not None:
        raise UserExistsError(f"a user named {name!r} already exists")
    password = generate_password()
    user = User(name=name, email=email, password_hash=hash_password(password), group=group)
    db.add(user)
    await db.flush()
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
        details={"group": group.value},
    )
    await db.commit()
    return user, password


async def reset_password(
    db: AsyncSession,
    user: User,
    *,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
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
    return password


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


async def set_user_active(
    db: AsyncSession,
    user: User,
    active: bool,
    *,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    ip_address: str | None = None,
) -> int:
    """Enable or disable a user, audit it, and end the sessions of a disabled user.

    Disabling the last active superadmin is refused and the refusal is audited.
    Returns the number of sessions that were ended.
    """
    action = "user.enable" if active else "user.disable"
    if (
        not active
        and user.is_active
        and user.group is UserGroup.SUPERADMIN
        and await _other_active_superadmins(db, user) == 0
    ):
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
            details={"reason": "last_active_superadmin"},
        )
        await db.commit()
        raise LastSuperadminError("the last active superadmin cannot be disabled")
    user.is_active = active
    ended = await revoke_user_sessions(db, user.id) if not active else 0
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
        details={"sessions_ended": ended},
    )
    await db.commit()
    return ended
