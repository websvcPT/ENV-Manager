"""Deterministic development and test dataset.

The same dataset backs the permission tests, the Playwright global setup and the
``envmanager dev seed`` command. Extend it when a new scenario needs data instead of building
fixtures by hand in a test.

Structure (project, deployment, service, environments):

    Fancy Proj. Name
      prod (/opt/rentals/prod)
        mysql: .env.db, .env.db.secrets
        web: .env.web
      staging (/opt/rentals/staging)
        web: .env.web
    billing
      main (/opt/billing/main)
        api: .env.api
        worker: .env.worker
      legacy (/srv/old/legacy, outside the allowed paths)
        cron: no environments

The first project is a display name (spaces, a dot and capitals), while its host folders keep
plain paths.

Allowed paths: /opt/rentals and /opt/billing (the deployment roots lie under them, except legacy).

Deploy runs: Fancy Proj. Name/prod web succeeded (by admin, post-deploy ran), then mysql failed
(by root, post-deploy skipped, the newest run of the project); billing/main has only a dry run, so
it counts as never deployed. Each run has per-environment results. The prod deployment writes
files as www-data:www-data.

Audit history: deploy entries for Fancy Proj. Name prod web (success) and mysql (failure), and
file saves of .env.db and billing .env.api, each with the scope ids of its item.

API tokens: billing-ci, owned by admin, restricted to the billing .env.api environment.

Dist links: .env.db and .env.web (Fancy Proj. Name prod) point at environments/.env.db-dist and
environments/.env.web-dist below their deployment root; the files themselves are created by the
tests that need them.

Access:

    superadmin            every project, no grants needed
    admin                  projects Fancy Proj. Name and billing
    viewer                 project Fancy Proj. Name
    project_admin          project Fancy Proj. Name
    deployment_admin       deployment Fancy Proj. Name/prod
    env_scoped_admin       environment .env.db only (not .env.db.secrets)
    env_scoped_viewer      environments .env.db and billing .env.api
    ungranted_admin        no grants
    totp_user              admin without grants, second factor on (DEV_TOTP_SECRET)
    totp_user_alt          the same, a second account so parallel browser runs never share a code

The audit log figures (``app/seeds/audit_figures.py``) are not part of ``seed``: tests that read the
audit log by action would find entries they did not write. The development seed command and the
audit log tests add them with ``ensure_audit_figures``.

The demo project (``app/seeds/demo.py``) is added only when a demo root is passed to ``seed``;
it brings grants for project_admin (the project) and env_scoped_admin (its .env.db file) and
leaves every other user's view untouched.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.passwords import hash_password
from app.models import (
    AccessGrant,
    AllowedPath,
    ApiToken,
    ApiTokenRestriction,
    AuditEntry,
    AuditResult,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    GrantLevel,
    HookType,
    PostDeployStatus,
    Project,
    RestrictionLevel,
    Service,
    ServiceHook,
    User,
    UserGroup,
)
from app.models.types import utc_now
from app.seeds.demo import SeedDemo, ensure_demo_project
from app.services import totp
from app.services.audit import AuditActor, record_actor_audit
from app.services.versions import create_version

# Fixed password of every seeded user. Development and testing only: the seed command refuses
# to run in any other environment and nothing else may reference this value.
DEV_PASSWORD = "dev-only-password"  # noqa: S105

# Fixed authenticator secret of the seeded second factor user, same development and testing only
# restriction as the password.
DEV_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105
TOTP_USER_NAME = "totp_user"
TOTP_ALT_USER_NAME = "totp_user_alt"

# The seeded project with a display name (capitals, a dot and spaces), and the plain name a
# database seeded before project names became display names still holds it under.
FANCY_PROJECT_NAME = "Fancy Proj. Name"
_FORMER_FANCY_PROJECT_NAME = "rentals"

# The stored command of the staging web service, and the texts older seeds stored for it and
# for the demo services, which named their service with a placeholder.
STAGING_WEB_COMMAND = "docker compose up -d --no-deps web"
_PLACEHOLDER_SEED_COMMANDS = (
    "docker compose up -d --no-deps {service_name}",
    "docker compose up -d --no-deps --force-recreate {service_name}",
)

# Root of the seeded deployment that lies outside every allowed path.
LEGACY_ROOT = "/srv/old/legacy"

# Directory the development compose file mounts for deployments; the development seed allows it.
DEV_ALLOWED_ROOT = "/srv/deployments"


# Name and distinctive secret values of the seeded editor file; tests search masked pages for
# these values.
EDITOR_ENVIRONMENT_NAME = ".env.db.app"
EDITOR_SECRET_VALUES = (
    "Zq7vK2mPw9",
    "Tn4xB8cL1d",
    "MIIDdzCCAl7sentinelCertBody",
    "Ke3rH6vNa5",
)
_EDITOR_VERSIONS = (
    "# Database connection\n"
    "MYSQL_HOST=db.internal.example.com\n"
    "MYSQL_PORT=3306\n"
    "MYSQL_PASSWORD=Zq7vK2mPw9\n",
    "# Database connection\n"
    "MYSQL_HOST=db.internal.example.com\n"
    "MYSQL_PORT=3306\n"
    "MYSQL_PASSWORD=Ke3rH6vNa5\n"
    "# rotated by hand, the old password was Zq7vK2mPw9\n"
    "MAX_CONNECTIONS=150\n",
    "# Database connection\n"
    "MYSQL_HOST=db.internal.example.com\n"
    "MYSQL_PORT=3306\n"
    "MYSQL_PASSWORD=Ke3rH6vNa5\n"
    "# rotated by hand, the old password was Tn4xB8cL1d\n"
    "export MAX_CONNECTIONS=200\n"
    "TLS_MIN_VERSION=TLSv1.2\n"
    'TLS_CA_CERT="-----BEGIN CERTIFICATE-----\n'
    "MIIDdzCCAl7sentinelCertBody\n"
    'AQIEAgAAuTANBgkqhkiG9w0BAQUFADBa\n-----END CERTIFICATE-----"\n',
)


class SeedError(Exception):
    """The dataset cannot be created."""


@dataclass(frozen=True)
class SeedUsers:
    """The seeded users."""

    superadmin: User
    admin: User
    viewer: User
    project_admin: User
    deployment_admin: User
    env_scoped_admin: User
    env_scoped_viewer: User
    ungranted_admin: User
    totp_user: User
    totp_user_alt: User


@dataclass(frozen=True)
class SeedProjects:
    """The seeded projects."""

    fancy: Project
    billing: Project


@dataclass(frozen=True)
class SeedDeployments:
    """The seeded deployments."""

    fancy_prod: Deployment
    fancy_staging: Deployment
    billing_main: Deployment
    billing_legacy: Deployment


@dataclass(frozen=True)
class SeedServices:
    """The seeded services."""

    mysql: Service
    web: Service
    staging_web: Service
    api: Service
    worker: Service
    cron: Service


@dataclass(frozen=True)
class SeedEnvironments:
    """The seeded environments."""

    db: Environment
    db_secrets: Environment
    web: Environment
    staging_web: Environment
    api: Environment
    worker: Environment


@dataclass(frozen=True)
class SeedServiceFigures:
    """The staging web environments that cover every status: pending, blocked and never deployed."""

    tuning: Environment
    replica: Environment
    ca: Environment


@dataclass(frozen=True)
class SeedData:
    """Named references to everything the seed created.

    ``editor`` is the production MySQL file with comments, a multiline value and three versions.
    ``demo`` is the demo project and is None unless a demo root was given.
    """

    users: SeedUsers
    projects: SeedProjects
    deployments: SeedDeployments
    services: SeedServices
    envs: SeedEnvironments
    figures: SeedServiceFigures
    editor: Environment
    allowed_paths: tuple[AllowedPath, ...]
    deploy_runs: tuple[DeployRun, ...]
    tokens: tuple[ApiToken, ...]
    demo: SeedDemo | None = None


@lru_cache
def _dev_password_hash() -> str:
    """Hash of the dev password, computed once because Argon2id is deliberately slow."""
    return hash_password(DEV_PASSWORD)


def _user(name: str, group: UserGroup) -> User:
    """A seeded user with the dev password."""
    return User(
        name=name,
        email=f"{name}@example.test",
        password_hash=_dev_password_hash(),
        group=group,
    )


async def ensure_totp_user(
    db: AsyncSession, crypto: CryptoService, name: str = TOTP_USER_NAME
) -> User:
    """A seeded user that signs in with a second factor, created when missing.

    Separate from the full dataset so a database seeded before this user existed can get it
    without being reseeded. The caller commits.
    """
    existing = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if existing is not None:
        return existing
    user = _user(name, UserGroup.ADMIN)
    db.add(user)
    await db.flush()
    totp.store_secret(user, DEV_TOTP_SECRET, crypto)
    user.totp_enabled = True
    user.totp_enabled_at = utc_now()
    return user


async def _environment(
    db: AsyncSession,
    crypto: CryptoService,
    service: Service,
    name: str,
    contents: list[str],
    author: User,
    dist_path: str | None = None,
) -> Environment:
    """An environment with one saved version per content, oldest first."""
    environment = Environment(
        service_id=service.id,
        name=name,
        target_path=f"environments/{name}",
        dist_path=dist_path,
    )
    db.add(environment)
    await db.flush()
    for number, content in enumerate(contents, start=1):
        await create_version(
            db, crypto, environment.id, content, author=author, comment=f"seed version {number}"
        )
    return environment


def _at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A UTC moment of the dataset history."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _run(
    service_id: int,
    service_name: str,
    deployment_name: str,
    actor: User,
    status: DeployStatus,
    started_at: datetime,
    dry_run: bool,
) -> DeployRun:
    """A finished deploy run of a service by a user."""
    return DeployRun(
        service_id=service_id,
        service_name=service_name,
        deployment_name=deployment_name,
        triggered_by_user_id=actor.id,
        trigger=DeployTrigger.UI,
        dry_run=dry_run,
        status=status,
        started_at=started_at,
        finished_at=started_at,
    )


async def _ensure_audit_history(db: AsyncSession, users: dict[str, User]) -> None:
    """Audit entries of the dataset's history (deploys and file saves), created when missing.

    Entries carry the scope ids of the item they concern, so scoped users see only their part.
    """
    already = (
        await db.execute(
            select(func.count())
            .select_from(AuditEntry)
            .where(AuditEntry.action.in_(["deploy", "file_save"]))
        )
    ).scalar_one()
    if already:
        return
    services = {
        (deployment, service): (service_id, deployment_id, project_id)
        for service_id, deployment, service, deployment_id, project_id in (
            await db.execute(
                select(
                    Service.id, Deployment.name, Service.name, Deployment.id, Deployment.project_id
                ).join(Deployment, Deployment.id == Service.deployment_id)
            )
        ).all()
    }
    environments = {
        (deployment, name): environment_id
        for environment_id, deployment, name in (
            await db.execute(
                select(Environment.id, Deployment.name, Environment.name)
                .join(Service, Service.id == Environment.service_id)
                .join(Deployment, Deployment.id == Service.deployment_id)
            )
        ).all()
    }
    history = (
        ("deploy", "admin", AuditResult.SUCCESS, _at(2026, 10, 16, 9, 42), "prod", "web", None),
        ("deploy", "root", AuditResult.FAILURE, _at(2026, 10, 17, 10, 5), "prod", "mysql", None),
        (
            "file_save",
            "root",
            AuditResult.SUCCESS,
            _at(2026, 10, 15, 17, 40),
            "prod",
            "mysql",
            ".env.db",
        ),
        (
            "file_save",
            "root",
            AuditResult.SUCCESS,
            _at(2026, 10, 14, 8, 15),
            "main",
            "api",
            ".env.api",
        ),
    )
    for action, actor, result, at, deployment, service, environment in history:
        service_id, deployment_id, project_id = services[(deployment, service)]
        await record_actor_audit(
            db,
            AuditActor.for_user(users[actor]),
            action=action,
            result=result,
            target_type="environment" if environment else "service",
            target_name=environment if environment else service,
            project_id=project_id,
            deployment_id=deployment_id,
            service_id=service_id,
            environment_id=environments[(deployment, environment)] if environment else None,
            created_at=at,
        )
    await db.flush()


async def ensure_activity(db: AsyncSession) -> tuple[tuple[DeployRun, ...], tuple[ApiToken, ...]]:
    """Deploy runs, audit history and the restricted API token, created when missing.

    Separate from the full dataset so a database seeded before they existed can get them without
    being reseeded. Looks the users, services and environment up by name. The caller commits.
    """
    user_rows = await db.execute(select(User).where(User.name.in_(["root", "admin"])))
    users = {user.name: user for user in user_rows.scalars()}
    services = {
        (deployment, service): service_id
        for service_id, deployment, service in (
            await db.execute(
                select(Service.id, Deployment.name, Service.name).join(
                    Deployment, Deployment.id == Service.deployment_id
                )
            )
        ).all()
    }
    runs: tuple[DeployRun, ...] = ()
    if (await db.execute(select(func.count()).select_from(DeployRun))).scalar_one() == 0:
        ok, failed = DeployStatus.SUCCESS, DeployStatus.FAILED
        history = (
            ("prod", "web", "admin", ok, _at(2026, 10, 16, 9, 42), False),
            ("prod", "mysql", "root", failed, _at(2026, 10, 17, 10, 5), False),
            ("main", "api", "admin", ok, _at(2026, 10, 17, 11, 0), True),
        )
        runs = tuple(
            _run(services[(deployment, name)], name, deployment, users[actor], status, at, dry)
            for deployment, name, actor, status, at, dry in history
        )
        db.add_all(runs)
    token = (
        await db.execute(select(ApiToken).where(ApiToken.name == "billing-ci"))
    ).scalar_one_or_none()
    if token is None:
        api_environment_id = (
            await db.execute(
                select(Environment.id)
                .join(Service, Service.id == Environment.service_id)
                .join(Deployment, Deployment.id == Service.deployment_id)
                .where(Deployment.name == "main", Environment.name == ".env.api")
            )
        ).scalar_one()
        token = ApiToken(
            owner_id=users["admin"].id,
            name="billing-ci",
            token_hash=hashlib.sha256(b"seed-token-billing-ci").hexdigest(),
            prefix="emt_seed",
            operations=["deploy"],
        )
        db.add(token)
        await db.flush()
        db.add(
            ApiTokenRestriction(
                token_id=token.id,
                level=RestrictionLevel.ENVIRONMENT,
                environment_id=api_environment_id,
            )
        )
    await _ensure_audit_history(db, users)
    await db.flush()
    return runs, (token,)


async def ensure_deployment_history(db: AsyncSession) -> None:
    """The legacy deployment, per-environment deploy results and post-deploy outcomes of the runs.

    Separate from the full dataset so a database seeded before they existed can get them without
    being reseeded. Looks the runs and environments up by name and only fills what is missing.
    The caller commits.
    """
    billing = (
        await db.execute(select(Project).where(Project.name == "billing"))
    ).scalar_one_or_none()
    if billing is not None:
        legacy = (
            await db.execute(
                select(Deployment).where(
                    Deployment.project_id == billing.id, Deployment.name == "legacy"
                )
            )
        ).scalar_one_or_none()
        if legacy is None:
            legacy = Deployment(project_id=billing.id, name="legacy", root_path=LEGACY_ROOT)
            db.add(legacy)
            await db.flush()
            db.add(Service(deployment_id=legacy.id, name="cron"))
    prod = (
        await db.execute(select(Deployment).where(Deployment.name == "prod"))
    ).scalar_one_or_none()
    if prod is not None and prod.file_owner is None:
        prod.file_owner = prod.file_group = "www-data"

    environments = {
        (deployment, name): environment_id
        for environment_id, deployment, name in (
            await db.execute(
                select(Environment.id, Deployment.name, Environment.name)
                .join(Service, Service.id == Environment.service_id)
                .join(Deployment, Deployment.id == Service.deployment_id)
            )
        ).all()
    }
    versions = {
        (environment_id, number): (version_id, number)
        for version_id, environment_id, number in (
            await db.execute(
                select(
                    EnvironmentVersion.id,
                    EnvironmentVersion.environment_id,
                    EnvironmentVersion.number,
                )
            )
        ).all()
    }
    written, failed, planned = (
        DeployResultStatus.WRITTEN,
        DeployResultStatus.FAILED,
        DeployResultStatus.WOULD_WRITE,
    )
    outcomes = {
        ("prod", "web"): (
            PostDeployStatus.RAN,
            0,
            "web recreated",
            None,
            (("prod", ".env.web", 1, written),),
        ),
        ("prod", "mysql"): (
            PostDeployStatus.SKIPPED,
            None,
            None,
            "deploy did not finish",
            (("prod", ".env.db", 2, failed), ("prod", ".env.db.secrets", 1, failed)),
        ),
        ("main", "api"): (
            PostDeployStatus.NONE,
            None,
            None,
            None,
            (("main", ".env.api", 1, planned),),
        ),
    }
    runs = (await db.execute(select(DeployRun))).scalars().all()
    have_results = set((await db.execute(select(DeployResult.run_id))).scalars())
    for run in runs:
        outcome = outcomes.get((run.deployment_name, run.service_name))
        if outcome is None:
            continue
        post_status, exit_code, output, reason, results = outcome
        if run.post_deploy_status is PostDeployStatus.NONE:
            run.post_deploy_status = post_status
            run.post_deploy_exit_code = exit_code
            run.post_deploy_output = output
            run.post_deploy_reason = reason
        if run.id in have_results:
            continue
        for deployment, name, number, status in results:
            environment_id = environments[(deployment, name)]
            version_id, _number = versions[(environment_id, number)]
            db.add(
                DeployResult(
                    run_id=run.id,
                    environment_id=environment_id,
                    environment_name=name,
                    version_id=version_id,
                    target_path=f"environments/{name}",
                    status=status,
                )
            )
    await db.flush()


async def ensure_service_figures(
    db: AsyncSession, crypto: CryptoService
) -> SeedServiceFigures | None:
    """Environments in every status and a post-deploy command on the staging web service.

    The staging `web` service gets a deployed file (the existing one), one saved again after its
    deploy (pending), one whose only attempt was blocked and one never deployed, plus a stored
    post-deploy command. Separate from the full dataset so a database seeded before it existed
    can get it; does nothing when the staging service is missing and returns the environments
    that were created or found. The caller commits.
    """
    service = (
        await db.execute(
            select(Service)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .where(Deployment.name == "staging", Service.name == "web")
        )
    ).scalar_one_or_none()
    root = (await db.execute(select(User).where(User.name == "root"))).scalar_one_or_none()
    admin = (await db.execute(select(User).where(User.name == "admin"))).scalar_one_or_none()
    if service is None or root is None or admin is None:
        return None
    found = {
        environment.name: environment
        for environment in (
            await db.execute(select(Environment).where(Environment.service_id == service.id))
        ).scalars()
    }
    if ".env.web.tuning" in found:
        return SeedServiceFigures(
            tuning=found[".env.web.tuning"],
            replica=found[".env.web.replica"],
            ca=found["web-ca.pem"],
        )
    web = (
        await db.execute(
            select(Environment).where(
                Environment.service_id == service.id, Environment.name == ".env.web"
            )
        )
    ).scalar_one()
    tuning = await _environment(
        db, crypto, service, ".env.web.tuning", ["WORKERS=2\n", "WORKERS=4\n"], root
    )
    replica = await _environment(db, crypto, service, ".env.web.replica", ["REPLICA=on\n"], root)
    ca = await _environment(
        db,
        crypto,
        service,
        "web-ca.pem",
        ["-----BEGIN CERTIFICATE-----\nc2VlZA==\n-----END CERTIFICATE-----\n"],
        root,
    )
    db.add(
        ServiceHook(
            service_id=service.id,
            type=HookType.POST_DEPLOY,
            commands=STAGING_WEB_COMMAND,
        )
    )
    await db.flush()
    version_ids = {
        (environment_id, number): version_id
        for version_id, environment_id, number in (
            await db.execute(
                select(
                    EnvironmentVersion.id,
                    EnvironmentVersion.environment_id,
                    EnvironmentVersion.number,
                ).where(EnvironmentVersion.environment_id.in_([web.id, tuning.id, replica.id]))
            )
        ).all()
    }
    blocked_run = _run(
        service.id,
        service.name,
        "staging",
        root,
        DeployStatus.BLOCKED,
        _at(2026, 10, 13, 10, 0),
        False,
    )
    written_run = _run(
        service.id,
        service.name,
        "staging",
        admin,
        DeployStatus.SUCCESS,
        _at(2026, 10, 15, 9, 0),
        False,
    )
    written_run.post_deploy_status = PostDeployStatus.RAN
    written_run.post_deploy_exit_code = 0
    written_run.post_deploy_output = "web recreated"
    blocked_run.post_deploy_status = PostDeployStatus.SKIPPED
    blocked_run.post_deploy_reason = "deploy did not finish"
    db.add_all((blocked_run, written_run))
    await db.flush()
    for run, environment, number, status in (
        (blocked_run, replica, 1, DeployResultStatus.BLOCKED),
        (written_run, web, 1, DeployResultStatus.WRITTEN),
        (written_run, tuning, 1, DeployResultStatus.WRITTEN),
    ):
        db.add(
            DeployResult(
                run_id=run.id,
                environment_id=environment.id,
                environment_name=environment.name,
                version_id=version_ids[(environment.id, number)],
                target_path=environment.target_path,
                status=status,
            )
        )
    await db.flush()
    return SeedServiceFigures(tuning=tuning, replica=replica, ca=ca)


async def ensure_editor_figures(db: AsyncSession, crypto: CryptoService) -> Environment | None:
    """A rich file for the editor: comments, an exported key, a multiline certificate and three
    versions whose values are distinctive (`EDITOR_SECRET_VALUES`).

    Added to the production MySQL service, so the superadmin, admin, viewer, project and
    deployment scoped users see it and the environment scoped users do not. Does nothing when
    the service is missing and returns the environment that was created or found. The caller
    commits.
    """
    service = (
        await db.execute(
            select(Service)
            .join(Deployment, Deployment.id == Service.deployment_id)
            .where(Deployment.name == "prod", Service.name == "mysql")
        )
    ).scalar_one_or_none()
    root = (await db.execute(select(User).where(User.name == "root"))).scalar_one_or_none()
    if service is None or root is None:
        return None
    found = (
        await db.execute(
            select(Environment).where(
                Environment.service_id == service.id,
                Environment.name == EDITOR_ENVIRONMENT_NAME,
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        return found
    return await _environment(
        db, crypto, service, EDITOR_ENVIRONMENT_NAME, list(_EDITOR_VERSIONS), root
    )


async def ensure_project_display_name(db: AsyncSession) -> None:
    """Give the seeded project its display name in a database seeded under the plain one.

    The project is renamed in place (ids, deployments and grants stay), and the names the
    seeded audit entries copied for it follow, so the log reads like a fresh seed. Does nothing
    when the plain name is gone or the display name is already taken. The caller commits.
    """
    former = (
        await db.execute(select(Project).where(Project.name == _FORMER_FANCY_PROJECT_NAME))
    ).scalar_one_or_none()
    taken = (await db.execute(select(Project.id).where(Project.name == FANCY_PROJECT_NAME))).first()
    if former is None or taken is not None:
        return
    former.name = FANCY_PROJECT_NAME
    await db.execute(
        update(AuditEntry)
        .where(
            AuditEntry.target_type == "project",
            AuditEntry.target_name == _FORMER_FANCY_PROJECT_NAME,
        )
        .values(target_name=FANCY_PROJECT_NAME)
    )
    await db.flush()


async def ensure_literal_commands(db: AsyncSession) -> None:
    """Name the service in the seeded commands that were stored with a placeholder.

    A database seeded before commands ran exactly as stored holds ``{service_name}`` in the
    staging web command and in both demo commands; each becomes the literal command of its own
    service. Any other stored text is left alone. The caller commits.
    """
    rows = (
        await db.execute(
            select(ServiceHook, Service.name)
            .join(Service, Service.id == ServiceHook.service_id)
            .where(ServiceHook.commands.in_(_PLACEHOLDER_SEED_COMMANDS))
        )
    ).all()
    for hook, service_name in rows:
        hook.commands = hook.commands.replace("{service_name}", service_name)
    await db.flush()


async def ensure_dev_host_paths(db: AsyncSession) -> None:
    """Allow the directory the development stack mounts, so the deployment form has real folders.

    Used by the development seed command only: the test dataset has no host directory to
    point at. The caller commits.
    """
    existing = (
        await db.execute(select(AllowedPath).where(AllowedPath.path == DEV_ALLOWED_ROOT))
    ).scalar_one_or_none()
    if existing is not None:
        return
    superadmin = (await db.execute(select(User).where(User.name == "root"))).scalar_one_or_none()
    db.add(AllowedPath(path=DEV_ALLOWED_ROOT, created_by_id=superadmin.id if superadmin else None))


async def seed(db: AsyncSession, crypto: CryptoService, demo_root: Path | None = None) -> SeedData:
    """Create the dataset in a migrated database and return named references.

    With ``demo_root`` the demo project rooted below it is created as well. Raises ``SeedError``
    when the dataset, or any user it would create, already exists.
    """
    taken = (
        (await db.execute(select(User.name).where(User.name.in_(_USER_NAMES)))).scalars().first()
    )
    if taken is not None:
        raise SeedError(f"user {taken!r} already exists, the dataset is already seeded")

    users = SeedUsers(
        superadmin=_user("root", UserGroup.SUPERADMIN),
        admin=_user("admin", UserGroup.ADMIN),
        viewer=_user("viewer", UserGroup.VIEWER),
        project_admin=_user("project_admin", UserGroup.ADMIN),
        deployment_admin=_user("deployment_admin", UserGroup.ADMIN),
        env_scoped_admin=_user("env_scoped_admin", UserGroup.ADMIN),
        env_scoped_viewer=_user("env_scoped_viewer", UserGroup.VIEWER),
        ungranted_admin=_user("ungranted_admin", UserGroup.ADMIN),
        totp_user=await ensure_totp_user(db, crypto),
        totp_user_alt=await ensure_totp_user(db, crypto, TOTP_ALT_USER_NAME),
    )
    db.add_all(vars(users).values())
    projects = SeedProjects(fancy=Project(name=FANCY_PROJECT_NAME), billing=Project(name="billing"))
    db.add_all(vars(projects).values())
    await db.flush()

    deployments = SeedDeployments(
        fancy_prod=Deployment(
            project_id=projects.fancy.id, name="prod", root_path="/opt/rentals/prod"
        ),
        fancy_staging=Deployment(
            project_id=projects.fancy.id, name="staging", root_path="/opt/rentals/staging"
        ),
        billing_main=Deployment(
            project_id=projects.billing.id, name="main", root_path="/opt/billing/main"
        ),
        billing_legacy=Deployment(
            project_id=projects.billing.id, name="legacy", root_path=LEGACY_ROOT
        ),
    )
    db.add_all(vars(deployments).values())
    await db.flush()

    services = SeedServices(
        mysql=Service(deployment_id=deployments.fancy_prod.id, name="mysql"),
        web=Service(deployment_id=deployments.fancy_prod.id, name="web"),
        staging_web=Service(deployment_id=deployments.fancy_staging.id, name="web"),
        api=Service(deployment_id=deployments.billing_main.id, name="api"),
        worker=Service(deployment_id=deployments.billing_main.id, name="worker"),
        cron=Service(deployment_id=deployments.billing_legacy.id, name="cron"),
    )
    db.add_all(vars(services).values())
    await db.flush()

    author = users.superadmin
    envs = SeedEnvironments(
        db=await _environment(
            db,
            crypto,
            services.mysql,
            ".env.db",
            ["DB_HOST=db\n", "DB_HOST=db\nDB_PORT=3306\n"],
            author,
            dist_path="environments/.env.db-dist",
        ),
        db_secrets=await _environment(
            db, crypto, services.mysql, ".env.db.secrets", ["DB_PASSWORD=seed-secret\n"], author
        ),
        web=await _environment(
            db,
            crypto,
            services.web,
            ".env.web",
            ["APP_ENV=prod\n"],
            author,
            dist_path="environments/.env.web-dist",
        ),
        staging_web=await _environment(
            db, crypto, services.staging_web, ".env.web", ["APP_ENV=staging\n"], author
        ),
        api=await _environment(
            db, crypto, services.api, ".env.api", ["API_KEY=seed-key\n"], author
        ),
        worker=await _environment(
            db, crypto, services.worker, ".env.worker", ["QUEUE=default\n"], author
        ),
    )

    allowed_paths = (
        AllowedPath(path="/opt/rentals", created_by_id=users.superadmin.id),
        AllowedPath(path="/opt/billing", created_by_id=users.superadmin.id),
    )
    db.add_all(allowed_paths)

    for user, level, target in (
        (users.admin, GrantLevel.PROJECT, projects.fancy),
        (users.admin, GrantLevel.PROJECT, projects.billing),
        (users.viewer, GrantLevel.PROJECT, projects.fancy),
        (users.project_admin, GrantLevel.PROJECT, projects.fancy),
        (users.deployment_admin, GrantLevel.DEPLOYMENT, deployments.fancy_prod),
        (users.env_scoped_admin, GrantLevel.ENVIRONMENT, envs.db),
        (users.env_scoped_viewer, GrantLevel.ENVIRONMENT, envs.db),
        (users.env_scoped_viewer, GrantLevel.ENVIRONMENT, envs.api),
    ):
        db.add(AccessGrant(user_id=user.id, level=level, **{f"{level.value}_id": target.id}))
    deploy_runs, tokens = await ensure_activity(db)
    await ensure_deployment_history(db)
    figures = await ensure_service_figures(db, crypto)
    if figures is None:
        raise SeedError("the staging web service was not created")
    editor = await ensure_editor_figures(db, crypto)
    if editor is None:
        raise SeedError("the editor environment was not created")
    demo = await ensure_demo_project(db, crypto, demo_root) if demo_root else None
    await db.commit()
    return SeedData(
        users=users,
        projects=projects,
        deployments=deployments,
        services=services,
        envs=envs,
        figures=figures,
        editor=editor,
        allowed_paths=allowed_paths,
        deploy_runs=deploy_runs,
        tokens=tokens,
        demo=demo,
    )


_USER_NAMES = [
    "root",
    "admin",
    "viewer",
    "project_admin",
    "deployment_admin",
    "env_scoped_admin",
    "env_scoped_viewer",
    "ungranted_admin",
    TOTP_USER_NAME,
    TOTP_ALT_USER_NAME,
]
