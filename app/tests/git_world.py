"""The world of the git sync tests: a seeded database, a bare repository and the services."""

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.git_process import REMOTE_PROTOCOLS
from app.services.git_push import PushResult, push_if_due
from app.services.git_services import GitServices
from app.tests.git_helpers import (
    Clock,
    RecordingEvents,
    RecordingRunner,
    Sleeper,
    clone,
    configure_git,
    git_services,
    make_bare_repository,
)

SCHEDULER = AuditActor.system("scheduler")


@dataclass
class World:
    """A seeded database, a bare repository and the services that push to it.

    The runner records every command and runs real git for the local ones; a test scripts the
    commands that would need a network. The clock and the sleep are the test's own.
    """

    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    services: GitServices
    bare: Path
    clock: Clock
    sleeper: Sleeper
    events: RecordingEvents
    runner: RecordingRunner
    crypto: CryptoService
    tmp_path: Path
    seed: SeedData

    async def push(self) -> PushResult:
        """Run the push that the scheduler runs."""
        async with self.factory() as db:
            return await push_if_due(db, self.services, actor=SCHEDULER)

    async def configure(self, **changes: object) -> None:
        """Save the git settings, pointing at the bare repository unless told otherwise."""
        settings: dict[str, object] = {"url": str(self.bare)}
        settings.update(changes)
        async with self.factory() as db:
            await configure_git(db, **settings)

    def checkout(self, name: str = "check") -> Path:
        """A fresh clone of what the repository holds now."""
        return clone(self.bare, self.tmp_path / name)

    def later(self, minutes: int = 61) -> None:
        """Move the clock forward, past the wait between pushes by default."""
        self.clock.now += timedelta(minutes=minutes)


def build_git_world(
    tmp_path: Path,
    engine: AsyncEngine,
    seed: SeedData,
    crypto: CryptoService,
    database_path: Path,
) -> World:
    """A world on a temporary folder with an empty bare repository."""
    clock, sleeper, events = Clock(), Sleeper(), RecordingEvents()
    runner = RecordingRunner(scratch=tmp_path / "git")
    services = git_services(
        tmp_path, crypto, database_path, runner=runner, clock=clock, sleeper=sleeper, events=events
    )
    return World(
        engine,
        create_session_factory(engine),
        services,
        make_bare_repository(tmp_path / "remote.git"),
        clock,
        sleeper,
        events,
        runner,
        crypto,
        tmp_path,
        seed,
    )


def https_only(world: World) -> None:
    """Narrow the world's services to what production allows: ssh and https, no local path."""
    world.services = replace(world.services, protocols=REMOTE_PROTOCOLS, allow_local=False)
