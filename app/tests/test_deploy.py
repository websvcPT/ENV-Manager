"""The deploy engine: selection by scope, records, actors, attributes, dry run and path checks."""

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from app.models import (
    AllowedPath,
    AuditActorType,
    AuditResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    DistOutcome,
    DriftPolicy,
    DriftState,
    Environment,
    PostDeployStatus,
)
from app.services import deploy as deploy_module
from app.services.audit import AuditActor
from app.services.deploy import (
    ENVIRONMENT_NOT_FOUND,
    NOTHING_SELECTED,
    SERVICE_NOT_FOUND,
    VERSION_NOT_FOUND,
)
from app.services.deploy_types import DeployOutcome, DeployRequest, DeployRequestError
from app.services.paths import AllowedPaths
from app.services.permissions import cli_scope, load_token_scope
from app.services.target_files import TargetLocation, write_target_file
from app.tests.deploy_helpers import (
    Harness,
    audit_of,
    files_in,
    results_of,
    set_deployment,
    set_environment,
    version_text,
)
from app.tests.dist_helpers import link_dist


def staging_folder(h: Harness) -> Path:
    """The environments folder of the staging deployment."""
    return h.roots["staging"] / "environments"


async def staging_run(
    h: Harness,
    *,
    environments: list[Environment] | None = None,
    versions: dict[int, int] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> DeployOutcome:
    """Deploy the staging web service."""
    return await h.deploy(
        h.seed.services.staging_web.id,
        environments=environments,
        versions=versions,
        dry_run=dry_run,
        force=force,
    )


async def test_a_deploy_writes_the_selected_environments_and_records_the_run(
    harness: Harness,
) -> None:
    h = harness
    chosen = [h.seed.envs.staging_web, h.seed.figures.tuning]
    outcome = await staging_run(h, environments=chosen)

    assert outcome.status is DeployStatus.SUCCESS and outcome.run_id is not None
    assert not outcome.dry_run and not outcome.force and outcome.refusal is None
    assert files_in(staging_folder(h)) == [".env.web", ".env.web.tuning"]
    for environment in chosen:
        written = staging_folder(h) / environment.name
        assert written.read_text(encoding="utf-8") == await version_text(
            h.db, h.crypto, environment.id
        )
        assert stat.S_IMODE(written.stat().st_mode) == 0o600

    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None
    assert run.service_id == h.seed.services.staging_web.id
    assert (run.service_name, run.deployment_name) == ("web", "staging")
    assert run.trigger is DeployTrigger.UI and run.triggered_by_user_id == h.seed.users.admin.id
    assert (run.dry_run, run.force) == (False, False)
    assert run.status is DeployStatus.SUCCESS and run.finished_at is not None
    assert run.post_deploy_status is PostDeployStatus.RAN and run.post_deploy_exit_code == 0

    results = await results_of(h.db, outcome.run_id)
    assert set(results) == {".env.web", ".env.web.tuning"}
    for environment in chosen:
        result = results[environment.name]
        assert result.status is DeployResultStatus.WRITTEN
        assert result.target_path == f"environments/{environment.name}"
        assert result.written_hash == h.crypto.content_hash(
            await version_text(h.db, h.crypto, environment.id)
        )
        assert result.target_hash_before is None
        assert (result.dist_outcome, result.drift_state) == (
            DistOutcome.SKIPPED,
            DriftState.MISSING,
        )
        assert result.version_id is not None and str(h.roots["staging"]) not in str(result.message)


async def test_a_deploy_is_audited_with_the_actor_versions_and_force_flag(
    harness: Harness,
) -> None:
    h = harness
    outcome = await staging_run(h, environments=[h.seed.figures.tuning], force=True)
    (entry,) = [
        entry
        for entry in await audit_of(h.db, "service.deploy")
        if entry.details.get("run_id") == outcome.run_id
    ]
    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None and run.force and run.audit_entry_id == entry.id
    assert entry.result is AuditResult.SUCCESS
    assert (entry.actor_type, entry.actor_user_id, entry.actor_name) == (
        AuditActorType.USER,
        h.seed.users.admin.id,
        "admin",
    )
    assert (entry.target_type, entry.target_id, entry.service_id) == (
        "service",
        h.seed.services.staging_web.id,
        h.seed.services.staging_web.id,
    )
    assert entry.deployment_id == h.seed.deployments.fancy_staging.id
    assert entry.details["force"] is True and entry.details["dry_run"] is False
    assert entry.details["environments"] == [
        {
            "environment": ".env.web.tuning",
            "version": 2,
            "status": "written",
            "dist": "skipped",
            "drift": "missing",
            "forced": False,
        }
    ]
    results = await results_of(h.db, outcome.run_id or 0)
    assert entry.version_ids == [results[".env.web.tuning"].version_id]


async def test_dist_checks_of_a_deploy_are_audited_with_the_deploy_actor(
    harness: Harness,
) -> None:
    h = harness
    chosen = [h.seed.envs.staging_web, h.seed.figures.tuning]
    await staging_run(h, environments=chosen)
    checks = await audit_of(h.db, "environment.dist_check")
    assert {entry.environment_id for entry in checks} == {environment.id for environment in chosen}
    assert {(entry.actor_type, entry.actor_name) for entry in checks} == {
        (AuditActorType.USER, "admin")
    }


async def test_a_token_and_the_cli_deploy_as_their_own_actor(harness: Harness) -> None:
    h = harness
    token = h.seed.tokens[0]
    token_scope = await load_token_scope(h.db, token)
    assert token_scope is not None
    api = await h.engine.run(
        h.db,
        token_scope,
        AuditActor.for_token(token, h.seed.users.admin),
        DeployRequest(service_id=h.seed.services.api.id),
    )
    cli = await h.engine.run(
        h.db,
        cli_scope(),
        AuditActor.cli(),
        DeployRequest(service_id=h.seed.services.worker.id),
    )
    api_run, cli_run = await h.db.get(DeployRun, api.run_id), await h.db.get(DeployRun, cli.run_id)
    assert api_run is not None and cli_run is not None
    assert api_run.trigger is DeployTrigger.API
    assert (api_run.triggered_by_token_id, api_run.triggered_by_user_id) == (token.id, None)
    assert cli_run.trigger is DeployTrigger.CLI
    assert (cli_run.triggered_by_token_id, cli_run.triggered_by_user_id) == (None, None)
    by_run = {entry.details["run_id"]: entry for entry in await audit_of(h.db, "service.deploy")}
    api_entry, cli_entry = by_run[api.run_id], by_run[cli.run_id]
    assert (api_entry.actor_type, api_entry.actor_token_id, api_entry.actor_user_id) == (
        AuditActorType.API,
        token.id,
        h.seed.users.admin.id,
    )
    assert api_entry.actor_name == f"token {token.name}"
    assert (cli_entry.actor_type, cli_entry.actor_user_id, cli_entry.actor_name) == (
        AuditActorType.CLI,
        None,
        "cli",
    )
    dist_actors = {entry.actor_type for entry in await audit_of(h.db, "environment.dist_check")}
    assert dist_actors == {AuditActorType.API, AuditActorType.CLI}


async def test_the_scope_decides_which_environments_are_deployed(harness: Harness) -> None:
    h = harness
    await link_dist(h.db, h.seed.envs.db, None)
    mysql = h.seed.services.mysql
    outcome = await h.deploy(mysql.id, user="env_scoped_admin")
    assert [environment.name for environment in outcome.environments] == [".env.db"]
    folder = h.roots["prod"] / "environments"
    assert files_in(folder) == [".env.db"]

    with pytest.raises(DeployRequestError, match=ENVIRONMENT_NOT_FOUND):
        await h.deploy(mysql.id, user="env_scoped_admin", environments=[h.seed.envs.db_secrets])
    assert files_in(folder) == [".env.db"]


@pytest.mark.parametrize("user", ["viewer", "ungranted_admin", "env_scoped_viewer"])
async def test_a_caller_without_deploy_permission_gets_no_service(
    harness: Harness, user: str
) -> None:
    with pytest.raises(DeployRequestError, match=SERVICE_NOT_FOUND):
        await harness.deploy(harness.seed.services.staging_web.id, user=user)
    assert files_in(staging_folder(harness)) == []


async def test_requests_naming_nothing_usable_are_refused(harness: Harness) -> None:
    h = harness
    with pytest.raises(DeployRequestError, match=SERVICE_NOT_FOUND):
        await h.deploy(999_999)
    scope = await h.scope_of("admin")
    actor = AuditActor.for_user(h.seed.users.admin)
    web_id = h.seed.services.staging_web.id
    with pytest.raises(DeployRequestError, match=NOTHING_SELECTED):
        await h.engine.run(h.db, scope, actor, DeployRequest(web_id, environment_ids=()))
    with pytest.raises(DeployRequestError, match=VERSION_NOT_FOUND):
        await h.deploy(web_id, versions={h.seed.envs.staging_web.id: 99})
    other = h.seed.envs.api
    with pytest.raises(DeployRequestError, match=ENVIRONMENT_NOT_FOUND):
        await h.deploy(web_id, environments=[other])
    assert files_in(staging_folder(h)) == []


async def test_an_older_version_can_be_deployed_and_is_the_one_checked(harness: Harness) -> None:
    h = harness
    tuning = h.seed.figures.tuning
    older = await version_text(h.db, h.crypto, tuning.id, 1)
    assert older != await version_text(h.db, h.crypto, tuning.id)
    outcome = await staging_run(h, environments=[tuning], versions={tuning.id: 1})
    assert (staging_folder(h) / tuning.name).read_text(encoding="utf-8") == older
    assert outcome.environments[0].version_number == 1
    (check,) = [
        entry
        for entry in await audit_of(h.db, "environment.dist_check")
        if entry.environment_id == tuning.id
    ]
    assert check.details["version"] == 1


async def test_deployment_defaults_and_environment_overrides_set_the_mode(
    harness: Harness,
) -> None:
    h = harness
    await set_deployment(h.db, h.seed.deployments.fancy_staging.id, file_mode="0640")
    await set_environment(h.db, h.seed.figures.tuning.id, file_mode="0644")
    await staging_run(h, environments=[h.seed.envs.staging_web, h.seed.figures.tuning])
    modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in staging_folder(h).iterdir()}
    assert modes == {".env.web": 0o640, ".env.web.tuning": 0o644}


async def test_the_configured_owner_and_group_are_applied_when_the_app_may_set_them(
    harness: Harness,
) -> None:
    h = harness
    await set_deployment(
        h.db,
        h.seed.deployments.fancy_staging.id,
        file_owner=str(os.getuid()),
        file_group=str(os.getgid()),
    )
    outcome = await staging_run(h, environments=[h.seed.figures.tuning])
    assert outcome.status is DeployStatus.SUCCESS
    info = (staging_folder(h) / ".env.web.tuning").stat()
    assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid())


async def test_an_owner_the_app_cannot_set_fails_the_environment_with_a_clear_reason(
    harness: Harness,
) -> None:
    h = harness

    def deny(_descriptor: int, _uid: int, _gid: int) -> None:
        raise PermissionError

    h.engine.change_owner = deny
    await set_deployment(h.db, h.seed.deployments.fancy_staging.id, file_owner="0")
    (staging_folder(h) / ".env.web.tuning").write_text("WORKERS=9\n", encoding="utf-8")
    outcome = await staging_run(h, environments=[h.seed.figures.tuning], force=True)

    (result,) = outcome.environments
    assert result.status is DeployResultStatus.FAILED
    assert "no privilege to change ownership" in result.message
    assert outcome.status is DeployStatus.FAILED
    assert (staging_folder(h) / ".env.web.tuning").read_text(encoding="utf-8") == "WORKERS=9\n"
    assert files_in(staging_folder(h)) == [".env.web.tuning"]
    assert h.runner.requests == []


async def test_an_unknown_owner_name_and_an_invalid_mode_fail_the_environment(
    harness: Harness,
) -> None:
    h = harness
    await set_environment(h.db, h.seed.figures.tuning.id, file_owner="no-such-account-xyz")
    await set_environment(h.db, h.seed.envs.staging_web.id, file_mode="rw-r--r--")
    outcome = await staging_run(
        h, environments=[h.seed.figures.tuning, h.seed.envs.staging_web, h.seed.figures.ca]
    )
    by_name = {result.name: result for result in outcome.environments}
    assert by_name[".env.web.tuning"].message == "The configured owner or group does not exist."
    assert by_name[".env.web"].message == "The configured file mode is invalid."
    assert by_name["web-ca.pem"].status is DeployResultStatus.WRITTEN
    assert files_in(staging_folder(h)) == ["web-ca.pem"]


async def test_an_unchanged_environment_is_reported_and_not_rewritten(harness: Harness) -> None:
    h = harness
    chosen = [h.seed.figures.tuning]
    await staging_run(h, environments=chosen)
    written = staging_folder(h) / ".env.web.tuning"
    before = written.stat()
    second = await staging_run(h, environments=chosen)

    (result,) = second.environments
    assert result.status is DeployResultStatus.UNCHANGED
    assert second.status is DeployStatus.SUCCESS
    after = written.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert files_in(staging_folder(h)) == [".env.web.tuning"]
    assert [request.changed for request in h.runner.requests] == [True, False]
    stored = await results_of(h.db, second.run_id or 0)
    assert result.written_hash is not None
    assert stored[".env.web.tuning"].written_hash == result.written_hash
    assert stored[".env.web.tuning"].drift_state is DriftState.IN_SYNC


async def test_a_dry_run_reports_without_writing_backing_up_or_taking_the_lock(
    harness: Harness,
) -> None:
    h = harness
    folder = staging_folder(h)
    (folder / ".env.web").write_text("WORKERS=old\n", encoding="utf-8")
    staging_id = h.seed.deployments.fancy_staging.id
    await set_deployment(h.db, staging_id, drift_policy=DriftPolicy.BACKUP_OVERWRITE)
    before = (folder / ".env.web").stat()
    outcome = await staging_run(h, dry_run=True)

    assert outcome.dry_run and outcome.status is DeployStatus.SUCCESS
    by_name = {result.name: result for result in outcome.environments}
    assert {result.status for result in by_name.values()} == {DeployResultStatus.WOULD_WRITE}
    assert "would be backed up" in by_name[".env.web"].message
    assert by_name[".env.web"].drift is DriftState.DRIFT
    assert by_name["web-ca.pem"].drift is DriftState.MISSING
    assert files_in(folder) == [".env.web"]
    assert (folder / ".env.web").read_text(encoding="utf-8") == "WORKERS=old\n"
    assert (folder / ".env.web").stat().st_mtime_ns == before.st_mtime_ns
    assert not h.lock_dir.exists()
    assert h.runner.requests == [] and h.notifier.events == []

    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None and run.dry_run and run.post_deploy_status is PostDeployStatus.NONE
    results = await results_of(h.db, run.id)
    assert {result.status for result in results.values()} == {DeployResultStatus.WOULD_WRITE}
    assert results[".env.web"].backup_path is None and results[".env.web"].written_hash is None
    (entry,) = [
        entry
        for entry in await audit_of(h.db, "service.deploy_dry_run")
        if entry.details.get("run_id") == run.id
    ]
    assert entry.details["dry_run"] is True and entry.result is AuditResult.SUCCESS
    dist_checks = await audit_of(h.db, "environment.dist_check")
    assert len(dist_checks) == 4


async def test_a_dry_run_reports_a_halt_like_a_real_run(harness: Harness) -> None:
    h = harness
    (staging_folder(h) / ".env.web").write_text("WORKERS=old\n", encoding="utf-8")
    outcome = await staging_run(h, dry_run=True)
    by_name = {result.name: result.status for result in outcome.environments}
    assert by_name[".env.web"] is DeployResultStatus.HALTED
    assert outcome.status is DeployStatus.HALTED
    forced = await staging_run(h, dry_run=True, force=True)
    assert {result.status for result in forced.environments} == {DeployResultStatus.WOULD_WRITE}


async def test_allowed_paths_are_loaded_fresh_for_every_deploy(harness: Harness) -> None:
    h = harness
    chosen = [h.seed.figures.tuning]
    assert (await staging_run(h, environments=chosen)).status is DeployStatus.SUCCESS
    (staging_folder(h) / ".env.web.tuning").write_text("EDITED=1\n", encoding="utf-8")
    await h.db.execute(delete(AllowedPath))
    await h.db.commit()

    outcome = await staging_run(h, environments=chosen, force=True)
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.FAILED and outcome.status is DeployStatus.FAILED
    assert result.message == "The deployment is outside the allowed paths."
    assert str(h.roots["staging"]) not in result.message
    assert (staging_folder(h) / ".env.web.tuning").read_text(encoding="utf-8") == "EDITED=1\n"
    assert result.dist is None and result.drift is None
    assert len(h.runner.requests) == 1


async def test_a_deployment_moved_outside_the_allowed_paths_cannot_deploy(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    elsewhere = tmp_path.resolve() / "elsewhere"
    (elsewhere / "environments").mkdir(parents=True)
    await set_deployment(h.db, h.seed.deployments.fancy_staging.id, root_path=str(elsewhere))
    outcome = await staging_run(h, force=True)
    assert outcome.status is DeployStatus.FAILED
    assert {result.status for result in outcome.environments} == {DeployResultStatus.FAILED}
    assert files_in(elsewhere / "environments") == []


async def test_a_folder_symlink_leaving_the_deployment_is_not_written_through(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    folder = staging_folder(h)
    folder.rmdir()
    folder.symlink_to(outside)
    outcome = await staging_run(h)
    assert {result.status for result in outcome.environments} == {DeployResultStatus.FAILED}
    assert {result.message for result in outcome.environments} == {
        "The target path leaves the deployment folder."
    }
    assert files_in(outside) == []


async def test_a_swap_to_a_symlink_between_validation_and_write_writes_nothing_outside(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("UNTOUCHED\n", encoding="utf-8")
    folder = staging_folder(h)
    (folder / ".env.web.tuning").write_text("OLD=1\n", encoding="utf-8")
    staging_id = h.seed.deployments.fancy_staging.id
    await set_deployment(h.db, staging_id, drift_policy=DriftPolicy.OVERWRITE)
    swapped: list[str] = []

    def swap_then_write(
        location: TargetLocation, allowed: AllowedPaths, data: bytes, **kwargs: Any
    ) -> str | None:
        """Validation has passed: replace the folder, or the file, by a symlink, then write."""
        if location.name == ".env.web.tuning":
            (folder / ".env.web.tuning").rename(folder / "tuning-original")
            (folder / ".env.web.tuning").symlink_to(victim)
            swapped.append("file")
        else:
            folder.rename(folder.with_name("environments-original"))
            folder.symlink_to(outside)
            swapped.append("folder")
        return write_target_file(location, allowed, data, **kwargs)

    monkeypatch.setattr(deploy_module, "write_target_file", swap_then_write)
    outcome = await staging_run(h, environments=[h.seed.figures.tuning])
    assert swapped == ["file"]
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.FAILED
    assert result.message == "The target is a symbolic link and is not replaced."
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"

    folder.joinpath(".env.web.tuning").unlink()
    (folder / "tuning-original").rename(folder / ".env.web.tuning")
    outcome = await staging_run(h, environments=[h.seed.envs.staging_web])
    assert swapped == ["file", "folder"]
    (result,) = outcome.environments
    assert result.status is DeployResultStatus.FAILED
    assert files_in(outside) == ["victim"]
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"


async def test_a_failed_environment_does_not_hold_back_the_others_but_stops_post_deploy(
    harness: Harness,
) -> None:
    h = harness
    await set_environment(h.db, h.seed.figures.tuning.id, target_path="environments/../escape")
    outcome = await staging_run(
        h, environments=[h.seed.figures.tuning, h.seed.envs.staging_web, h.seed.figures.ca]
    )
    statuses = {result.name: result.status for result in outcome.environments}
    assert statuses == {
        ".env.web": DeployResultStatus.WRITTEN,
        ".env.web.tuning": DeployResultStatus.FAILED,
        "web-ca.pem": DeployResultStatus.WRITTEN,
    }
    assert outcome.status is DeployStatus.FAILED
    assert files_in(staging_folder(h)) == [".env.web", "web-ca.pem"]
    assert not (h.roots["staging"] / "escape").exists()
    assert h.runner.requests == []
    assert outcome.post_deploy.status is PostDeployStatus.SKIPPED
    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None and run.post_deploy_reason == outcome.post_deploy.reason
    assert run.status is DeployStatus.FAILED


async def test_runs_are_not_recorded_for_requests_that_were_refused(harness: Harness) -> None:
    h = harness
    runs_before = (await h.db.execute(select(func.count()).select_from(DeployRun))).scalar_one()
    audits_before = len(await audit_of(h.db, "service.deploy"))
    with pytest.raises(DeployRequestError):
        await h.deploy(h.seed.services.staging_web.id, user="viewer")
    runs_after = (await h.db.execute(select(func.count()).select_from(DeployRun))).scalar_one()
    assert runs_after == runs_before
    assert len(await audit_of(h.db, "service.deploy")) == audits_before
