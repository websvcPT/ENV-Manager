"""Writing audit entries. Details must never contain secret values."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditActorType, AuditEntry, AuditResult, User


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    result: AuditResult,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    actor_name: str | None = None,
    ip_address: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    target_name: str | None = None,
    project_id: int | None = None,
    deployment_id: int | None = None,
    service_id: int | None = None,
    environment_id: int | None = None,
    details: dict[str, object] | None = None,
) -> AuditEntry:
    """Add an audit entry to the session. The caller commits it with its own change."""
    entry = AuditEntry(
        action=action,
        result=result,
        actor_type=actor_type,
        actor_user_id=actor_user.id if actor_user else None,
        actor_name=actor_user.name if actor_user else actor_name,
        ip_address=ip_address,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        project_id=project_id,
        deployment_id=deployment_id,
        service_id=service_id,
        environment_id=environment_id,
        details=details or {},
    )
    db.add(entry)
    await db.flush()
    return entry
