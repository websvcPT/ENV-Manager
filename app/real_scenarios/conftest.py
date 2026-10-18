"""Fixtures of the real-scenario tests: the demo folder at its host path, a seeded database and
the real command line."""

import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.seeds import SeedData, SeedDemo, seed

DEMO_ROOT_VARIABLE = "ENVMANAGER_DEMO_ROOT"
DIST_SUFFIX = "-dist"


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

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the CLI in a child process on this world's data folder and key."""
        environment = {
            **os.environ,
            "ENVMANAGER_DATA_PATH": str(self.data_path),
            "ENVMANAGER_KEY": self.key,
        }
        environment.pop("ENVMANAGER_KEY_FILE", None)
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "app.cli", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def deploy(self, service: str, *options: str) -> subprocess.CompletedProcess[str]:
        """Run ``envmanager deploy demo/stack/<service>`` with the options."""
        return self.command("deploy", f"demo/stack/{service}", *options)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session on the world's database."""
        engine = create_engine(self.data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                yield db
        finally:
            await engine.dispose()


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
