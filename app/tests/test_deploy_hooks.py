"""The deploy engine with the real post-deploy runner on a fake docker executable."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import create_session_factory
from app.models import (
    AuditResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DockerRegistry,
    HookType,
    PostDeployStatus,
    ServiceHook,
)
from app.services.deploy import REGISTRY_UNREADABLE_REASON, DeployEngine
from app.services.deploy_lock import DeployLockBusyError, hold_deploy_lock
from app.services.deploy_recovery import INTERRUPTED_REASON
from app.services.deploy_types import DeployOutcome, DeployRefusal
from app.services.docker_cli import NOT_MOUNTED_REASON
from app.services.hook_runner import NOT_CHANGED_REASON, DockerHookRunner
from app.services.registries import RegistryLogin, add_registry, registry_context
from app.services.service_detail import (
    load_post_deploy,
    load_service_header,
    load_service_history,
)
from app.tests.deploy_helpers import (
    Harness,
    audit_of,
    require_extra_key,
    set_environment,
)
from app.tests.fake_docker import FakeDocker
from app.tests.grant_helpers import count_statements
from app.tests.test_hook_process import wait_until_gone
from app.tests.test_hook_runner import TOKEN, credentials
from app.tests.test_post_deploy_switch import switch


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker whose commands print a fixed line and succeed."""
    docker = FakeDocker(tmp_path / "fake")
    docker.behavior('echo "container recreated"')
    return docker


@pytest.fixture
async def hooked(harness: Harness, fake: FakeDocker) -> Harness:
    """The harness with the real runner on the fake docker instead of the recording runner."""
    harness.engine = DeployEngine(
        harness.crypto,
        harness.lock_dir,
        hook_runner=DockerHookRunner(fake.cli()),
        notifier=harness.notifier,
    )
    return harness


async def deploy_staging(
    h: Harness, *, dry_run: bool = False, force: bool = False, only_tuning: bool = False
) -> DeployOutcome:
    """Deploy the staging web service, or only its tuning file."""
    return await h.deploy(
        h.seed.services.staging_web.id,
        environments=[h.seed.figures.tuning] if only_tuning else None,
        dry_run=dry_run,
        force=force,
    )


async def change_hook(h: Harness, **fields: object) -> None:
    """Change the stored post-deploy command of the staging web service."""
    hook = await h.db.scalar(
        select(ServiceHook).where(
            ServiceHook.service_id == h.seed.services.staging_web.id,
            ServiceHook.type == HookType.POST_DEPLOY,
        )
    )
    assert hook is not None
    for name, value in fields.items():
        setattr(hook, name, value)
    await h.db.commit()


async def stored_run(h: Harness, outcome: DeployOutcome) -> DeployRun:
    """The run row of an outcome, read fresh from the database."""
    await h.db.rollback()
    run = await h.db.get(DeployRun, outcome.run_id, populate_existing=True)
    assert run is not None
    return run


async def test_a_deploy_runs_the_stored_command_once_and_records_the_result(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.SUCCESS
    assert outcome.post_deploy.status is PostDeployStatus.RAN

    (call,) = fake.calls()
    assert call.arguments == ("compose", "up", "-d", "--no-deps", "web")
    assert call.working_directory == str(h.roots["staging"])
    run = await stored_run(h, outcome)
    assert (run.post_deploy_status, run.post_deploy_exit_code) == (PostDeployStatus.RAN, 0)
    assert run.post_deploy_output == "container recreated\n" and run.finished_at is not None
    (event,) = h.notifier.events
    assert event.post_deploy is PostDeployStatus.RAN and event.status is DeployStatus.SUCCESS


async def test_the_audit_entry_holds_exit_code_and_output_but_never_the_command(
    hooked: Harness,
) -> None:
    h = hooked
    await deploy_staging(h)
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["post_deploy"] == "ran"
    assert entry.details["post_deploy_exit_code"] == 0
    assert entry.details["post_deploy_output"] == "container recreated\n"
    assert "compose up" not in json.dumps(entry.details)


async def test_a_failing_command_makes_the_deploy_deployed_with_post_deploy_failed(
    hooked: Harness, fake: FakeDocker, caplog: pytest.LogCaptureFixture
) -> None:
    h = hooked
    fake.behavior("echo broken >&2; exit 3")
    caplog.set_level(logging.DEBUG)
    outcome = await deploy_staging(h)

    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert outcome.post_deploy.status is PostDeployStatus.FAILED
    assert {result.status.value for result in outcome.environments} == {"written"}
    run = await stored_run(h, outcome)
    assert run.status is DeployStatus.POST_DEPLOY_FAILED
    assert (run.post_deploy_exit_code, run.post_deploy_output) == (3, "broken\n")
    assert run.post_deploy_reason == "The command exited with code 3."
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.result is AuditResult.FAILURE
    assert entry.details["post_deploy_exit_code"] == 3
    assert entry.details["post_deploy_output"] == "broken\n"
    (event,) = h.notifier.events
    assert event.status is DeployStatus.POST_DEPLOY_FAILED
    assert event.post_deploy is PostDeployStatus.FAILED
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_a_timeout_fails_the_post_deploy_and_stops_the_process_group(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    await change_hook(h, timeout_seconds=1)
    pid_file = fake.folder / "child.pid"
    fake.behavior(f"sleep 300 & echo $! > {pid_file}; wait")
    started = time.monotonic()
    outcome = await deploy_staging(h)
    assert time.monotonic() - started < 20
    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    run = await stored_run(h, outcome)
    assert run.post_deploy_exit_code is None
    assert run.post_deploy_reason == "Timed out after 1 seconds and was stopped."
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))
    with hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        pass


async def test_a_deploy_where_nothing_changed_skips_the_command_unless_forced(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    assert (await deploy_staging(h)).post_deploy.status is PostDeployStatus.RAN
    again = await deploy_staging(h)
    assert again.status is DeployStatus.SUCCESS
    assert again.post_deploy.status is PostDeployStatus.SKIPPED
    assert again.post_deploy.reason == NOT_CHANGED_REASON
    run = await stored_run(h, again)
    assert (run.post_deploy_status, run.post_deploy_reason) == (
        PostDeployStatus.SKIPPED,
        NOT_CHANGED_REASON,
    )
    assert len(fake.calls()) == 1

    forced = await deploy_staging(h, force=True)
    assert forced.post_deploy.status is PostDeployStatus.RAN
    await change_hook(h, run_only_if_changed=False)
    assert (await deploy_staging(h)).post_deploy.status is PostDeployStatus.RAN
    assert len(fake.calls()) == 3


@pytest.mark.parametrize("situation", ["dry_run", "blocked", "halted", "failed"])
async def test_the_command_does_not_run_on_a_dry_run_block_halt_or_failure(
    hooked: Harness, fake: FakeDocker, situation: str
) -> None:
    h = hooked
    tuning = h.seed.figures.tuning
    target = h.roots["staging"] / "environments" / tuning.name
    if situation == "blocked":
        await require_extra_key(h)
    if situation == "halted":
        target.write_text("EDITED=1\n", encoding="utf-8")
    if situation == "failed":
        await set_environment(h.db, tuning.id, target_path="no-such-folder/.env")
    outcome = await deploy_staging(h, dry_run=situation == "dry_run", only_tuning=True)
    assert outcome.post_deploy.status in {PostDeployStatus.NONE, PostDeployStatus.SKIPPED}
    assert fake.calls() == []
    assert not (fake.folder / "calls").exists()


async def test_an_unavailable_docker_skips_the_command_and_the_deploy_still_succeeds(
    harness: Harness, tmp_path: Path, fake: FakeDocker
) -> None:
    h = harness
    h.engine = DeployEngine(
        h.crypto,
        h.lock_dir,
        hook_runner=DockerHookRunner(fake.cli(mounted=False)),
        notifier=h.notifier,
    )
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.SUCCESS
    assert outcome.post_deploy.status is PostDeployStatus.SKIPPED
    assert outcome.post_deploy.reason == NOT_MOUNTED_REASON
    assert fake.calls() == []
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.result is AuditResult.SUCCESS
    assert entry.details["post_deploy"] == "skipped"
    assert entry.details["post_deploy_reason"] == NOT_MOUNTED_REASON
    (event,) = h.notifier.events
    assert event.post_deploy is PostDeployStatus.SKIPPED


async def test_the_run_reads_running_and_the_lock_stays_held_until_the_command_ends(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    fake.behavior(
        'touch "$dir/started"; while [ ! -f "$dir/release" ]; do sleep 0.1; done; echo finished'
    )
    task = asyncio.create_task(deploy_staging(h))
    for _ in range(300):
        if (fake.folder / "started").exists():
            break
        await asyncio.sleep(0.1)
    assert (fake.folder / "started").exists()

    async with create_session_factory(h.database)() as other:
        newest = select(DeployRun).order_by(DeployRun.id.desc()).limit(1)
        run = (await other.execute(newest)).scalar_one()
        assert run.post_deploy_status is PostDeployStatus.RUNNING
        assert run.finished_at is None and run.post_deploy_exit_code is None
        scope = await h.scope_of("superadmin")
        header = await load_service_header(other, scope, h.seed.services.staging_web.id)
        assert header is not None
        panel = await load_post_deploy(other, header, await load_service_history(other, header))
        assert panel.last is not None and panel.last.status is PostDeployStatus.RUNNING
    with pytest.raises(DeployLockBusyError), hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        pass
    nested = await deploy_staging(h, only_tuning=True)
    assert nested.refusal is DeployRefusal.LOCKED

    (fake.folder / "release").write_text("", encoding="utf-8")
    outcome = await asyncio.wait_for(task, 30)
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    finished = await stored_run(h, outcome)
    assert finished.post_deploy_status is PostDeployStatus.RAN and finished.finished_at is not None
    assert finished.post_deploy_output == "finished\n"
    with hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        pass


async def test_cancelling_a_deploy_while_the_command_runs_closes_the_run_as_failed(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    pid_file = fake.folder / "child.pid"
    fake.behavior(f'sleep 300 & echo $! > {pid_file}; touch "$dir/started"; wait')
    task = asyncio.create_task(deploy_staging(h))
    for _ in range(300):
        if (fake.folder / "started").exists():
            break
        await asyncio.sleep(0.1)
    assert (fake.folder / "started").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with create_session_factory(h.database)() as other:
        newest = select(DeployRun).order_by(DeployRun.id.desc()).limit(1)
        run = (await other.execute(newest)).scalar_one()
        assert run.status is DeployStatus.POST_DEPLOY_FAILED
        assert run.post_deploy_status is PostDeployStatus.FAILED
        assert run.post_deploy_reason == INTERRUPTED_REASON
        assert run.finished_at is not None and run.audit_entry_id is not None
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.result is AuditResult.FAILURE and entry.details["post_deploy"] == "failed"
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))
    with hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        pass


SECOND_TOKEN = "ghp_second-registry-token-77"


async def store_registry(
    h: Harness,
    host: str = "registry.example.com",
    username: str = "bot",
    token: str = TOKEN,
) -> DockerRegistry:
    """Add a registry login as the superadmin."""
    return await add_registry(h.db, h.crypto, host, username, token, actor=h.seed.users.superadmin)


async def registry_reads(h: Harness, deploy: Awaitable[DeployOutcome]) -> int:
    """How many statements read the registries while the deploy ran."""
    with count_statements(h.database) as statements:
        await deploy
    return sum(1 for statement in statements if "docker_registries" in statement)


async def test_a_deploy_hands_the_stored_logins_to_the_runner_and_hides_them_in_the_output(
    harness: Harness,
) -> None:
    h = harness
    await store_registry(h)
    await store_registry(h, "docker.io", "hub-user", SECOND_TOKEN)
    outcome = await deploy_staging(h)
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    (request,) = h.runner.requests
    assert request.registry_logins == (
        RegistryLogin("docker.io", "hub-user", SECOND_TOKEN),
        RegistryLogin("registry.example.com", "bot", TOKEN),
    )
    assert TOKEN not in repr(request) and SECOND_TOKEN not in repr(request)
    for secret in (
        TOKEN,
        SECOND_TOKEN,
        credentials("bot", TOKEN),
        credentials("hub-user", SECOND_TOKEN),
    ):
        assert request.scrubber.scrub_text(f"before {secret} after") == "before **** after"


async def test_a_deploy_without_registries_hands_the_runner_no_logins(harness: Harness) -> None:
    h = harness
    await deploy_staging(h)
    (request,) = h.runner.requests
    assert request.registry_logins == ()


async def test_a_deploy_that_runs_a_command_reads_the_registries_once(hooked: Harness) -> None:
    h = hooked
    await store_registry(h)
    assert await registry_reads(h, deploy_staging(h)) == 1
    assert await registry_reads(h, deploy_staging(h, force=True)) == 1


@pytest.mark.parametrize(
    "situation", ["no command", "unchanged", "switch off", "dry run", "blocked"]
)
async def test_no_registry_is_read_unless_a_command_will_run(
    hooked: Harness, situation: str
) -> None:
    h = hooked
    await store_registry(h)
    only_tuning = False
    if situation == "no command":
        await change_hook(h, commands="")
    if situation == "unchanged":
        assert (await deploy_staging(h)).post_deploy.status is PostDeployStatus.RAN
    if situation == "switch off":
        await switch(h, False)
    if situation == "blocked":
        await require_extra_key(h)
        only_tuning = True
    deploy = deploy_staging(h, dry_run=situation == "dry run", only_tuning=only_tuning)
    assert await registry_reads(h, deploy) == 0


async def test_a_token_that_cannot_be_opened_stops_the_command_and_names_no_secret(
    hooked: Harness, fake: FakeDocker, caplog: pytest.LogCaptureFixture
) -> None:
    h = hooked
    registry = await store_registry(h)
    sealed = registry.token_encrypted
    unreadable = h.crypto.encrypt(TOKEN, registry_context(registry.id + 1))
    registry.token_encrypted = unreadable
    await h.db.commit()
    caplog.set_level(logging.DEBUG)

    outcome = await deploy_staging(h)

    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert all(env.status is DeployResultStatus.WRITTEN for env in outcome.environments)
    assert outcome.post_deploy.status is PostDeployStatus.FAILED
    reason = outcome.post_deploy.reason
    assert reason == REGISTRY_UNREADABLE_REASON and "Settings, Registries" in reason
    assert outcome.post_deploy.exit_code is None and outcome.post_deploy.output is None
    assert fake.calls() == []
    run = await stored_run(h, outcome)
    assert (run.post_deploy_status, run.post_deploy_reason) == (
        PostDeployStatus.FAILED,
        REGISTRY_UNREADABLE_REASON,
    )
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["post_deploy"] == "failed"
    everything = json.dumps(entry.details) + repr(outcome) + caplog.text
    assert TOKEN not in everything
    assert unreadable not in everything and sealed not in everything
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    (event,) = h.notifier.events
    assert event.post_deploy is PostDeployStatus.FAILED


async def test_the_command_runs_with_the_stored_logins_and_prints_none_of_them(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    await store_registry(h)
    fake.capture_config(f'echo "pulling with {TOKEN}"; cat "$DOCKER_CONFIG/config.json"')

    outcome = await deploy_staging(h)

    assert outcome.post_deploy.status is PostDeployStatus.RAN
    captured = fake.captured_config()
    assert captured is not None
    assert captured.content == {
        "auths": {"registry.example.com": {"auth": credentials("bot", TOKEN)}}
    }
    assert list((fake.folder / "scratch").iterdir()) == []
    output = outcome.post_deploy.output
    assert output is not None and output.startswith("pulling with ****\n")
    run = await stored_run(h, outcome)
    (entry,) = await audit_of(h.db, "service.deploy")
    for secret in (TOKEN, credentials("bot", TOKEN)):
        assert secret not in output
        assert secret not in str(run.post_deploy_output)
        assert secret not in json.dumps(entry.details)


async def test_a_failing_command_output_is_scrubbed_of_the_logins_too(
    hooked: Harness, fake: FakeDocker
) -> None:
    h = hooked
    await store_registry(h)
    fake.behavior(f'echo "denied for bot:{TOKEN}" >&2; exit 1')
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert outcome.post_deploy.output == "denied for bot:****\n"
    run = await stored_run(h, outcome)
    assert run.post_deploy_output == "denied for bot:****\n"
