"""The deploy engine under the dist, drift and missing target policies, with force and backups."""

import stat
from pathlib import Path

from app.models import (
    AuditResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DistMissingPolicy,
    DistOutcome,
    DriftPolicy,
    DriftState,
    Environment,
    MissingTargetPolicy,
)
from app.services.deploy_types import DeployOutcome
from app.services.versions import create_version
from app.tests.deploy_helpers import (
    Harness,
    audit_of,
    files_in,
    require_extra_key,
    results_of,
    set_deployment,
    set_environment,
)
from app.tests.dist_helpers import link_dist, write_dist


def folder_of(h: Harness) -> Path:
    """The environments folder of the staging deployment."""
    return h.roots["staging"] / "environments"


async def deploy_files(
    h: Harness, *environments: Environment, dry_run: bool = False, force: bool = False
) -> DeployOutcome:
    """Deploy the given files of the staging web service."""
    return await h.deploy(
        h.seed.services.staging_web.id,
        environments=list(environments),
        dry_run=dry_run,
        force=force,
    )


async def set_staging(h: Harness, **fields: object) -> None:
    """Change fields of the staging deployment."""
    await set_deployment(h.db, h.seed.deployments.rentals_staging.id, **fields)


async def test_missing_dist_keys_block_the_environment_by_default(harness: Harness) -> None:
    h = harness
    tuning = await require_extra_key(h)
    outcome = await deploy_files(h, tuning)

    (result,) = outcome.environments
    assert result.status is DeployResultStatus.BLOCKED
    assert result.message == "Blocked: 1 dist key is missing from this file."
    assert result.dist is DistOutcome.BLOCKED
    assert outcome.status is DeployStatus.BLOCKED
    assert not (folder_of(h) / tuning.name).exists()
    assert h.runner.requests == []
    stored = await results_of(h.db, outcome.run_id or 0)
    assert stored[tuning.name].dist_outcome is DistOutcome.BLOCKED
    (check,) = await audit_of(h.db, "environment.dist_check")
    assert check.result is AuditResult.BLOCKED and check.details["missing"] == 1
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.result is AuditResult.BLOCKED


async def test_the_warn_policy_deploys_with_a_warning(harness: Harness) -> None:
    h = harness
    tuning = await require_extra_key(h)
    await set_staging(h, dist_missing_policy=DistMissingPolicy.WARN)
    outcome = await deploy_files(h, tuning)
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.WRITTEN and result.dist is DistOutcome.WARNING
    assert "the policy warns" in result.message
    assert outcome.status is DeployStatus.SUCCESS
    assert (folder_of(h) / tuning.name).exists()


async def test_the_environment_policy_wins_over_the_deployment_default(harness: Harness) -> None:
    h = harness
    tuning = await require_extra_key(h)
    await set_staging(h, dist_missing_policy=DistMissingPolicy.WARN)
    await set_environment(h.db, tuning.id, dist_missing_policy=DistMissingPolicy.BLOCK)
    blocked = await deploy_files(h, tuning)
    assert blocked.environments[0].status is DeployResultStatus.BLOCKED

    await set_staging(h, dist_missing_policy=DistMissingPolicy.BLOCK)
    await set_environment(h.db, tuning.id, dist_missing_policy=DistMissingPolicy.WARN)
    warned = await deploy_files(h, tuning)
    assert warned.environments[0].status is DeployResultStatus.WRITTEN


async def test_force_overrides_a_dist_block_and_is_audited(harness: Harness) -> None:
    h = harness
    tuning = await require_extra_key(h)
    outcome = await deploy_files(h, tuning, force=True)
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.WRITTEN and result.forced
    assert "Forced past the dist check." in result.message
    assert result.dist is DistOutcome.BLOCKED
    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None and run.force
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["force"] is True
    environments = entry.details["environments"]
    assert isinstance(environments, list) and environments[0]["forced"] is True


async def test_a_dist_check_that_cannot_run_follows_the_policy(harness: Harness) -> None:
    h = harness
    tuning = h.seed.figures.tuning
    await link_dist(h.db, tuning, "environments/not-there-dist")
    blocked = await deploy_files(h, tuning)
    (result,) = blocked.environments
    assert result.status is DeployResultStatus.BLOCKED and result.dist is DistOutcome.FAILED
    assert result.message == (
        "Blocked: the dist check could not run. "
        "The dist file does not exist or is not a regular file."
    )
    await set_staging(h, dist_missing_policy=DistMissingPolicy.WARN)
    warned = await deploy_files(h, tuning)
    assert warned.environments[0].status is DeployResultStatus.WRITTEN
    assert "could not run (the policy warns)" in warned.environments[0].message


async def test_files_without_a_dist_comparison_are_deployed_and_recorded_as_skipped(
    harness: Harness,
) -> None:
    h = harness
    write_dist(h.roots["staging"], "environments/web-ca.pem-dist", "not env format\n")
    await link_dist(h.db, h.seed.figures.ca, "environments/web-ca.pem-dist")
    outcome = await deploy_files(h, h.seed.figures.ca, h.seed.envs.staging_web)
    assert {result.dist for result in outcome.environments} == {DistOutcome.SKIPPED}
    assert {result.status for result in outcome.environments} == {DeployResultStatus.WRITTEN}


async def test_a_blocked_environment_does_not_hold_back_the_others(harness: Harness) -> None:
    h = harness
    tuning = await require_extra_key(h)
    outcome = await deploy_files(h, tuning, h.seed.envs.staging_web, h.seed.figures.ca)
    statuses = {result.name: result.status for result in outcome.environments}
    assert statuses == {
        ".env.web": DeployResultStatus.WRITTEN,
        ".env.web.tuning": DeployResultStatus.BLOCKED,
        "web-ca.pem": DeployResultStatus.WRITTEN,
    }
    assert outcome.status is DeployStatus.BLOCKED
    assert h.runner.requests == []


async def test_the_first_deploy_to_a_differing_target_halts_by_default(harness: Harness) -> None:
    h = harness
    web = h.seed.envs.staging_web
    target = folder_of(h) / web.name
    target.write_text("WORKERS=old\n", encoding="utf-8")
    outcome = await deploy_files(h, web)

    (result,) = outcome.environments
    assert result.status is DeployResultStatus.HALTED and result.drift is DriftState.DRIFT
    assert outcome.status is DeployStatus.HALTED
    assert target.read_text(encoding="utf-8") == "WORKERS=old\n"
    assert files_in(folder_of(h)) == [".env.web"]
    assert result.target_hash_before == h.crypto.content_hash("WORKERS=old\n")
    assert result.written_hash is None
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.result is AuditResult.HALTED
    assert h.runner.requests == []


async def test_force_overwrites_a_drifted_target_and_the_force_flag_is_audited(
    harness: Harness,
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    target = folder_of(h) / web.name
    target.write_text("WORKERS=old\n", encoding="utf-8")
    outcome = await deploy_files(h, web, force=True)
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.WRITTEN and result.forced
    assert result.backup_path is None and files_in(folder_of(h)) == [".env.web"]
    assert target.read_text(encoding="utf-8") != "WORKERS=old\n"
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.details["force"] is True and entry.result is AuditResult.SUCCESS


async def test_the_overwrite_policy_replaces_a_drifted_target_without_a_backup(
    harness: Harness,
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    (folder_of(h) / web.name).write_text("WORKERS=old\n", encoding="utf-8")
    await set_staging(h, drift_policy=DriftPolicy.OVERWRITE)
    outcome = await deploy_files(h, web)
    assert outcome.environments[0].status is DeployResultStatus.WRITTEN
    assert not outcome.environments[0].forced
    assert files_in(folder_of(h)) == [".env.web"]


async def test_the_backup_policy_keeps_the_old_file_next_to_the_target_with_mode_0600(
    harness: Harness,
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    target = folder_of(h) / web.name
    target.write_text("WORKERS=old\n", encoding="utf-8")
    target.chmod(0o644)
    await set_staging(h, drift_policy=DriftPolicy.BACKUP_OVERWRITE)
    outcome = await deploy_files(h, web)

    (result,) = outcome.environments
    assert result.status is DeployResultStatus.WRITTEN
    assert result.backup_path is not None and result.backup_path.startswith(
        "environments/.env.web.bak-"
    )
    assert "backed up" in result.message
    backup = h.roots["staging"] / result.backup_path
    assert backup.read_text(encoding="utf-8") == "WORKERS=old\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") != "WORKERS=old\n"
    stored = await results_of(h.db, outcome.run_id or 0)
    assert stored[".env.web"].backup_path == result.backup_path


async def test_the_environment_drift_policy_wins_over_the_deployment_default(
    harness: Harness,
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    target = folder_of(h) / web.name
    target.write_text("WORKERS=old\n", encoding="utf-8")
    await set_staging(h, drift_policy=DriftPolicy.HALT)
    await set_environment(h.db, web.id, drift_policy=DriftPolicy.OVERWRITE)
    assert (await deploy_files(h, web)).environments[0].status is DeployResultStatus.WRITTEN

    target.write_text("WORKERS=old again\n", encoding="utf-8")
    await set_staging(h, drift_policy=DriftPolicy.OVERWRITE)
    await set_environment(h.db, web.id, drift_policy=DriftPolicy.HALT)
    assert (await deploy_files(h, web)).environments[0].status is DeployResultStatus.HALTED


async def test_the_first_deploy_to_an_empty_target_is_not_drift(harness: Harness) -> None:
    h = harness
    web, tuning = h.seed.envs.staging_web, h.seed.figures.tuning
    (folder_of(h) / web.name).write_text("", encoding="utf-8")
    (folder_of(h) / tuning.name).write_text(" \n", encoding="utf-8")
    outcome = await deploy_files(h, web, tuning)
    assert {result.status for result in outcome.environments} == {DeployResultStatus.WRITTEN}
    assert {result.drift for result in outcome.environments} == {DriftState.IN_SYNC}


async def test_an_identical_target_is_unchanged_and_becomes_the_drift_baseline(
    harness: Harness,
) -> None:
    h = harness
    tuning = h.seed.figures.tuning
    target = folder_of(h) / tuning.name
    target.write_text("WORKERS=4\n", encoding="utf-8")
    first = await deploy_files(h, tuning)
    assert first.environments[0].status is DeployResultStatus.UNCHANGED
    assert first.environments[0].drift is DriftState.IN_SYNC

    await create_version(h.db, h.crypto, tuning.id, "WORKERS=8\n", author=h.seed.users.admin)
    await h.db.commit()
    pending = await deploy_files(h, tuning)
    assert pending.environments[0].status is DeployResultStatus.WRITTEN
    assert pending.environments[0].drift is DriftState.IN_SYNC
    assert target.read_text(encoding="utf-8") == "WORKERS=8\n"


async def test_a_target_edited_outside_the_app_after_a_deploy_is_drift(harness: Harness) -> None:
    h = harness
    tuning = h.seed.figures.tuning
    assert (await deploy_files(h, tuning)).status is DeployStatus.SUCCESS
    target = folder_of(h) / tuning.name
    target.write_text("WORKERS=99\n", encoding="utf-8")
    await create_version(h.db, h.crypto, tuning.id, "WORKERS=8\n", author=h.seed.users.admin)
    await h.db.commit()

    halted = await deploy_files(h, tuning)
    assert halted.environments[0].status is DeployResultStatus.HALTED
    assert target.read_text(encoding="utf-8") == "WORKERS=99\n"
    forced = await deploy_files(h, tuning, force=True)
    assert forced.environments[0].status is DeployResultStatus.WRITTEN
    assert target.read_text(encoding="utf-8") == "WORKERS=8\n"


async def test_a_dry_run_never_becomes_the_drift_baseline(harness: Harness) -> None:
    h = harness
    web = h.seed.envs.staging_web
    (folder_of(h) / web.name).write_text("WORKERS=old\n", encoding="utf-8")
    await set_staging(h, drift_policy=DriftPolicy.OVERWRITE)
    dry = await deploy_files(h, web, dry_run=True)
    assert dry.environments[0].status is DeployResultStatus.WOULD_WRITE
    await set_staging(h, drift_policy=DriftPolicy.HALT)
    real = await deploy_files(h, web)
    assert real.environments[0].status is DeployResultStatus.HALTED


async def test_a_missing_target_is_created_by_default(harness: Harness) -> None:
    h = harness
    ca = h.seed.figures.ca
    outcome = await deploy_files(h, ca)
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.WRITTEN and result.drift is DriftState.MISSING
    assert result.message.startswith("Created.")
    assert (folder_of(h) / ca.name).exists()


async def test_a_missing_target_can_halt_and_force_overrides_it(harness: Harness) -> None:
    h = harness
    ca = h.seed.figures.ca
    await set_staging(h, missing_target_policy=MissingTargetPolicy.HALT)
    halted = await deploy_files(h, ca)
    assert halted.environments[0].status is DeployResultStatus.HALTED
    assert not (folder_of(h) / ca.name).exists()
    forced = await deploy_files(h, ca, force=True)
    assert forced.environments[0].status is DeployResultStatus.WRITTEN
    assert forced.environments[0].forced and (folder_of(h) / ca.name).exists()


async def test_a_target_removed_after_a_deploy_is_created_again(harness: Harness) -> None:
    h = harness
    tuning = h.seed.figures.tuning
    await deploy_files(h, tuning)
    (folder_of(h) / tuning.name).unlink()
    again = await deploy_files(h, tuning)
    assert again.environments[0].status is DeployResultStatus.WRITTEN
    assert again.environments[0].drift is DriftState.MISSING


async def test_a_missing_target_folder_fails_the_environment(harness: Harness) -> None:
    h = harness
    await set_environment(h.db, h.seed.figures.ca.id, target_path="not-there/web-ca.pem")
    outcome = await deploy_files(h, h.seed.figures.ca)
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.FAILED
    assert result.message == "The target folder does not exist."
    assert not (h.roots["staging"] / "not-there").exists()


async def test_a_target_that_is_a_symlink_or_a_folder_is_not_replaced(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    victim = tmp_path / "victim"
    victim.write_text("UNTOUCHED\n", encoding="utf-8")
    (folder_of(h) / ".env.web").symlink_to(victim)
    (folder_of(h) / ".env.web.tuning").mkdir()
    outcome = await deploy_files(h, h.seed.envs.staging_web, h.seed.figures.tuning, force=True)
    messages = {result.name: result.message for result in outcome.environments}
    assert messages[".env.web"] == "The target is a symbolic link and is not replaced."
    assert messages[".env.web.tuning"] == "The target is not a regular file."
    assert {result.status for result in outcome.environments} == {DeployResultStatus.FAILED}
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"
    assert (folder_of(h) / ".env.web").is_symlink()
