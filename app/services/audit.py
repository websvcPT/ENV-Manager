"""Writing audit entries. Details must never contain secret values."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telemetry import current_trace_id
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
    def for_sign_in(cls, user: User | None, ip_address: str | None) -> AuditActor:
        """Someone signing in: the account they named, or no account when it does not exist."""
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

    @property
    def entry_name(self) -> str | None:
        """The actor name an audit entry stores: the label, none for an account that is unknown."""
        if self.type is AuditActorType.USER and self.user is None:
            return None
        return self.label


class AuditedEnvironment(Protocol):
    """An environment with the ids of everything above it, which each entry about a file carries."""

    @property
    def id(self) -> int:
        """The environment id."""
        ...

    @property
    def name(self) -> str:
        """The file name."""
        ...

    @property
    def project_id(self) -> int:
        """The project the file belongs to."""
        ...

    @property
    def deployment_id(self) -> int:
        """The deployment the file belongs to."""
        ...

    @property
    def service_id(self) -> int:
        """The service the file belongs to."""
        ...


@dataclass(frozen=True)
class EnvironmentRef:
    """An environment named for an audit entry, where no editor header is at hand."""

    id: int
    name: str
    project_id: int
    deployment_id: int
    service_id: int


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
    version_ids: Sequence[int] = (),
    created_at: datetime | None = None,
) -> AuditEntry:
    """Add an audit entry to the session. The caller commits it with its own change.

    The entry keeps the id of the trace being recorded when it is written, so it can be matched to
    its trace; it has none while nothing is recording. ``actor_name`` is the name stored for the
    actor, the actor user's name when it is not given. ``version_ids`` and ``created_at`` are
    written with the entry: the table is append-only, so nothing may be set after the insert.
    """
    if actor_name is None and actor_user is not None:
        actor_name = actor_user.name
    entry = AuditEntry(
        action=action,
        result=result,
        actor_type=actor_type,
        actor_user_id=actor_user.id if actor_user else None,
        actor_token_id=actor_token.id if actor_token else None,
        actor_name=actor_name,
        ip_address=ip_address,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        project_id=project_id,
        deployment_id=deployment_id,
        service_id=service_id,
        environment_id=environment_id,
        details=details or {},
        version_ids=list(version_ids),
        trace_id=current_trace_id(),
    )
    if created_at is not None:
        entry.created_at = created_at
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
    version_ids: Sequence[int] = (),
    created_at: datetime | None = None,
) -> AuditEntry:
    """Add an audit entry for an actor that may be a user, an API token or the CLI.

    A token entry is named after the token (``actor_name``) and keeps the owner as the user id.
    The caller commits.
    """
    return await record_audit(
        db,
        action=action,
        result=result,
        actor_type=actor.type,
        actor_user=actor.user,
        actor_token=actor.token,
        actor_name=actor.entry_name,
        ip_address=actor.ip_address,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        project_id=project_id,
        deployment_id=deployment_id,
        service_id=service_id,
        environment_id=environment_id,
        details=details,
        version_ids=version_ids,
        created_at=created_at,
    )


async def record_environment_audit(
    db: AsyncSession,
    actor: AuditActor,
    environment: AuditedEnvironment,
    *,
    action: str,
    details: dict[str, object],
    result: AuditResult = AuditResult.SUCCESS,
    version_ids: Sequence[int] = (),
    created_at: datetime | None = None,
) -> AuditEntry:
    """Add the audit entry of an action on a file, with the file and its ancestors as the scope.

    ``version_ids`` are the stored versions the action involved. The caller commits.
    """
    return await record_actor_audit(
        db,
        actor,
        action=action,
        result=result,
        target_type="environment",
        target_id=environment.id,
        target_name=environment.name,
        project_id=environment.project_id,
        deployment_id=environment.deployment_id,
        service_id=environment.service_id,
        environment_id=environment.id,
        details=details,
        version_ids=version_ids,
        created_at=created_at,
    )


async def record_committed_audit(
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
    details: dict[str, object] | None = None,
) -> None:
    """Add an audit entry for an action that changes nothing else, and commit it.

    The writer of the attempts that leave only their entry behind: a connection test, a refused
    restore, a refused API call.
    """
    await record_actor_audit(
        db,
        actor,
        action=action,
        result=result,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        project_id=project_id,
        deployment_id=deployment_id,
        service_id=service_id,
        details=details,
    )
    await db.commit()
