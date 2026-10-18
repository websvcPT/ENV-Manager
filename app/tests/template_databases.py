"""Template databases: built once per test session, copied for every test.

Running the migrations and the seed dominates the cost of a database test, so each test worker
builds a template once (all of them sealed with one master key) and every test works on its own
copy of the file. A test never opens a template, so nothing a test writes reaches another test.
The builders are synchronous (``asyncio.run``) because a template belongs to the session, not to
the event loop of one test.
"""

import asyncio
import shutil
from pathlib import Path
from typing import NamedTuple

from app.core.crypto import CryptoService
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.seeds import SeedData, seed
from app.services.key_check import ensure_key_check

JOURNAL_SUFFIXES = ("-wal", "-shm", "-journal")


class SeededTemplate(NamedTuple):
    """A migrated and seeded template file with the references the seed returned.

    The references are read only here: ``seed_data`` hands every test its own copy.
    """

    path: Path
    data: SeedData


def copy_database(template: Path, destination: Path) -> None:
    """Copy a template to ``destination``, which must not hold a database yet."""
    if destination.exists():
        raise RuntimeError(f"{destination} exists: a test asked for two databases at one path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, destination)


def _seal(path: Path) -> None:
    """Finish a template: require a single checkpointed file and make it read only.

    A journal left next to the file would be lost by a plain copy. Read only, a test that
    opened a template by mistake could not write to it.
    """
    leftovers = [name for suffix in JOURNAL_SUFFIXES if (name := Path(f"{path}{suffix}")).exists()]
    if leftovers:
        raise RuntimeError(f"template database {path} is not checkpointed: {leftovers}")
    path.chmod(0o444)


def build_migrated_template(path: Path) -> None:
    """Create an empty database at ``path`` migrated to the newest revision."""

    async def build() -> None:
        engine = create_engine(path)
        try:
            await migrate_database(engine, path, path.parent / "backups")
        finally:
            await engine.dispose()

    asyncio.run(build())
    _seal(path)


def build_seeded_template(migrated: Path, path: Path, crypto: CryptoService) -> SeededTemplate:
    """Copy a migrated template to ``path`` and add the seed dataset sealed with ``crypto``."""
    copy_database(migrated, path)

    async def build() -> SeedData:
        engine = create_engine(path)
        try:
            async with create_session_factory(engine)() as session:
                return await seed(session, crypto)
        finally:
            await engine.dispose()

    data = asyncio.run(build())
    _seal(path)
    return SeededTemplate(path, data)


def build_web_template(seeded: Path, path: Path, crypto: CryptoService) -> None:
    """Copy a seeded template to ``path`` and store the key check record of ``crypto``.

    That is the state the application lifespan leaves behind on a first start, so a lifespan
    started on a copy finds a database at head with the matching key.
    """
    copy_database(seeded, path)

    async def build() -> None:
        engine = create_engine(path)
        try:
            async with create_session_factory(engine)() as session:
                await ensure_key_check(session, crypto)
        finally:
            await engine.dispose()

    asyncio.run(build())
    _seal(path)
