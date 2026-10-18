"""Post-deploy commands of the demo stack against the host Docker daemon.

The real command line (and, for a scoped user, the engine in this process) deploys the demo
services; the stored command recreates the workload through the mounted Docker socket. The
scenarios read what the daemon did: which container was created how many times and which key
names the recreated container printed (never their values), and what the run and the audit entry
recorded. Every scenario starts without demo containers and removes them again.
"""

import os
import time
from pathlib import Path

from sqlalchemy import select

from app.models import (
    AuditEntry,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    PostDeployStatus,
    User,
)
from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.helpers import MANAGED, cli_runs, output, snapshot
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_lock import hold_deploy_lock
from app.services.deploy_types import DeployOutcome, DeployRequest
from app.services.hook_runner import NOT_CHANGED_REASON, DockerHookRunner
from app.services.permissions import load_user_scope

API_NAMES = {"DEMO_API_MODE", "DEMO_API_PORT", "DEMO_API_TOKEN"}
NEW_API_KEYS = "DEMO_API_NEW_FLAG=on\nDEMO_API_NEW_LIMIT=9\n"
DB_NAMES = {"DEMO_DB_ENGINE", "DEMO_DB_PORT", "DEMO_DB_POOL"}
SECRET_NAMES = {"DEMO_DB_USER", "DEMO_DB_PASSWORD"}
SECRET_VALUE = "demo-password-not-a-secret"
ALPINE = "alpine:3.24.2"


def processes_running(argument: str) -> list[str]:
    """Command lines of live docker processes that were started with the argument.

    Zombies have no command line and are not counted: their parent has not reaped them, they
    are not running.
    """
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            words = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        text = [word.decode("utf-8", errors="replace") for word in words if word]
        if argument in text and any(Path(word).name.startswith("docker") for word in text):
            found.append(" ".join(text))
    return found


async def deploy_as(world: World, user_name: str, service_id: int) -> DeployOutcome:
    """Deploy a whole service in this process as a seeded user, with the real runner."""
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
            DeployRequest(service_id=service_id),
        )


async def test_a_changed_managed_version_recreates_the_api_container_once_with_the_new_key_names(
    demo: DemoStack, world: World, workloads: Workloads
) -> None:
    (demo.targets / ".env.api").unlink()
    first = world.deploy("api", "--env", ".env.api")
    assert first.returncode == 0, output(first)
    assert "post-deploy: ran" in first.stdout and "post-deploy exit code: 0" in first.stdout
    (first_container,) = workloads.container_ids("api")
    workloads.wait_for_names("api", API_NAMES)

    changed = MANAGED[".env.api"] + NEW_API_KEYS
    await world.save_version(world.demo_seed.env_api.id, changed)
    before = time.time()
    second = world.deploy("api", "--env", ".env.api")
    assert second.returncode == 0, output(second)
    assert ".env.api (v2): written." in second.stdout and "post-deploy: ran" in second.stdout
    assert (demo.targets / ".env.api").read_text(encoding="utf-8") == changed

    (second_container,) = workloads.container_ids("api")
    assert second_container != first_container
    assert workloads.created_since("api", before) == 1
    logs = workloads.wait_for_names("api", {*API_NAMES, "DEMO_API_NEW_FLAG", "DEMO_API_NEW_LIMIT"})
    assert "=" not in logs and "demo-token-not-a-secret" not in logs
    assert not any(".bak-" in entry.name for entry in demo.targets.iterdir())

    async with world.session() as db:
        runs = await cli_runs(db)
        assert [(run.post_deploy_status, run.post_deploy_exit_code) for run in runs] == [
            (PostDeployStatus.RAN, 0),
            (PostDeployStatus.RAN, 0),
        ]
        assert runs[1].post_deploy_output is not None and "Started" in runs[1].post_deploy_output
        entries = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "service.deploy"))
        ).scalars()
        details = [entry.details for entry in entries]
        assert [item["post_deploy"] for item in details] == ["ran", "ran"]
        assert all("force-recreate" not in str(item) for item in details)


async def test_a_user_granted_only_env_db_deploys_db_which_writes_only_that_file_and_runs_once(
    demo: DemoStack, world: World, workloads: Workloads
) -> None:
    baseline = world.deploy("db", "--env", ".env.db")
    assert baseline.returncode == 0, output(baseline)
    assert ".env.db (v2): unchanged." in baseline.stdout
    assert f"post-deploy: skipped ({NOT_CHANGED_REASON})" in baseline.stdout
    assert workloads.container_ids("db") == []

    changed = MANAGED[".env.db"] + "DEMO_DB_CACHE=on\n"
    await world.save_version(world.demo_seed.env_db.id, changed)
    secrets_before = snapshot(demo.targets)[".env.db.secrets"]
    before = time.time()
    outcome = await deploy_as(world, "env_scoped_admin", world.demo_seed.db.id)

    assert outcome.status is DeployStatus.SUCCESS
    assert [(env.name, env.status) for env in outcome.environments] == [
        (".env.db", DeployResultStatus.WRITTEN)
    ]
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    assert (demo.targets / ".env.db").read_text(encoding="utf-8") == changed
    assert snapshot(demo.targets)[".env.db.secrets"] == secrets_before
    assert workloads.created_since("db", before) == 1
    logs = workloads.wait_for_names("db", {*DB_NAMES, "DEMO_DB_CACHE", *SECRET_NAMES})
    assert "=" not in logs and SECRET_VALUE not in logs

    async with world.session() as db:
        scoped_run = (
            await db.execute(select(DeployRun).order_by(DeployRun.id.desc()).limit(1))
        ).scalar_one()
        assert scoped_run.post_deploy_status is PostDeployStatus.RAN
        assert SECRET_VALUE not in str(scoped_run.post_deploy_output)
        entry = await db.get(AuditEntry, scoped_run.audit_entry_id)
        assert entry is not None and entry.actor_name == "env_scoped_admin"
        assert SECRET_VALUE not in str(entry.details)

    again = await deploy_as(world, "env_scoped_admin", world.demo_seed.db.id)
    assert again.status is DeployStatus.SUCCESS
    assert again.post_deploy.status is PostDeployStatus.SKIPPED
    assert again.post_deploy.reason == NOT_CHANGED_REASON
    assert workloads.created_since("db", before) == 1


async def test_a_failing_command_is_reported_as_post_deploy_failed_with_exit_code_and_output(
    demo: DemoStack, world: World, workloads: Workloads
) -> None:
    await world.change_hook(
        world.demo_seed.db.id,
        commands="docker compose config --quiet\ndocker inspect env-manager-demo-no-such-container",
        run_only_if_changed=False,
    )
    before = snapshot(demo.targets)
    result = world.deploy("db", "--env", ".env.db")

    assert result.returncode == 1, output(result)
    lines = result.stdout.splitlines()
    assert lines[0].startswith("deploy of demo/stack/db: post_deploy_failed (run ")
    assert "  .env.db (v2): unchanged. The target already matches this version." in lines
    assert "  post-deploy: failed (Command 2 of 2 exited with code 1.)" in lines
    assert "  post-deploy exit code: 1" in lines
    assert "    [command 2 of 2]" in lines and "no such object" in result.stdout.lower()
    assert snapshot(demo.targets) == before

    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert run.status is DeployStatus.POST_DEPLOY_FAILED
        assert (run.post_deploy_status, run.post_deploy_exit_code) == (PostDeployStatus.FAILED, 1)
        assert run.post_deploy_output is not None
        assert "no such object" in run.post_deploy_output.lower()
        entry = await db.get(AuditEntry, run.audit_entry_id)
        assert entry is not None and entry.result.value == "failure"
        assert entry.details["post_deploy_exit_code"] == 1
        assert "inspect" not in str(entry.details)


async def test_a_command_with_a_lot_of_output_is_stored_cut_to_the_first_and_last_four_kilobytes(
    world: World,
) -> None:
    await world.change_hook(
        world.demo_seed.db.id,
        commands=f'docker run --rm {ALPINE} sh -c "seq 1 5000; exit 3"',
        run_only_if_changed=False,
    )
    result = world.deploy("db", "--env", ".env.db")
    assert result.returncode == 1, output(result)
    assert "post-deploy: failed (The command exited with code 3.)" in result.stdout

    async with world.session() as db:
        (run,) = await cli_runs(db)
        stored = run.post_deploy_output
        assert run.post_deploy_exit_code == 3 and stored is not None
        assert stored.startswith("1\n2\n3\n") and stored.endswith("4999\n5000\n")
        assert "bytes omitted" in stored and len(stored) < 8400
        head = stored.split("[...", 1)[0]
        assert "\n100\n" in f"\n{head}" and "2500\n" not in stored


async def test_a_timeout_kills_the_command_and_everything_it_started(
    demo: DemoStack, world: World, workloads: Workloads
) -> None:
    await world.change_hook(
        world.demo_seed.db.id,
        commands="docker compose events",
        timeout_seconds=3,
        run_only_if_changed=False,
    )
    started = time.monotonic()
    result = world.deploy("db", "--env", ".env.db")
    elapsed = time.monotonic() - started

    assert result.returncode == 1, output(result)
    assert "post-deploy: failed (Timed out after 3 seconds and was stopped.)" in result.stdout
    assert "post-deploy exit code" not in result.stdout
    assert elapsed < 60
    deadline = time.monotonic() + 5
    while processes_running("events") and time.monotonic() < deadline:
        time.sleep(0.2)
    assert processes_running("events") == []
    with hold_deploy_lock(world.data_path / "locks", str(demo.root)):
        pass

    async with world.session() as db:
        (run,) = await cli_runs(db)
        assert run.status is DeployStatus.POST_DEPLOY_FAILED
        assert run.post_deploy_exit_code is None and run.finished_at is not None
        assert run.post_deploy_reason == "Timed out after 3 seconds and was stopped."


async def test_without_the_docker_socket_the_command_is_skipped_and_the_deploy_succeeds(
    demo: DemoStack, world: World, tmp_path: Path, workloads: Workloads
) -> None:
    (demo.targets / ".env.api").unlink()
    result = world.deploy(
        "api",
        "--env",
        ".env.api",
        environment={"ENVMANAGER_DOCKER_SOCKET": str(tmp_path / "missing.sock")},
    )
    assert result.returncode == 0, output(result)
    assert "deploy of demo/stack/api: success" in result.stdout
    assert "post-deploy: skipped (The Docker socket is not mounted.)" in result.stdout
    assert (demo.targets / ".env.api").read_text(encoding="utf-8") == MANAGED[".env.api"]
    assert workloads.container_ids() == []
    assert os.path.exists("/var/run/docker.sock")
