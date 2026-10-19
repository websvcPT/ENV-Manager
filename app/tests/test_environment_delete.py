"""Environment delete: the cascade, the audit and the token restriction guard."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditResult,
    DeployResult,
    Environment,
    EnvironmentVersion,
)
from app.seeds import SeedData
from app.services.audit import AuditActor, EnvironmentRef
from app.services.environment_delete import EnvironmentInUseError, delete_environment
from app.services.structure_lookup import environment_chain
from app.services.tokens import revoke_token
from app.tests.audit_helpers import audit_entries


async def reference(db: AsyncSession, environment: Environment) -> EnvironmentRef:
    """The audit reference of a stored environment."""
    chain = await environment_chain(db, environment.id)
    assert chain is not None
    return EnvironmentRef(
        environment.id,
        environment.name,
        chain.deployment.project_id,
        chain.deployment.id,
        chain.service.id,
    )


async def test_delete_removes_the_file_and_its_versions_and_keeps_history(
    db: AsyncSession, seed_data: SeedData
) -> None:
    environment = seed_data.envs.staging_web
    environment_id = environment.id
    ref = await reference(db, environment)
    versions = (
        await db.execute(
            select(func.count())
            .select_from(EnvironmentVersion)
            .where(EnvironmentVersion.environment_id == environment_id)
        )
    ).scalar_one()
    results = (
        await db.execute(
            select(func.count())
            .select_from(DeployResult)
            .where(DeployResult.environment_id == environment_id)
        )
    ).scalar_one()
    await delete_environment(db, environment, ref, actor=seed_data.users.superadmin)
    assert await db.get(Environment, environment_id) is None
    assert (
        await db.execute(
            select(EnvironmentVersion.id).where(EnvironmentVersion.environment_id == environment_id)
        )
    ).first() is None
    kept = (
        await db.execute(
            select(func.count())
            .select_from(DeployResult)
            .where(DeployResult.environment_name == ref.name)
        )
    ).scalar_one()
    assert kept >= results
    (entry,) = await audit_entries(db, "environment.delete")
    assert entry.result is AuditResult.SUCCESS
    assert (entry.project_id, entry.deployment_id, entry.service_id) == (
        ref.project_id,
        ref.deployment_id,
        ref.service_id,
    )
    assert entry.details == {"versions": versions, "target_path": environment.target_path}


async def test_delete_is_refused_while_a_live_token_is_restricted_to_the_file(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """billing-ci is restricted to the api file: it stays, and the refusal is audited."""
    environment = seed_data.envs.api
    ref = await reference(db, environment)
    with pytest.raises(EnvironmentInUseError, match="billing-ci"):
        await delete_environment(db, environment, ref, actor=seed_data.users.superadmin)
    assert await db.get(Environment, environment.id) is not None
    (entry,) = await audit_entries(db, "environment.delete")
    assert entry.result is AuditResult.BLOCKED
    assert entry.details == {"reason": "restricted_tokens", "tokens": ["billing-ci"]}
    assert entry.environment_id == environment.id


async def test_a_revoked_token_no_longer_holds_its_file(
    db: AsyncSession, seed_data: SeedData
) -> None:
    token = await db.get(ApiToken, seed_data.tokens[0].id)
    assert token is not None
    await revoke_token(db, token, AuditActor.for_user(seed_data.users.admin))
    environment = seed_data.envs.api
    await delete_environment(
        db, environment, await reference(db, environment), actor=seed_data.users.superadmin
    )
    assert (await db.execute(select(ApiTokenRestriction))).first() is None
    assert (await audit_entries(db, "environment.delete"))[0].result is AuditResult.SUCCESS
