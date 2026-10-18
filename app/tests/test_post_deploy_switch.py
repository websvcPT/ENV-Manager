"""The Settings switch for post-deploy commands: the engine, the real runner and the preview."""

from pathlib import Path

import pytest

from app.models import DeployRun, DeployStatus, PostDeployStatus
from app.services.app_settings import PostDeploySettings, save_section
from app.services.audit import AuditActor
from app.services.deploy import DeployEngine
from app.services.deploy_types import DeployOutcome
from app.services.hook_runner import DockerHookRunner
from app.services.hooks import POST_DEPLOY_OFF_REASON
from app.tests.deploy_helpers import Harness, RecordingRunner, audit_of
from app.tests.fake_docker import FakeDocker
from app.tests.test_deploy_dialog import preflight_of, unlink_dists


async def switch(h: Harness, enabled: bool) -> None:
    """Set the Settings switch for post-deploy commands."""
    await save_section(
        h.db, "post_deploy", PostDeploySettings(enabled=enabled), actor=AuditActor.cli()
    )
    await h.db.commit()


async def deploy_staging(h: Harness, *, force: bool = False) -> DeployOutcome:
    """Deploy the staging web service, which has a stored command."""
    return await h.deploy(h.seed.services.staging_web.id, force=force)


async def stored_run(h: Harness, outcome: DeployOutcome) -> DeployRun:
    """The run row of an outcome, read fresh from the database."""
    await h.db.rollback()
    run = await h.db.get(DeployRun, outcome.run_id, populate_existing=True)
    assert run is not None
    return run


async def test_with_the_switch_on_the_command_runs_as_before(harness: Harness) -> None:
    """A new installation has no stored value and the switch reads as on."""
    h = harness
    outcome = await deploy_staging(h)
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    assert len(h.runner.requests) == 1


async def test_with_the_switch_off_the_command_is_skipped_with_the_reason(harness: Harness) -> None:
    """The deploy is a success, the runner is never called and the reason is recorded."""
    h = harness
    await switch(h, False)
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.SUCCESS
    assert outcome.post_deploy.status is PostDeployStatus.SKIPPED
    assert outcome.post_deploy.reason == POST_DEPLOY_OFF_REASON
    assert h.runner.requests == []
    run = await stored_run(h, outcome)
    assert run.post_deploy_status is PostDeployStatus.SKIPPED
    assert run.post_deploy_reason == POST_DEPLOY_OFF_REASON
    assert run.post_deploy_exit_code is None and run.post_deploy_output is None
    assert run.finished_at is not None
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["post_deploy"] == "skipped"
    assert entry.details["post_deploy_reason"] == POST_DEPLOY_OFF_REASON
    (event,) = h.notifier.events
    assert event.post_deploy is PostDeployStatus.SKIPPED and event.status is DeployStatus.SUCCESS


async def test_the_run_is_never_recorded_as_running_when_the_switch_is_off(
    harness: Harness,
) -> None:
    """The skip is known before the run is stored, so no page ever shows a command in progress."""
    h = harness
    seen: list[PostDeployStatus] = []

    async def look() -> None:
        seen.append(PostDeployStatus.RUNNING)

    h.engine = DeployEngine(
        h.crypto, h.lock_dir, hook_runner=RecordingRunner(on_run=look), notifier=h.notifier
    )
    await switch(h, False)
    outcome = await deploy_staging(h)
    assert seen == []
    assert (await stored_run(h, outcome)).post_deploy_status is PostDeployStatus.SKIPPED


async def test_force_does_not_override_the_switch(harness: Harness) -> None:
    """Force overrides block and halt policies, not the decision to switch commands off."""
    h = harness
    await switch(h, False)
    outcome = await deploy_staging(h, force=True)
    assert outcome.post_deploy.reason == POST_DEPLOY_OFF_REASON
    assert h.runner.requests == []


async def test_a_service_without_a_command_reports_none_whatever_the_switch(
    harness: Harness,
) -> None:
    """There is nothing to skip when no command is stored."""
    h = harness
    await switch(h, False)
    outcome = await h.deploy(h.seed.services.mysql.id)
    assert outcome.post_deploy.status is PostDeployStatus.NONE
    assert outcome.post_deploy.reason is None


async def test_the_switch_is_read_again_on_every_deploy(harness: Harness) -> None:
    """Turning it back on takes effect for the very next deploy, and so does turning it off."""
    h = harness
    await switch(h, False)
    assert (await deploy_staging(h)).post_deploy.status is PostDeployStatus.SKIPPED
    await switch(h, True)
    assert (await deploy_staging(h, force=True)).post_deploy.status is PostDeployStatus.RAN
    await switch(h, False)
    assert (await deploy_staging(h, force=True)).post_deploy.status is PostDeployStatus.SKIPPED
    assert len(h.runner.requests) == 1


async def test_a_dry_run_is_unaffected(harness: Harness) -> None:
    """A dry run runs no command either way and records no run."""
    h = harness
    await switch(h, False)
    outcome = await h.deploy(h.seed.services.staging_web.id, dry_run=True)
    assert outcome.dry_run and h.runner.requests == []


async def test_the_real_runner_starts_no_process_when_the_switch_is_off(
    harness: Harness, tmp_path: Path
) -> None:
    """With the real runner on a fake docker, no docker call is made while the switch is off."""
    h = harness
    fake = FakeDocker(tmp_path / "fake")
    h.engine = DeployEngine(
        h.crypto, h.lock_dir, hook_runner=DockerHookRunner(fake.cli()), notifier=h.notifier
    )
    await switch(h, False)
    outcome = await deploy_staging(h)
    assert outcome.post_deploy.reason == POST_DEPLOY_OFF_REASON
    assert fake.calls() == []
    await switch(h, True)
    assert (await deploy_staging(h, force=True)).post_deploy.status is PostDeployStatus.RAN
    assert len(fake.calls()) == 1


async def test_the_deploy_dialog_preview_gives_the_same_reason(
    harness: Harness, tmp_path: Path
) -> None:
    """The pre-flight says the command is skipped, for the superadmin with the command text."""
    h = harness
    await unlink_dists(h)
    cli = FakeDocker(tmp_path / "docker").cli()
    assert (await preflight_of(h, cli=cli, user="superadmin")).preview.state == "run"
    await switch(h, False)
    superadmin = (await preflight_of(h, cli=cli, user="superadmin")).preview
    assert (superadmin.state, superadmin.reason) == ("skipped", POST_DEPLOY_OFF_REASON)
    assert superadmin.command is not None
    admin = (await preflight_of(h, cli=cli, user="admin")).preview
    assert (admin.state, admin.reason, admin.command) == ("skipped", POST_DEPLOY_OFF_REASON, None)


async def test_the_preview_of_a_service_without_a_command_stays_none(
    harness: Harness, tmp_path: Path
) -> None:
    """The switch does not turn a missing command into a skipped one."""
    h = harness
    await switch(h, False)
    cli = FakeDocker(tmp_path / "docker").cli()
    preview = (await preflight_of(h, cli=cli, service_id=h.seed.services.mysql.id)).preview
    assert preview.state == "none"


@pytest.mark.parametrize("enabled", [True, False])
async def test_changing_the_switch_is_audited_by_the_settings_service_only(
    harness: Harness, enabled: bool
) -> None:
    """Deploys leave the setting alone: only the settings change writes a settings entry."""
    h = harness
    await switch(h, enabled)
    await deploy_staging(h)
    entries = await audit_of(h.db, "settings.update")
    assert len(entries) == (0 if enabled else 1)
