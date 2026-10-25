"""Fixtures of the real-scenario tests: the demo folder at its host path, a seeded database and
the real command line."""

import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.main import create_app
from app.models import HookType, ServiceHook, User
from app.real_scenarios.registry import LocalRegistry, remove_local_image, start_registry
from app.seeds import SeedData, SeedDemo, seed
from app.services.versions import create_version

SOURCE_IMAGE = "alpine:3.24.2"
PRIVATE_REPOSITORY = "private/alpine"
PRIVATE_TAG = "1"
DEMO_ROOT_VARIABLE = "ENVMANAGER_DEMO_ROOT"
DIST_SUFFIX = "-dist"
COMPOSE_PROJECT = "env-manager-demo"
PROJECT_LABEL = f"label=com.docker.compose.project={COMPOSE_PROJECT}"
LOG_WAIT_SECONDS = 30.0


@dataclass(frozen=True)
class DemoStack:
    """The demo folder as the app container sees it, at the same path as on the host."""

    base: Path

    @property
    def root(self) -> Path:
        """The deployment root of the demo stack."""
        return self.base / "stack"

    @property
    def targets(self) -> Path:
        """The folder with the real target files and the committed dist templates."""
        return self.root / "environments"

    @property
    def baseline(self) -> Path:
        """The known target state that ``make demo-reset`` restores."""
        return self.base / "baseline" / "environments"


def restore_baseline(stack: DemoStack) -> None:
    """Put the target files back to the baseline: remove every non-template file, copy the baseline.

    This is what ``make demo-reset`` does; files are written with mode 0600.
    """
    for entry in list(stack.targets.iterdir()):
        if not entry.name.endswith(DIST_SUFFIX):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for source in stack.baseline.iterdir():
        descriptor = os.open(stack.targets / source.name, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source.read_bytes())


@pytest.fixture(scope="session")
def demo() -> DemoStack:
    """The demo stack; the tests refuse to run without the demo override."""
    value = os.environ.get(DEMO_ROOT_VARIABLE)
    if not value:
        pytest.fail(f"{DEMO_ROOT_VARIABLE} is not set: run these tests through make e2e-demo")
    stack = DemoStack(Path(value))
    if not stack.targets.is_dir() or not os.access(stack.targets, os.W_OK):
        pytest.fail("the demo folder is not mounted read-write at its host path")
    return stack


@pytest.fixture(autouse=True)
def at_baseline(demo: DemoStack) -> Iterator[None]:
    """Every scenario starts from the baseline and leaves the demo folder at it."""
    restore_baseline(demo)
    yield
    restore_baseline(demo)


@dataclass
class World:
    """A seeded database in a temporary data folder and the real ``envmanager`` command on it."""

    data_path: Path
    key: str
    seed: SeedData
    crypto: CryptoService

    @property
    def demo_seed(self) -> SeedDemo:
        """The demo project rows of the seeded database."""
        assert self.seed.demo is not None
        return self.seed.demo

    def command(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the CLI in a child process on this world's data folder and key.

        ``environment`` adds or replaces variables of the child process.
        """
        child_environment = {
            **os.environ,
            "ENVMANAGER_DATA_PATH": str(self.data_path),
            "ENVMANAGER_KEY": self.key,
            **(environment or {}),
        }
        child_environment.pop("ENVMANAGER_KEY_FILE", None)
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "app.cli", *arguments],
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def deploy(
        self, service: str, *options: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run ``envmanager deploy demo/stack/<service>`` with the options."""
        return self.command("deploy", f"demo/stack/{service}", *options, environment=environment)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session on the world's database."""
        engine = create_engine(self.data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                yield db
        finally:
            await engine.dispose()

    async def save_version(self, environment_id: int, content: str) -> None:
        """Save content as the next version of an environment, as the superadmin."""
        async with self.session() as db:
            author = await db.scalar(select(User).where(User.name == "root"))
            await create_version(
                db, self.crypto, environment_id, content, author=author, comment="scenario"
            )
            await db.commit()

    async def change_hook(self, service_id: int, **fields: object) -> None:
        """Change fields of the stored post-deploy command of a service."""
        async with self.session() as db:
            hook = await db.scalar(
                select(ServiceHook).where(
                    ServiceHook.service_id == service_id,
                    ServiceHook.type == HookType.POST_DEPLOY,
                )
            )
            assert hook is not None
            for name, value in fields.items():
                setattr(hook, name, value)
            await db.commit()


@pytest.fixture
async def world(demo: DemoStack, tmp_path: Path) -> World:
    """A fresh database with the dataset and the demo project rooted at the mounted folder."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    key = generate_master_key()
    crypto = CryptoService(parse_master_key(key))
    engine = create_engine(data_path / "envmanager.db")
    try:
        await migrate_database(engine, data_path / "envmanager.db", data_path / "backups")
        async with create_session_factory(engine)() as db:
            seeded = await seed(db, crypto, demo_root=demo.base)
    finally:
        await engine.dispose()
    return World(data_path, key, seeded, crypto)


@pytest.fixture
async def web(world: World, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """The application on the world's data folder and key, reached in process."""
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(world.data_path))
    monkeypatch.setenv("ENVMANAGER_KEY", world.key)
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    get_settings.cache_clear()
    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"Accept": "text/html"}
        ) as http:
            yield http
    get_settings.cache_clear()


@dataclass(frozen=True)
class Workloads:
    """The demo containers on the host daemon, read through the docker command."""

    root: Path

    @staticmethod
    def docker(*arguments: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        """Run the docker command with the environment of this test process."""
        return subprocess.run(  # noqa: S603
            ["docker", *arguments],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def container_ids(self, service: str | None = None) -> list[str]:
        """Ids of the demo containers (all of them, or those of one service), running or not."""
        filters = ["--filter", PROJECT_LABEL]
        if service:
            filters += ["--filter", f"label=com.docker.compose.service={service}"]
        return self.docker("ps", "--all", "--quiet", *filters).stdout.split()

    def created_since(self, service: str, since: float) -> int:
        """How many containers of the service were created after ``since`` (epoch seconds)."""
        result = self.docker(
            "events",
            "--since",
            f"{since:.3f}",
            "--until",
            f"{time.time():.3f}",
            "--filter",
            "event=create",
            "--filter",
            PROJECT_LABEL,
            "--filter",
            f"label=com.docker.compose.service={service}",
        )
        return len(result.stdout.splitlines())

    def wait_for_names(self, service: str, names: set[str]) -> str:
        """The logs of the service's container once every name is listed in them, else fail."""
        (container,) = self.container_ids(service)
        deadline = time.monotonic() + LOG_WAIT_SECONDS
        logs = ""
        while time.monotonic() < deadline:
            logs = self.docker("logs", container).stdout
            if names <= set(logs.split()):
                return logs
            time.sleep(0.5)
        pytest.fail(f"the logs of {service} never listed {sorted(names)}: {logs!r}")

    def stop(self) -> None:
        """Remove the demo containers and network when there are any."""
        if self.container_ids():
            self.docker("compose", "-p", COMPOSE_PROJECT, "down", "--remove-orphans")


@pytest.fixture(autouse=True)
def workloads(demo: DemoStack) -> Iterator[Workloads]:
    """The demo containers; every scenario starts without them and leaves none behind."""
    found = Workloads(demo.root)
    found.stop()
    yield found
    found.stop()


@pytest.fixture(scope="session")
def local_registry() -> Iterator[LocalRegistry]:
    """A private registry with the test login on 127.0.0.1, started once and always removed."""
    registry = start_registry()
    try:
        yield registry
    finally:
        registry.remove()


@pytest.fixture
def private_image(local_registry: LocalRegistry) -> Iterator[str]:
    """The reference of a private image in the registry, with no copy on the host daemon.

    The image is the pinned alpine pushed under a name of the registry and untagged locally, so a
    pull has to authenticate against the registry. Whatever a scenario pulled is removed again.
    """
    reference = local_registry.push(SOURCE_IMAGE, PRIVATE_REPOSITORY, PRIVATE_TAG)
    try:
        yield reference
    finally:
        remove_local_image(reference)
