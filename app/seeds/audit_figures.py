"""Audit history that covers every kind of entry the audit log screen shows.

Separate from the main dataset because the audit log needs entries of every actor kind and scope
level: an API token, the CLI, an anonymous failed login, entries with a source address, details
and version ids, entries at project, deployment, service and environment level, an entry about a
project that no longer exists, and enough scope-less entries to need a second page. All of them
are dated before the dataset's deploy history ends, so a date range up to ``LAST_FIGURE_DAY``
selects exactly the seeded history.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApiToken,
    AuditActorType,
    AuditEntry,
    AuditResult,
    Deployment,
    Environment,
    EnvironmentVersion,
    Project,
    Service,
    User,
)
from app.services.audit import AuditActor, record_actor_audit, record_audit

BULK_ACTION = "backup.create"
BULK_COUNT = 55
DELETED_PROJECT_ID = 900001
DELETED_PROJECT_NAME = "retired"
CI_CLIENT_NAME = "billing-ci"
FIRST_FIGURE_DAY = date(2026, 7, 1)
LAST_FIGURE_DAY = date(2026, 10, 17)


def _at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A UTC moment of the audit history."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@dataclass(frozen=True)
class _Scope:
    """Ids of the item an entry concerns and its ancestors."""

    project: int | None = None
    deployment: int | None = None
    service: int | None = None
    environment: int | None = None


async def _version_id(db: AsyncSession, environment_id: int, number: int) -> int:
    """The id of a stored version of an environment."""
    return (
        await db.execute(
            select(EnvironmentVersion.id).where(
                EnvironmentVersion.environment_id == environment_id,
                EnvironmentVersion.number == number,
            )
        )
    ).scalar_one()


async def ensure_audit_figures(db: AsyncSession) -> None:
    """Create the audit figures when missing. The caller commits.

    Looks the users, structure and token up by name, so it also completes a database seeded
    before it existed. It leaves the database alone once the bulk entries exist.
    """
    already = (
        await db.execute(
            select(func.count()).select_from(AuditEntry).where(AuditEntry.action == BULK_ACTION)
        )
    ).scalar_one()
    if already:
        return
    users = {user.name: user for user in (await db.execute(select(User))).scalars()}
    projects = {
        name: id_ for id_, name in (await db.execute(select(Project.id, Project.name))).all()
    }
    deployments = {
        (project, name): (id_, project_id)
        for id_, name, project, project_id in (
            await db.execute(
                select(Deployment.id, Deployment.name, Project.name, Project.id).join(
                    Project, Project.id == Deployment.project_id
                )
            )
        ).all()
    }
    services = {
        (deployment, name): id_
        for id_, name, deployment in (
            await db.execute(
                select(Service.id, Service.name, Deployment.name).join(
                    Deployment, Deployment.id == Service.deployment_id
                )
            )
        ).all()
    }
    environments = {
        (deployment, name): id_
        for id_, name, deployment in (
            await db.execute(
                select(Environment.id, Environment.name, Deployment.name)
                .join(Service, Service.id == Environment.service_id)
                .join(Deployment, Deployment.id == Service.deployment_id)
            )
        ).all()
    }

    def scope(
        project: str,
        deployment: str,
        service: str | None = None,
        environment: str | None = None,
    ) -> _Scope:
        """The ids of an item by names, with the ancestors it has."""
        deployment_id, project_id = deployments[(project, deployment)]
        return _Scope(
            project=project_id,
            deployment=deployment_id,
            service=services[(deployment, service)] if service else None,
            environment=environments[(deployment, environment)] if environment else None,
        )

    async def user_entry(
        who: str,
        ip: str,
        at: datetime,
        action: str,
        result: AuditResult,
        where: _Scope,
        target_type: str,
        target_name: str,
        details: dict[str, object],
        versions: list[int] | None = None,
    ) -> None:
        """Add the entry of a signed-in user at a fixed time, with the versions it involved."""
        entry = await record_actor_audit(
            db,
            AuditActor.for_user(users[who], ip),
            action=action,
            result=result,
            target_type=target_type,
            target_name=target_name,
            project_id=where.project,
            deployment_id=where.deployment,
            service_id=where.service,
            environment_id=where.environment,
            details=details,
        )
        entry.created_at = at
        entry.version_ids = versions or []

    rentals_db = scope("rentals", "prod", "mysql", ".env.db")
    secrets = scope("rentals", "prod", "mysql", ".env.db.secrets")
    billing_api = scope("billing", "main", "api", ".env.api")
    db_v2 = await _version_id(db, environments[("prod", ".env.db")], 2)
    secrets_v1 = await _version_id(db, environments[("prod", ".env.db.secrets")], 1)
    api_v1 = await _version_id(db, environments[("main", ".env.api")], 1)

    await user_entry(
        "admin",
        "10.0.4.31",
        _at(2026, 10, 17, 9, 12),
        "environment.save",
        AuditResult.SUCCESS,
        rentals_db,
        "environment",
        ".env.db",
        {"version": 2, "mode": "keys", "changed_keys": ["DB_PORT"]},
        [db_v2],
    )
    await user_entry(
        "root",
        "10.0.4.12",
        _at(2026, 10, 16, 14, 20),
        "environment.reveal",
        AuditResult.SUCCESS,
        secrets,
        "environment",
        ".env.db.secrets",
        {"version": 1, "scope": "key", "key": "DB_PASSWORD"},
        [secrets_v1],
    )
    await user_entry(
        "root",
        "10.0.4.12",
        _at(2026, 10, 13, 16, 5),
        "deployment.update",
        AuditResult.SUCCESS,
        scope("rentals", "prod"),
        "deployment",
        "prod",
        {"drift_policy": "halt", "missing_keys_policy": "block"},
    )
    await user_entry(
        "root",
        "10.0.4.12",
        _at(2026, 10, 12, 11, 30),
        "project.rename",
        AuditResult.SUCCESS,
        _Scope(project=projects["rentals"]),
        "project",
        "rentals",
        {"previous_name": "rentals-old"},
    )
    await user_entry(
        "admin",
        "10.0.4.31",
        _at(2026, 10, 16, 18, 40),
        "environment.dist_check",
        AuditResult.BLOCKED,
        billing_api,
        "environment",
        ".env.api",
        {"outcome": "blocked", "policy": "block", "version": 1, "missing": 2},
        [api_v1],
    )
    await user_entry(
        "root",
        "10.0.4.12",
        _at(2026, 10, 11, 9, 0),
        "project.delete",
        AuditResult.SUCCESS,
        _Scope(project=DELETED_PROJECT_ID),
        "project",
        DELETED_PROJECT_NAME,
        {"deployments": 2},
    )

    token = (
        await db.execute(select(ApiToken).where(ApiToken.name == CI_CLIENT_NAME))
    ).scalar_one_or_none()
    if token is not None:
        entry = await record_actor_audit(
            db,
            AuditActor.for_token(token, users["admin"], "203.0.113.9"),
            action="service.deploy",
            result=AuditResult.HALTED,
            target_type="service",
            target_name="api",
            project_id=billing_api.project,
            deployment_id=billing_api.deployment,
            service_id=billing_api.service,
            details={
                "dry_run": False,
                "force": False,
                "status": "halted",
                "post_deploy": "none",
                "environments": [
                    {
                        "environment": ".env.api",
                        "version": 1,
                        "status": "halted",
                        "drift": "drift",
                        "forced": False,
                    }
                ],
            },
        )
        entry.created_at = _at(2026, 10, 16, 18, 10)
        entry.version_ids = [api_v1]

    entry = await record_actor_audit(
        db,
        AuditActor(AuditActorType.CLI, ip_address="127.0.0.1"),
        action="user.create",
        result=AuditResult.SUCCESS,
        target_type="user",
        target_name="new.hire",
        details={"group": "viewer"},
    )
    entry.created_at = _at(2026, 10, 16, 8, 2)
    entry = await record_audit(
        db,
        action="login.failure",
        result=AuditResult.FAILURE,
        actor_type=AuditActorType.USER,
        ip_address="198.51.100.7",
        details={"reason": "invalid_credentials"},
    )
    entry.created_at = _at(2026, 10, 14, 22, 15)

    for index in range(BULK_COUNT):
        entry = await record_audit(
            db,
            action=BULK_ACTION,
            result=AuditResult.FAILURE if index % 11 == 10 else AuditResult.SUCCESS,
            actor_type=AuditActorType.SYSTEM,
            actor_name="system",
            target_type="backup",
            target_name=f"backup-{index + 1:02d}",
            details={"trigger": "scheduled", "size_bytes": 1_048_576 + index},
        )
        entry.created_at = _at(2026, 9, 30, 2, 0) - timedelta(days=index)
    await db.flush()
