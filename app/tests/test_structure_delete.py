"""Service rename and delete and deployment delete: the rules, the audit and the token guard."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditResult,
    Deployment,
    DeployRun,
    Environment,
    EnvironmentVersion,
    Service,
)
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.deployments import DeploymentInUseError, delete_deployment
from app.services.permissions import load_user_scope
from app.services.services import (
    ServiceInUseError,
    ServiceNameError,
    create_service,
    delete_service,
    rename_service,
)
from app.services.tokens import create_token, revoke_token
from app.tests.audit_helpers import audit_entries


async def count(db: AsyncSession, model: type) -> int:
    """How many rows a table holds."""
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def test_rename_service_changes_the_name_and_audits_the_old_one(
    db: AsyncSession, seed_data: SeedData
) -> None:
    service = seed_data.services.web
    await rename_service(db, service, "  frontend ", actor=seed_data.users.superadmin)
    assert service.name == "frontend"
    (entry,) = await audit_entries(db, "service.rename")
    assert entry.result is AuditResult.SUCCESS
    assert entry.target_name == "frontend" and entry.details == {"previous_name": "web"}
    deployment = seed_data.deployments.fancy_prod
    assert (entry.project_id, entry.deployment_id, entry.service_id) == (
        deployment.project_id,
        deployment.id,
        service.id,
    )


async def test_renaming_to_the_same_name_changes_and_audits_nothing(
    db: AsyncSession, seed_data: SeedData
) -> None:
    await rename_service(db, seed_data.services.web, "web", actor=seed_data.users.superadmin)
    assert await audit_entries(db, "service.rename") == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "Enter a service name."),
        ("Bad Name", "Use 1 to"),
        ("mysql", "A service named mysql already exists in this deployment."),
    ],
)
async def test_a_refused_new_name_changes_nothing(
    db: AsyncSession, seed_data: SeedData, raw: str, message: str
) -> None:
    with pytest.raises(ServiceNameError, match=message):
        await rename_service(db, seed_data.services.web, raw, actor=seed_data.users.superadmin)
    stored = await db.get(Service, seed_data.services.web.id)
    assert stored is not None and stored.name == "web"
    assert await audit_entries(db, "service.rename") == []


async def test_a_name_used_in_another_deployment_is_free(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Names are unique inside one deployment only: staging already has a web service."""
    await rename_service(db, seed_data.services.mysql, "web2", actor=seed_data.users.superadmin)
    await rename_service(db, seed_data.services.api, "web", actor=seed_data.users.superadmin)
    assert seed_data.services.api.name == "web"


async def test_delete_service_removes_its_files_and_versions_and_keeps_history(
    db: AsyncSession, seed_data: SeedData
) -> None:
    service = seed_data.services.mysql
    service_id = service.id
    runs = await count(db, DeployRun)
    environments_before = await count(db, Environment)
    versions_before = await count(db, EnvironmentVersion)
    own = (
        (await db.execute(select(Environment.id).where(Environment.service_id == service_id)))
        .scalars()
        .all()
    )
    assert own
    await delete_service(db, service, actor=seed_data.users.superadmin)
    assert await db.get(Service, service_id) is None
    assert await count(db, Environment) == environments_before - len(own)
    assert await count(db, EnvironmentVersion) < versions_before
    assert await count(db, DeployRun) == runs
    kept = (
        (await db.execute(select(DeployRun).where(DeployRun.service_name == "mysql")))
        .scalars()
        .all()
    )
    assert kept and all(run.service_id is None for run in kept)
    (entry,) = await audit_entries(db, "service.delete")
    assert (entry.result, entry.service_id, entry.target_name) == (
        AuditResult.SUCCESS,
        service_id,
        "mysql",
    )
    assert entry.details == {"environments": len(own)}


async def test_a_deleted_service_id_is_never_handed_out_again(
    db: AsyncSession, seed_data: SeedData
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.billing_legacy.id)
    assert deployment is not None
    created = await create_service(db, deployment, "temp", actor=seed_data.users.superadmin)
    await delete_service(db, created, actor=seed_data.users.superadmin)
    again = await create_service(db, deployment, "temp", actor=seed_data.users.superadmin)
    assert again.id > created.id


async def test_delete_service_is_refused_while_a_live_token_is_restricted_inside(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """billing-ci is restricted to a file of the api service: the service stays, audited."""
    service = seed_data.services.api
    with pytest.raises(ServiceInUseError, match="billing-ci"):
        await delete_service(db, service, actor=seed_data.users.superadmin)
    assert await db.get(Service, service.id) is not None
    (entry,) = await audit_entries(db, "service.delete")
    assert entry.result is AuditResult.BLOCKED
    assert entry.details == {"reason": "restricted_tokens", "tokens": ["billing-ci"]}
    assert entry.service_id == service.id


async def test_a_revoked_token_no_longer_holds_its_service(
    db: AsyncSession, seed_data: SeedData
) -> None:
    token = await db.get(ApiToken, seed_data.tokens[0].id)
    assert token is not None
    await revoke_token(db, token, AuditActor.for_user(seed_data.users.admin))
    await delete_service(db, seed_data.services.api, actor=seed_data.users.superadmin)
    assert (await db.execute(select(ApiTokenRestriction))).first() is None
    assert (await audit_entries(db, "service.delete"))[0].result is AuditResult.SUCCESS


async def test_a_token_restricted_to_the_service_itself_blocks_its_delete(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    await create_token(
        db,
        admin,
        await load_user_scope(db, admin),
        name="web-ci",
        operations=["deploy"],
        restriction=f"service:{seed_data.services.web.id}",
        expires="",
        actor=AuditActor.for_user(admin),
    )
    with pytest.raises(ServiceInUseError, match="web-ci"):
        await delete_service(db, seed_data.services.web, actor=seed_data.users.superadmin)
    assert await db.get(Service, seed_data.services.web.id) is not None


async def test_delete_deployment_removes_the_tree_and_keeps_history(
    db: AsyncSession, seed_data: SeedData
) -> None:
    deployment = seed_data.deployments.billing_legacy
    deployment_id = deployment.id
    services = (
        await db.execute(
            select(func.count()).select_from(Service).where(Service.deployment_id == deployment_id)
        )
    ).scalar_one()
    await delete_deployment(db, deployment, actor=seed_data.users.superadmin)
    assert await db.get(Deployment, deployment_id) is None
    assert (
        await db.execute(select(Service.id).where(Service.deployment_id == deployment_id))
    ).first() is None
    (entry,) = await audit_entries(db, "deployment.delete")
    assert (entry.result, entry.deployment_id, entry.project_id) == (
        AuditResult.SUCCESS,
        deployment_id,
        deployment.project_id,
    )
    assert entry.details == {"services": services, "root_path": deployment.root_path}


async def test_delete_deployment_is_refused_while_a_live_token_is_restricted_inside(
    db: AsyncSession, seed_data: SeedData
) -> None:
    deployment = seed_data.deployments.billing_main
    with pytest.raises(DeploymentInUseError, match="billing-ci"):
        await delete_deployment(db, deployment, actor=seed_data.users.superadmin)
    assert await db.get(Deployment, deployment.id) is not None
    (entry,) = await audit_entries(db, "deployment.delete")
    assert entry.result is AuditResult.BLOCKED
    assert entry.details == {"reason": "restricted_tokens", "tokens": ["billing-ci"]}


async def test_a_revoked_token_no_longer_holds_its_deployment(
    db: AsyncSession, seed_data: SeedData
) -> None:
    token = await db.get(ApiToken, seed_data.tokens[0].id)
    assert token is not None
    await revoke_token(db, token, AuditActor.for_user(seed_data.users.admin))
    main = seed_data.deployments.billing_main
    await delete_deployment(db, main, actor=seed_data.users.superadmin)
    assert (await db.execute(select(ApiTokenRestriction))).first() is None
    kept = await db.get(ApiToken, token.id)
    assert kept is not None and kept.revoked_at is not None
