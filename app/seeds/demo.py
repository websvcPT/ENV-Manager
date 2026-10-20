"""The demo project: the database side of the committed Compose stack under ``demo/``.

Development and testing only, added when a demo root is given (``ENVMANAGER_DEMO_ROOT`` for the
``envmanager dev seed`` command). The project ``demo`` has one deployment, ``stack``, rooted at
``<demo root>/stack`` (which is also added to the allowed paths), with two services:

    api: .env.api, demo-ca.pem
    db:  .env.db, .env.db.secrets

Both services store the post-deploy command that recreates just that workload. The files that
sit in ``demo/stack/environments`` beside the Compose file after ``make demo-reset`` are the
copies in ``demo/baseline/environments``:

    .env.api          drift: the target differs from the managed version
    .env.db           identical to the managed version
    .env.db.secrets   identical, but its dist template has a key the managed version lacks (blocked)
    demo-ca.pem       missing target; a certificate, so its dist link is ignored (not applicable)

Grants: ``project_admin`` gets the whole project, ``env_scoped_admin`` only ``.env.db`` (so a
deploy of ``db`` by that user touches one of the two files). Every value is an obviously fake
demo value. Keep the constants here and the files under ``demo/`` in step; a test compares them.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    AccessGrant,
    AllowedPath,
    Deployment,
    Environment,
    FileType,
    GrantLevel,
    HookType,
    Project,
    Service,
    ServiceHook,
    User,
)
from app.services.versions import create_version

DEMO_PROJECT_NAME = "demo"
DEMO_DEPLOYMENT_NAME = "stack"
# Folder of the Compose stack inside the demo root.
DEMO_STACK_FOLDER = "stack"
DEMO_SERVICE_NAMES = ("api", "db")
# The stored command of each demo service names that service, as stored commands run literally.
DEMO_HOOK_COMMANDS = {
    name: f"docker compose up -d --no-deps --force-recreate {name}" for name in DEMO_SERVICE_NAMES
}

_API_SETTINGS = (
    "# Demo API settings. Fake values for the demo stack, not secrets.\n"
    "DEMO_API_PORT=8081\n"
    "DEMO_API_MODE=demo\n"
    "DEMO_API_TOKEN=demo-token-not-a-secret\n"
)
_DB_SETTINGS_FIRST = (
    "# Demo database settings. Fake values for the demo stack, not secrets.\n"
    "DEMO_DB_ENGINE=demo-sqlite\n"
    "DEMO_DB_PORT=5433\n"
)
_DB_SETTINGS = _DB_SETTINGS_FIRST + "DEMO_DB_POOL=4\n"
_DB_SECRETS = (
    "# Demo database credentials. Fake values for the demo stack, not secrets.\n"
    "DEMO_DB_USER=demo\n"
    "DEMO_DB_PASSWORD=demo-password-not-a-secret\n"
)
_CERTIFICATE = (
    "Demo fixture, not a real certificate and not a secret.\n"
    "-----BEGIN CERTIFICATE-----\n"
    "RGVtbyBmaXh0dXJlLCBub3QgYSByZWFsIGNlcnRpZmljYXRlLg==\n"
    "-----END CERTIFICATE-----\n"
)


@dataclass(frozen=True)
class DemoFile:
    """A managed file of the demo stack: service, saved versions (oldest first), dist link, type."""

    service: str
    name: str
    versions: tuple[str, ...]
    dist_path: str
    file_type: FileType = FileType.ENV

    @property
    def target_path(self) -> str:
        """Target below the deployment root; the same folder holds the dist templates."""
        return f"environments/{self.name}"

    @property
    def latest(self) -> str:
        """The content of the newest version."""
        return self.versions[-1]


DEMO_FILES = (
    DemoFile("api", ".env.api", (_API_SETTINGS,), "environments/.env.api-dist"),
    DemoFile("api", "demo-ca.pem", (_CERTIFICATE,), "environments/demo-ca.pem-dist", FileType.CERT),
    DemoFile("db", ".env.db", (_DB_SETTINGS_FIRST, _DB_SETTINGS), "environments/.env.db-dist"),
    DemoFile("db", ".env.db.secrets", (_DB_SECRETS,), "environments/.env.db.secrets-dist"),
)


@dataclass(frozen=True)
class SeedDemo:
    """Named references to the demo project the seed created or found."""

    project: Project
    deployment: Deployment
    api: Service
    db: Service
    env_api: Environment
    env_ca: Environment
    env_db: Environment
    env_db_secrets: Environment
    allowed_path: AllowedPath


async def _get_or_create_services(db: AsyncSession, deployment: Deployment) -> dict[str, Service]:
    """The demo services with their stored post-deploy command."""
    found = {
        service.name: service
        for service in (
            await db.execute(select(Service).where(Service.deployment_id == deployment.id))
        ).scalars()
    }
    for name in DEMO_SERVICE_NAMES:
        if name not in found:
            found[name] = Service(deployment_id=deployment.id, name=name)
            db.add(found[name])
    await db.flush()
    for name in DEMO_SERVICE_NAMES:
        hook = await db.scalar(
            select(ServiceHook).where(
                ServiceHook.service_id == found[name].id, ServiceHook.type == HookType.POST_DEPLOY
            )
        )
        if hook is None:
            db.add(
                ServiceHook(
                    service_id=found[name].id,
                    type=HookType.POST_DEPLOY,
                    commands=DEMO_HOOK_COMMANDS[name],
                )
            )
    return found


async def _get_or_create_environments(
    db: AsyncSession,
    crypto: CryptoService,
    services: dict[str, Service],
    author: User,
) -> dict[str, Environment]:
    """The demo environments with their saved versions, by file name."""
    environments: dict[str, Environment] = {}
    for demo_file in DEMO_FILES:
        service = services[demo_file.service]
        environment = await db.scalar(
            select(Environment).where(
                Environment.service_id == service.id, Environment.name == demo_file.name
            )
        )
        if environment is None:
            environment = Environment(
                service_id=service.id,
                name=demo_file.name,
                file_type=demo_file.file_type,
                target_path=demo_file.target_path,
                dist_path=demo_file.dist_path,
            )
            db.add(environment)
            await db.flush()
            for number, content in enumerate(demo_file.versions, start=1):
                await create_version(
                    db,
                    crypto,
                    environment.id,
                    content,
                    author=author,
                    comment=f"demo seed version {number}",
                )
        environments[demo_file.name] = environment
    return environments


async def _ensure_grant(
    db: AsyncSession, user_name: str, level: GrantLevel, target: Project | Environment
) -> None:
    """Give a seeded user access to one item, unless the user is missing or already has it."""
    user = await db.scalar(select(User).where(User.name == user_name))
    if user is None:
        return
    target_column = f"{level.value}_id"
    existing = await db.scalar(
        select(AccessGrant.id).where(
            AccessGrant.user_id == user.id, getattr(AccessGrant, target_column) == target.id
        )
    )
    if existing is None:
        db.add(AccessGrant(user_id=user.id, level=level, **{target_column: target.id}))


async def ensure_demo_project(
    db: AsyncSession, crypto: CryptoService, demo_root: Path
) -> SeedDemo | None:
    """The demo project, created or completed, rooted at ``<demo_root>/stack``.

    Safe to run again and on a database seeded earlier: every level is looked up before it is
    created, and an existing deployment (and its allowed path) follows a moved demo root. Does
    nothing and returns None when the ``root`` user is missing. The caller commits.
    """
    superadmin = await db.scalar(select(User).where(User.name == "root"))
    if superadmin is None:
        return None
    stack_root = str(demo_root / DEMO_STACK_FOLDER)

    project = await db.scalar(select(Project).where(Project.name == DEMO_PROJECT_NAME))
    if project is None:
        project = Project(name=DEMO_PROJECT_NAME)
        db.add(project)
        await db.flush()
    deployment = await db.scalar(
        select(Deployment).where(
            Deployment.project_id == project.id, Deployment.name == DEMO_DEPLOYMENT_NAME
        )
    )
    if deployment is None:
        deployment = Deployment(
            project_id=project.id, name=DEMO_DEPLOYMENT_NAME, root_path=stack_root
        )
        db.add(deployment)
    else:
        deployment.root_path = stack_root
    allowed_path = await db.scalar(select(AllowedPath).where(AllowedPath.path == stack_root))
    if allowed_path is None:
        allowed_path = AllowedPath(path=stack_root, created_by_id=superadmin.id)
        db.add(allowed_path)
    await db.flush()

    services = await _get_or_create_services(db, deployment)
    environments = await _get_or_create_environments(db, crypto, services, superadmin)
    await _ensure_grant(db, "project_admin", GrantLevel.PROJECT, project)
    await _ensure_grant(db, "env_scoped_admin", GrantLevel.ENVIRONMENT, environments[".env.db"])
    await db.flush()
    return SeedDemo(
        project=project,
        deployment=deployment,
        api=services["api"],
        db=services["db"],
        env_api=environments[".env.api"],
        env_ca=environments["demo-ca.pem"],
        env_db=environments[".env.db"],
        env_db_secrets=environments[".env.db.secrets"],
        allowed_path=allowed_path,
    )
