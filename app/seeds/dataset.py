"""Deterministic development and test dataset.

The same dataset backs the permission tests, the Playwright global setup and the
``envmanager dev seed`` command. Extend it when a new scenario needs data instead of building
fixtures by hand in a test.

Structure (project, deployment, service, environments):

    rentals
      prod (/opt/rentals/prod)
        mysql: .env.db, .env.db.secrets
        web: .env.web
      staging (/opt/rentals/staging)
        web: .env.web
    billing
      main (/opt/billing/main)
        api: .env.api
        worker: .env.worker

Allowed paths: /opt/rentals and /opt/billing (the deployment roots lie under them).

Deploy runs: rentals/prod web succeeded (by admin), then mysql failed (by root, the newest run of
the project); billing/main has only a dry run, so it counts as never deployed.

API tokens: billing-ci, owned by admin, restricted to the billing .env.api environment.

Dist links: .env.db and .env.web (rentals prod) point at environments/.env.db-dist and
environments/.env.web-dist below their deployment root; the files themselves are created by the
tests that need them.

Access:

    superadmin            every project, no grants needed
    admin                  projects rentals and billing
    viewer                 project rentals
    project_admin          project rentals
    deployment_admin       deployment rentals/prod
    env_scoped_admin       environment .env.db only (not .env.db.secrets)
    env_scoped_viewer      environments .env.db and billing .env.api
    ungranted_admin        no grants
    totp_user              admin without grants, second factor on (DEV_TOTP_SECRET)
    totp_user_alt          the same, a second account so parallel browser runs never share a code
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.passwords import hash_password
from app.models import (
    AccessGrant,
    AllowedPath,
    ApiToken,
    ApiTokenRestriction,
    Deployment,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    GrantLevel,
    Project,
    RestrictionLevel,
    Service,
    User,
    UserGroup,
)
from app.services import totp

# Fixed password of every seeded user. Development and testing only: the seed command refuses
# to run in any other environment and nothing else may reference this value.
DEV_PASSWORD = "dev-only-password"  # noqa: S105

# Fixed authenticator secret of the seeded second factor user, same development and testing only
# restriction as the password.
DEV_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105
TOTP_USER_NAME = "totp_user"
TOTP_ALT_USER_NAME = "totp_user_alt"


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

    rentals: Project
    billing: Project


@dataclass(frozen=True)
class SeedDeployments:
    """The seeded deployments."""

    rentals_prod: Deployment
    rentals_staging: Deployment
    billing_main: Deployment


@dataclass(frozen=True)
class SeedServices:
    """The seeded services."""

    mysql: Service
    web: Service
    staging_web: Service
    api: Service
    worker: Service


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
class SeedData:
    """Named references to everything the seed created."""

    users: SeedUsers
    projects: SeedProjects
    deployments: SeedDeployments
    services: SeedServices
    envs: SeedEnvironments
    allowed_paths: tuple[AllowedPath, ...]
    deploy_runs: tuple[DeployRun, ...]
    tokens: tuple[ApiToken, ...]


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
        db.add(
            EnvironmentVersion(
                environment_id=environment.id,
                number=number,
                content_encrypted=crypto.encrypt(content),
                content_hash=crypto.content_hash(content),
                comment=f"seed version {number}",
                author_id=author.id,
            )
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


async def ensure_activity(db: AsyncSession) -> tuple[tuple[DeployRun, ...], tuple[ApiToken, ...]]:
    """Deploy runs and the restricted API token of the dataset, created when missing.

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
            await db.execute(select(Environment.id).where(Environment.name == ".env.api"))
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
    await db.flush()
    return runs, (token,)


async def seed(db: AsyncSession, crypto: CryptoService) -> SeedData:
    """Create the dataset in a migrated database and return named references.

    Raises ``SeedError`` when the dataset, or any user it would create, already exists.
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
    projects = SeedProjects(rentals=Project(name="rentals"), billing=Project(name="billing"))
    db.add_all(vars(projects).values())
    await db.flush()

    deployments = SeedDeployments(
        rentals_prod=Deployment(
            project_id=projects.rentals.id, name="prod", root_path="/opt/rentals/prod"
        ),
        rentals_staging=Deployment(
            project_id=projects.rentals.id, name="staging", root_path="/opt/rentals/staging"
        ),
        billing_main=Deployment(
            project_id=projects.billing.id, name="main", root_path="/opt/billing/main"
        ),
    )
    db.add_all(vars(deployments).values())
    await db.flush()

    services = SeedServices(
        mysql=Service(deployment_id=deployments.rentals_prod.id, name="mysql"),
        web=Service(deployment_id=deployments.rentals_prod.id, name="web"),
        staging_web=Service(deployment_id=deployments.rentals_staging.id, name="web"),
        api=Service(deployment_id=deployments.billing_main.id, name="api"),
        worker=Service(deployment_id=deployments.billing_main.id, name="worker"),
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
        (users.admin, GrantLevel.PROJECT, projects.rentals),
        (users.admin, GrantLevel.PROJECT, projects.billing),
        (users.viewer, GrantLevel.PROJECT, projects.rentals),
        (users.project_admin, GrantLevel.PROJECT, projects.rentals),
        (users.deployment_admin, GrantLevel.DEPLOYMENT, deployments.rentals_prod),
        (users.env_scoped_admin, GrantLevel.ENVIRONMENT, envs.db),
        (users.env_scoped_viewer, GrantLevel.ENVIRONMENT, envs.db),
        (users.env_scoped_viewer, GrantLevel.ENVIRONMENT, envs.api),
    ):
        db.add(AccessGrant(user_id=user.id, level=level, **{f"{level.value}_id": target.id}))
    deploy_runs, tokens = await ensure_activity(db)
    await db.commit()
    return SeedData(
        users=users,
        projects=projects,
        deployments=deployments,
        services=services,
        envs=envs,
        allowed_paths=allowed_paths,
        deploy_runs=deploy_runs,
        tokens=tokens,
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
