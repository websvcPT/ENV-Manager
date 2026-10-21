"""Helpers shared by the real-scenario tests: managed contents, folder snapshots and run reads."""

import stat
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEntry, DeployRun, DeployTrigger, User
from app.real_scenarios.conftest import World
from app.seeds import DEMO_FILES
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_types import DeployOutcome, DeployRequest
from app.services.hook_runner import DockerHookRunner
from app.services.permissions import load_user_scope

MANAGED = {file.name: file.latest for file in DEMO_FILES}
BASELINE_NAMES = {
    ".env.api",
    ".env.api-dist",
    ".env.db",
    ".env.db-dist",
    ".env.db.secrets",
    ".env.db.secrets-dist",
}


def snapshot(folder: Path) -> dict[str, tuple[int, int, int, int]]:
    """Inode, size, modification time and mode of every entry of a folder."""
    entries: dict[str, tuple[int, int, int, int]] = {}
    for entry in folder.iterdir():
        info = entry.stat()
        entries[entry.name] = (
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            stat.S_IMODE(info.st_mode),
        )
    return entries


def output(result: subprocess.CompletedProcess[str]) -> str:
    """Everything a finished command printed, for failure messages."""
    return result.stdout + result.stderr


async def cli_runs(db: AsyncSession) -> list[DeployRun]:
    """The deploy runs the command line made, oldest first."""
    rows = await db.execute(
        select(DeployRun).where(DeployRun.trigger == DeployTrigger.CLI).order_by(DeployRun.id)
    )
    return list(rows.scalars())


async def deploy_audit(db: AsyncSession) -> list[AuditEntry]:
    """The audit entries of real deploys, oldest first."""
    rows = await db.execute(
        select(AuditEntry).where(AuditEntry.action == "service.deploy").order_by(AuditEntry.id)
    )
    return list(rows.scalars())


async def deploy_as(
    world: World, user_name: str, service_id: int, *, force: bool = False
) -> DeployOutcome:
    """Deploy a whole service in this process as a seeded user, with the real runner; a forced
    deploy runs a command that runs only when a file changed."""
    async with world.session() as db:
        user = await db.scalar(select(User).where(User.name == user_name))
        assert user is not None
        engine = DeployEngine(
            world.crypto, world.data_path / "locks", hook_runner=DockerHookRunner()
        )
        return await engine.run(
            db,
            await load_user_scope(db, user),
            AuditActor.for_user(user),
            DeployRequest(service_id=service_id, force=force),
        )
