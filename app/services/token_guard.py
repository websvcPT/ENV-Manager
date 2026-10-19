"""The token restriction guard shared by every delete of a structure item.

An API token may be restricted to one project, deployment, service or environment. Dropping that
restriction along with the item would widen the token to everything its owner can reach, so a
delete is refused while a usable token is restricted to the item or anything inside it, and the
refusal is audited. A token that is revoked or expired can never be used again, so its restriction
rows are removed with the item instead of keeping the item from being deleted.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditResult,
    Deployment,
    Environment,
    Service,
)
from app.services.audit import AuditActor, record_actor_audit
from app.services.tokens import is_live


def _subtree(
    project_id: int | None,
    deployment_id: int | None,
    service_id: int | None,
    environment_id: int | None,
) -> tuple[Select[int], Select[int], Select[int]]:
    """The selects of the deployment, service and environment ids of one subtree.

    Exactly one of the arguments is given; the select of a level above it is empty.
    """
    if environment_id is not None:
        return (
            select(Deployment.id).where(Deployment.id.is_(None)),
            select(Service.id).where(Service.id.is_(None)),
            select(Environment.id).where(Environment.id == environment_id),
        )
    if project_id is not None:
        deployments = select(Deployment.id).where(Deployment.project_id == project_id)
    elif deployment_id is not None:
        deployments = select(Deployment.id).where(Deployment.id == deployment_id)
    else:
        deployments = select(Deployment.id).where(Deployment.id.is_(None))
    if service_id is not None:
        services = select(Service.id).where(Service.id == service_id)
    else:
        services = select(Service.id).where(Service.deployment_id.in_(deployments))
    environments = select(Environment.id).where(Environment.service_id.in_(services))
    return deployments, services, environments


async def restrictions_below(
    db: AsyncSession,
    *,
    project_id: int | None = None,
    deployment_id: int | None = None,
    service_id: int | None = None,
    environment_id: int | None = None,
) -> list[tuple[ApiToken, ApiTokenRestriction]]:
    """The tokens and restriction rows that point at an item or anything below it.

    Name the item by exactly one of ``project_id``, ``deployment_id``, ``service_id`` or
    ``environment_id``.
    """
    named = (project_id, deployment_id, service_id, environment_id)
    if sum(value is not None for value in named) != 1:
        raise ValueError("name exactly one item")
    deployments, services, environments = _subtree(
        project_id, deployment_id, service_id, environment_id
    )
    condition = (
        ApiTokenRestriction.deployment_id.in_(deployments)
        | ApiTokenRestriction.service_id.in_(services)
        | ApiTokenRestriction.environment_id.in_(environments)
    )
    if project_id is not None:
        condition = (ApiTokenRestriction.project_id == project_id) | condition
    stmt = (
        select(ApiToken, ApiTokenRestriction)
        .join(ApiTokenRestriction, ApiTokenRestriction.token_id == ApiToken.id)
        .where(condition)
        .order_by(ApiToken.name, ApiToken.id)
    )
    return [(token, restriction) for token, restriction in (await db.execute(stmt)).all()]


async def guard_delete(
    db: AsyncSession,
    who: AuditActor,
    restricting: list[tuple[ApiToken, ApiTokenRestriction]],
    *,
    action: str,
    target_type: str,
    target_id: int,
    target_name: str,
    project_id: int,
    deployment_id: int | None = None,
    service_id: int | None = None,
    environment_id: int | None = None,
) -> list[str]:
    """Check the restrictions below an item that is about to be deleted.

    Returns the names of the usable tokens that block the delete, after writing a blocked audit
    entry and committing it; the caller raises its own refusal. With no usable token the
    restriction rows of dead tokens are deleted (flushed, not committed) and the list is empty.
    The ids are the scope of the audit entry: the item and the ones above it.
    """
    tokens = sorted({token.name for token, _ in restricting if is_live(token)})
    if tokens:
        await record_actor_audit(
            db,
            who,
            action=action,
            result=AuditResult.BLOCKED,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            project_id=project_id,
            deployment_id=deployment_id,
            service_id=service_id,
            environment_id=environment_id,
            details={"reason": "restricted_tokens", "tokens": tokens},
        )
        await db.commit()
        return tokens
    for _, restriction in restricting:
        await db.delete(restriction)
    await db.flush()
    return []
