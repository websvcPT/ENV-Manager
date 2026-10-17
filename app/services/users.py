"""User account state changes."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditActorType, AuditResult, User
from app.services.audit import record_audit
from app.services.sessions import revoke_user_sessions


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

    Returns the number of sessions that were ended.
    """
    user.is_active = active
    ended = await revoke_user_sessions(db, user.id) if not active else 0
    await record_audit(
        db,
        action="user.enable" if active else "user.disable",
        result=AuditResult.SUCCESS,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name="cli" if actor_type is AuditActorType.CLI else None,
        ip_address=ip_address,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"sessions_ended": ended},
    )
    await db.commit()
    return ended
