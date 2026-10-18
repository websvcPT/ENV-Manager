"""Helpers shared by the deploy engine tests: fakes for the seams, harness and content reads.

The seeded deployments are moved onto a real temporary folder by ``dist_helpers`` (which also
makes that folder the only allowed path); these helpers build an engine on it and read back what
a deploy recorded.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    AuditEntry,
    Deployment,
    DeployResult,
    DeployRun,
    Environment,
    EnvironmentVersion,
    PostDeployStatus,
)
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_events import DeployFinished
from app.services.deploy_types import DeployOutcome, DeployRequest
from app.services.docker_cli import DockerCli
from app.services.hook_runner import DockerHookRunner
from app.services.hooks import PostDeployRequest, PostDeployResult
from app.services.permissions import AccessScope, load_user_scope
from app.tests.dist_helpers import link_dist, write_dist


class RecordingRunner:
    """A hook runner that records its requests and answers with a fixed result.

    ``on_run`` runs while the runner is called, which is while the engine holds the lock.
    """

    def __init__(
        self,
        result: PostDeployResult | None = None,
        on_run: Callable[[], Awaitable[None]] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Answer ``result`` (a clean run by default), call ``on_run`` first, or raise ``error``."""
        self.requests: list[PostDeployRequest] = []
        self.result = result or PostDeployResult(PostDeployStatus.RAN, exit_code=0, output="done")
        self.on_run = on_run
        self.error = error

    async def run_post_deploy(self, request: PostDeployRequest) -> PostDeployResult:
        """Record the request and answer."""
        self.requests.append(request)
        if self.on_run is not None:
            await self.on_run()
        if self.error is not None:
            raise self.error
        return self.result


class RecordingNotifier:
    """A notifier that records its events and can run a probe or fail."""

    def __init__(
        self,
        on_event: Callable[[], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Record events, call ``on_event`` for each, or raise ``error``."""
        self.events: list[DeployFinished] = []
        self.on_event = on_event
        self.error = error

    async def deploy_finished(self, event: DeployFinished) -> None:
        """Record the event."""
        self.events.append(event)
        if self.on_event is not None:
            self.on_event()
        if self.error is not None:
            raise self.error


@dataclass
class Harness:
    """An engine on a temporary lock folder with recording seams, and the seeded structure."""

    engine: DeployEngine
    runner: RecordingRunner
    notifier: RecordingNotifier
    lock_dir: Path
    roots: dict[str, Path]
    seed: SeedData
    db: AsyncSession
    database: AsyncEngine
    crypto: CryptoService
    scopes: dict[str, AccessScope] = field(default_factory=dict)

    async def scope_of(self, name: str) -> AccessScope:
        """The scope of a seeded user by field name of ``SeedData.users``."""
        if name not in self.scopes:
            self.scopes[name] = await load_user_scope(self.db, getattr(self.seed.users, name))
        return self.scopes[name]

    async def deploy(
        self,
        service_id: int,
        *,
        environments: list[Environment] | None = None,
        versions: dict[int, int] | None = None,
        dry_run: bool = False,
        force: bool = False,
        user: str = "admin",
    ) -> DeployOutcome:
        """Deploy a service as a seeded user (the seeded admin by default)."""
        request = DeployRequest(
            service_id=service_id,
            environment_ids=(
                tuple(environment.id for environment in environments)
                if environments is not None
                else None
            ),
            versions=versions or {},
            dry_run=dry_run,
            force=force,
        )
        return await self.engine.run(
            self.db,
            await self.scope_of(user),
            AuditActor.for_user(getattr(self.seed.users, user)),
            request,
        )


async def version_text(
    db: AsyncSession, crypto: CryptoService, environment_id: int, number: int | None = None
) -> str:
    """The decrypted content of a version of an environment, the newest by default."""
    stmt = select(EnvironmentVersion).where(EnvironmentVersion.environment_id == environment_id)
    stmt = (
        stmt.where(EnvironmentVersion.number == number)
        if number is not None
        else stmt.order_by(EnvironmentVersion.number.desc()).limit(1)
    )
    version = (await db.execute(stmt)).scalar_one()
    return crypto.decrypt_text(version.content_encrypted)


async def runs(db: AsyncSession) -> list[DeployRun]:
    """Every deploy run, oldest first."""
    return list((await db.execute(select(DeployRun).order_by(DeployRun.id))).scalars())


async def results_of(db: AsyncSession, run_id: int) -> dict[str, DeployResult]:
    """The results of a run by environment name."""
    rows = await db.execute(select(DeployResult).where(DeployResult.run_id == run_id))
    return {result.environment_name: result for result in rows.scalars()}


async def audit_of(db: AsyncSession, action: str) -> list[AuditEntry]:
    """The audit entries of an action, oldest first."""
    rows = await db.execute(
        select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
    )
    return list(rows.scalars())


async def set_deployment(db: AsyncSession, deployment_id: int, **fields: object) -> None:
    """Change fields of a deployment, for example a policy."""
    deployment = await db.get(Deployment, deployment_id)
    assert deployment is not None
    for name, value in fields.items():
        setattr(deployment, name, value)
    await db.commit()


async def set_environment(db: AsyncSession, environment_id: int, **fields: object) -> None:
    """Change fields of an environment, for example a policy override or a mode."""
    environment = await db.get(Environment, environment_id)
    assert environment is not None
    for name, value in fields.items():
        setattr(environment, name, value)
    await db.commit()


def files_in(folder: Path) -> list[str]:
    """The names of the entries of a folder, sorted."""
    return sorted(entry.name for entry in folder.iterdir())


TUNING_DIST = "environments/.env.web.tuning-dist"


async def require_extra_key(h: Harness) -> Environment:
    """Link the staging tuning file to a dist template with a key the file lacks and return it."""
    tuning = h.seed.figures.tuning
    write_dist(h.roots["staging"], TUNING_DIST, "WORKERS=\nEXTRA_KEY=\n")
    await link_dist(h.db, tuning, TUNING_DIST)
    return tuning


def docker_less_runner(folder: Path) -> DockerHookRunner:
    """The real runner on a socket path that does not exist: it skips any stored command."""
    return DockerHookRunner(DockerCli(socket_path=folder / "no-docker.sock"))
