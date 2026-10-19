"""Tests for the audit, deploy run, allowed path and backup tables."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import (
    AllowedPath,
    ApiToken,
    AuditActorType,
    AuditEntry,
    AuditResult,
    Backup,
    BackupStatus,
    BackupTrigger,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    PostDeployStatus,
    Project,
    Service,
    User,
    UserGroup,
)

PLACEHOLDER_HASH = "not-a-real-hash"


async def test_audit_entry_survives_actor_and_scope_deletion(engine: AsyncEngine) -> None:
    """Deleting the user, token and project leaves the entry readable with its copied names."""
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(
            name="alice",
            email="a@example.test",
            password_hash=PLACEHOLDER_HASH,
            group=UserGroup.ADMIN,
        )
        project = Project(name="acme-shop")
        session.add_all([user, project])
        await session.flush()
        token = ApiToken(owner_id=user.id, name="ci", token_hash=PLACEHOLDER_HASH, prefix="emt_")
        session.add(token)
        await session.flush()
        entry = AuditEntry(
            actor_type=AuditActorType.API,
            actor_user_id=user.id,
            actor_token_id=token.id,
            actor_name="alice via ci",
            action="deploy.run",
            result=AuditResult.HALTED,
            target_type="service",
            target_name="mysql",
            project_id=project.id,
            version_ids=[3, 4],
            details={"force": False},
        )
        session.add(entry)
        await session.commit()
        entry_id = entry.id
        project_id = project.id
        await session.delete(token)
        await session.delete(user)
        await session.delete(project)
        await session.commit()

    async with factory() as session:
        stored = (await session.execute(select(AuditEntry))).scalar_one()
    assert stored.id == entry_id
    assert stored.actor_user_id is None
    assert stored.actor_token_id is None
    assert stored.actor_name == "alice via ci"
    assert stored.project_id == project_id
    assert stored.result is AuditResult.HALTED
    assert stored.version_ids == [3, 4]
    assert stored.details == {"force": False}
    assert stored.created_at.tzinfo is not None


async def test_deploy_history_survives_structure_deletion(engine: AsyncEngine) -> None:
    """Deleting the service keeps the run and its results, with names and nulled references."""
    factory = create_session_factory(engine)
    async with factory() as session:
        project = Project(name="acme-shop")
        session.add(project)
        await session.flush()
        deployment = Deployment(project_id=project.id, name="deployment-a", root_path="/opt/a")
        session.add(deployment)
        await session.flush()
        service = Service(deployment_id=deployment.id, name="mysql")
        session.add(service)
        await session.flush()
        environment = Environment(service_id=service.id, name=".env.db", target_path=".env.db")
        session.add(environment)
        await session.flush()
        version = EnvironmentVersion(
            environment_id=environment.id, number=1, content_encrypted="x", content_hash="h"
        )
        session.add(version)
        await session.flush()
        run = DeployRun(
            service_id=service.id,
            service_name="mysql",
            deployment_name="deployment-a",
            trigger=DeployTrigger.UI,
            status=DeployStatus.POST_DEPLOY_FAILED,
            post_deploy_status=PostDeployStatus.FAILED,
            post_deploy_exit_code=1,
            post_deploy_output="boom",
        )
        session.add(run)
        await session.flush()
        session.add(
            DeployResult(
                run_id=run.id,
                environment_id=environment.id,
                environment_name=".env.db",
                version_id=version.id,
                target_path="/opt/a/.env.db",
                status=DeployResultStatus.WRITTEN,
                written_hash="h",
            )
        )
        await session.commit()
        await session.delete(project)
        await session.commit()

    async with factory() as session:
        stored_run = (await session.execute(select(DeployRun))).scalar_one()
        stored_result = (await session.execute(select(DeployResult))).scalar_one()
    assert stored_run.service_id is None
    assert stored_run.service_name == "mysql"
    assert stored_run.dry_run is False
    assert stored_run.force is False
    assert stored_run.status is DeployStatus.POST_DEPLOY_FAILED
    assert stored_result.run_id == stored_run.id
    assert stored_result.environment_id is None
    assert stored_result.version_id is None
    assert stored_result.environment_name == ".env.db"
    assert stored_result.written_hash == "h"


async def test_deleting_a_run_removes_its_results(engine: AsyncEngine) -> None:
    """Results belong to their run."""
    factory = create_session_factory(engine)
    async with factory() as session:
        run = DeployRun(
            service_name="mysql",
            deployment_name="deployment-a",
            trigger=DeployTrigger.API,
            status=DeployStatus.SUCCESS,
        )
        session.add(run)
        await session.flush()
        session.add(
            DeployResult(
                run_id=run.id,
                environment_name=".env.db",
                target_path="/opt/a/.env.db",
                status=DeployResultStatus.UNCHANGED,
            )
        )
        await session.commit()
        await session.delete(run)
        await session.commit()
        assert (await session.execute(select(DeployResult))).first() is None


async def test_allowed_paths_are_unique(engine: AsyncEngine) -> None:
    """The same path cannot be listed twice."""
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(AllowedPath(path="/opt/xxx1"))
        await session.commit()
        session.add(AllowedPath(path="/opt/xxx1"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_backup_record_round_trip(engine: AsyncEngine) -> None:
    """A failed backup keeps its error and a successful one its size."""
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all(
            [
                Backup(
                    filename="envmanager-1.db",
                    trigger=BackupTrigger.SCHEDULED,
                    status=BackupStatus.SUCCESS,
                    size_bytes=4096,
                    extra_destination_path="/opt/xxx1/backups",
                ),
                Backup(
                    filename="envmanager-2.db",
                    trigger=BackupTrigger.MANUAL,
                    status=BackupStatus.FAILURE,
                    error="disk full",
                ),
            ]
        )
        await session.commit()
        stored = (await session.execute(select(Backup).order_by(Backup.id))).scalars().all()
    assert [backup.status for backup in stored] == [BackupStatus.SUCCESS, BackupStatus.FAILURE]
    assert stored[0].size_bytes == 4096
    assert stored[0].extra_destination_path == "/opt/xxx1/backups"
    assert stored[1].error == "disk full"
    assert stored[1].size_bytes is None
