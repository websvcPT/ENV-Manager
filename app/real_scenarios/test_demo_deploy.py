"""Deploys of the demo stack through the real command line, onto the mounted host folder.

Each scenario starts from the baseline target files (``.env.api`` drifted, ``.env.db`` and
``.env.db.secrets`` identical, ``demo-ca.pem`` missing) and checks the result on the real files:
mode, owner, content, and what was not touched.
"""

import os
import stat
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditEntry,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployTrigger,
    DriftPolicy,
    Environment,
    User,
)
from app.real_scenarios.conftest import DemoStack, World
from app.seeds import DEMO_FILES
from app.services.environments import EnvironmentForm, create_environment, validate_environment
from app.services.paths import load_allowed_paths

MANAGED = {file.name: file.latest for file in DEMO_FILES}
BASELINE_NAMES = {
    ".env.api",
    ".env.api-dist",
    ".env.db",
    ".env.db-dist",
    ".env.db.secrets",
    ".env.db.secrets-dist",
    "demo-ca.pem-dist",
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


async def test_a_deploy_writes_the_file_with_mode_0600_and_the_managed_content(
    demo: DemoStack, world: World
) -> None:
    (demo.targets / ".env.api").unlink()
    result = world.deploy("api", "--env", ".env.api")
    assert result.returncode == 0, output(result)
    assert ".env.api (v1): written. Created." in result.stdout

    target = demo.targets / ".env.api"
    info = target.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid()
    assert target.read_text(encoding="utf-8") == MANAGED[".env.api"]
    assert {entry.name for entry in demo.targets.iterdir()} == BASELINE_NAMES

    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert (run.dry_run, run.force) == (False, False)
        rows = await db.execute(select(DeployResult).where(DeployResult.run_id == run.id))
        (written,) = rows.scalars()
        assert written.status is DeployResultStatus.WRITTEN
        assert written.written_hash == world.crypto.content_hash(MANAGED[".env.api"])


async def test_a_drifted_target_halts_by_default_and_force_overwrites_it(
    demo: DemoStack, world: World
) -> None:
    drifted = (demo.targets / ".env.api").read_text(encoding="utf-8")
    assert drifted != MANAGED[".env.api"]
    halted = world.deploy("api", "--env", ".env.api")
    assert halted.returncode == 1, output(halted)
    assert "halted" in halted.stdout and "changed outside the app" in halted.stdout
    assert (demo.targets / ".env.api").read_text(encoding="utf-8") == drifted
    assert {entry.name for entry in demo.targets.iterdir()} == BASELINE_NAMES

    forced = world.deploy("api", "--env", ".env.api", "--force")
    assert forced.returncode == 0, output(forced)
    target = demo.targets / ".env.api"
    assert target.read_text(encoding="utf-8") == MANAGED[".env.api"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert {entry.name for entry in demo.targets.iterdir()} == BASELINE_NAMES

    async with world.session() as db:
        assert [run.force for run in await cli_runs(db)] == [False, True]
        entries = await deploy_audit(db)
        assert [entry.result.value for entry in entries] == ["halted", "success"]
        assert [entry.details["force"] for entry in entries] == [False, True]
        assert {entry.actor_name for entry in entries} == {"cli"}


async def test_a_missing_certificate_is_created_and_nothing_else_is_touched(
    demo: DemoStack, world: World
) -> None:
    before = snapshot(demo.targets)
    result = world.deploy("api", "--env", "demo-ca.pem")
    assert result.returncode == 0, output(result)
    assert "demo-ca.pem (v1): written. Created." in result.stdout

    created = demo.targets / "demo-ca.pem"
    assert created.read_text(encoding="utf-8") == MANAGED["demo-ca.pem"]
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    after = snapshot(demo.targets)
    assert {name: value for name, value in after.items() if name != "demo-ca.pem"} == before


async def test_a_dry_run_writes_nothing(demo: DemoStack, world: World) -> None:
    before = snapshot(demo.targets)
    only_certificate = world.deploy("api", "--env", "demo-ca.pem", "--dry-run")
    assert only_certificate.returncode == 0, output(only_certificate)
    assert "dry run of demo/stack/api: success" in only_certificate.stdout
    assert "would_write" in only_certificate.stdout
    whole_service = world.deploy("api", "--dry-run")
    assert whole_service.returncode == 1, output(whole_service)
    assert ".env.api (v1): halted." in whole_service.stdout
    assert "demo-ca.pem (v1): would_write." in whole_service.stdout
    assert snapshot(demo.targets) == before

    async with world.session() as db:
        assert [run.dry_run for run in await cli_runs(db)] == [True, True]
        actions = {entry.action for entry in (await db.execute(select(AuditEntry))).scalars()}
        assert "service.deploy_dry_run" in actions
        assert await deploy_audit(db) == []


async def test_identical_files_are_unchanged_and_a_missing_dist_key_still_blocks(
    demo: DemoStack, world: World
) -> None:
    before = snapshot(demo.targets)
    unchanged = world.deploy("db", "--env", ".env.db")
    assert unchanged.returncode == 0, output(unchanged)
    assert ".env.db (v2): unchanged." in unchanged.stdout
    assert snapshot(demo.targets) == before

    whole_service = world.deploy("db")
    assert whole_service.returncode == 1, output(whole_service)
    assert ".env.db (v2): unchanged." in whole_service.stdout
    assert ".env.db.secrets (v1): blocked. Blocked: 1 dist key is missing" in whole_service.stdout
    assert snapshot(demo.targets) == before


async def test_the_backup_policy_keeps_a_private_copy_of_the_drifted_file(
    demo: DemoStack, world: World
) -> None:
    drifted = (demo.targets / ".env.api").read_text(encoding="utf-8")
    async with world.session() as db:
        deployment = await db.get(Deployment, world.demo_seed.deployment.id)
        assert deployment is not None
        deployment.drift_policy = DriftPolicy.BACKUP_OVERWRITE
        await db.commit()
    result = world.deploy("api", "--env", ".env.api")
    assert result.returncode == 0, output(result)
    assert "The previous file was backed up." in result.stdout

    backups = [entry for entry in demo.targets.iterdir() if ".bak-" in entry.name]
    assert [entry.name.startswith(".env.api.bak-") for entry in backups] == [True]
    assert backups[0].read_text(encoding="utf-8") == drifted
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert (demo.targets / ".env.api").read_text(encoding="utf-8") == MANAGED[".env.api"]


async def test_an_owner_the_container_user_cannot_set_fails_the_file_and_changes_nothing(
    demo: DemoStack, world: World
) -> None:
    assert os.geteuid() != 0, "the app container must run as a non-root user"
    (demo.targets / ".env.api").unlink()
    async with world.session() as db:
        environment = await db.get(Environment, world.demo_seed.env_api.id)
        assert environment is not None
        environment.file_owner = "root"
        await db.commit()
    before = snapshot(demo.targets)
    result = world.deploy("api", "--env", ".env.api")
    assert result.returncode == 1, output(result)
    assert ".env.api (v1): failed." in result.stdout
    assert "no privilege to change ownership" in result.stdout
    assert snapshot(demo.targets) == before
    assert not (demo.targets / ".env.api").exists()


async def test_a_symlink_in_place_of_the_target_is_not_followed(
    demo: DemoStack, world: World, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("UNTOUCHED\n", encoding="utf-8")
    (demo.targets / ".env.api").unlink()
    (demo.targets / ".env.api").symlink_to(victim)
    result = world.deploy("api", "--env", ".env.api", "--force")
    assert result.returncode == 1, output(result)
    assert "symbolic link" in result.stdout
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"
    assert (demo.targets / ".env.api").is_symlink()


async def test_an_imported_target_deploys_as_unchanged(demo: DemoStack, world: World) -> None:
    content = "# Imported demo file. Fake value.\nDEMO_IMPORTED=yes\n"
    imported = demo.targets / ".env.imported"
    imported.write_text(content, encoding="utf-8")
    imported.chmod(0o600)
    async with world.session() as db:
        deployment = await db.get(Deployment, world.demo_seed.deployment.id)
        author = await db.scalar(select(User).where(User.name == "root"))
        assert deployment is not None and author is not None
        form = EnvironmentForm(
            service=str(world.demo_seed.api.id),
            name=".env.imported",
            target_path="environments/.env.imported",
            source="import",
        )
        clean = await validate_environment(db, await load_allowed_paths(db), deployment, form)
        assert clean.content == content
        await create_environment(db, world.crypto, deployment, clean, actor=author)
    before = snapshot(demo.targets)
    result = world.deploy("api", "--env", ".env.imported")
    assert result.returncode == 0, output(result)
    assert ".env.imported (v1): unchanged." in result.stdout
    assert snapshot(demo.targets) == before
