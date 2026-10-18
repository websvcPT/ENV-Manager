"""Writing audit entries. Details must never contain secret values."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, AuditActorType, AuditEntry, AuditResult, User

CLI_ACTOR_NAME = "cli"


@dataclass(frozen=True)
class AuditActor:
    """Who performed an action: a signed-in user, an API token acting for its owner, or the CLI.

    ``user`` is the signed-in user, or the owner of ``token``. ``label`` is the name shown in
    history lists and copied into audit entries.
    """

    type: AuditActorType
    user: User | None = None
    token: ApiToken | None = None
    ip_address: str | None = None
    system_name: str | None = None

    @classmethod
    def for_user(cls, user: User, ip_address: str | None = None) -> AuditActor:
        """A signed-in user."""
        return cls(AuditActorType.USER, user=user, ip_address=ip_address)

    @classmethod
    def for_token(cls, token: ApiToken, owner: User, ip_address: str | None = None) -> AuditActor:
        """An API token and its owner."""
        return cls(AuditActorType.API, user=owner, token=token, ip_address=ip_address)

    @classmethod
    def cli(cls) -> AuditActor:
        """The command line tool, which acts without a user."""
        return cls(AuditActorType.CLI)

    @classmethod
    def system(cls, name: str) -> AuditActor:
        """The application acting on its own, for example the scheduler, under a fixed name."""
        return cls(AuditActorType.SYSTEM, system_name=name)

    @property
    def label(self) -> str:
        """The actor's name for audit entries and history."""
        if self.type is AuditActorType.CLI:
            return CLI_ACTOR_NAME
        if self.type is AuditActorType.SYSTEM and self.system_name:
            return self.system_name
        if self.token is not None:
            return f"token {self.token.name}"
        return self.user.name if self.user else "unknown"


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    result: AuditResult,
    actor_type: AuditActorType,
    actor_user: User | None = None,
    actor_token: ApiToken | None = None,
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
        actor_token_id=actor_token.id if actor_token else None,
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


async def record_actor_audit(
    db: AsyncSession,
    actor: AuditActor,
    *,
    action: str,
    result: AuditResult,
    target_type: str | None = None,
    target_id: int | None = None,
    target_name: str | None = None,
    project_id: int | None = None,
    deployment_id: int | None = None,
    service_id: int | None = None,
    environment_id: int | None = None,
    details: dict[str, object] | None = None,
) -> AuditEntry:
    """Add an audit entry for an actor that may be a user, an API token or the CLI.

    A token entry is named after the token (``actor_name``) and keeps the owner as the user id.
    The caller commits.
    """
    entry = await record_audit(
        db,
        action=action,
        result=result,
        actor_type=actor.type,
        actor_user=actor.user,
        actor_token=actor.token,
        actor_name=actor.label,
        ip_address=actor.ip_address,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        project_id=project_id,
        deployment_id=deployment_id,
        service_id=service_id,
        environment_id=environment_id,
        details=details,
    )
    entry.actor_name = actor.label
    return entry
