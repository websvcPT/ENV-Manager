"""The deploy dialog service: the pre-flight from a real dry run and the result of a deploy."""

from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import (
    DistMissingPolicy,
    Environment,
    HookType,
    PostDeployStatus,
    ServiceHook,
)
from app.services.audit import AuditActor
from app.services.deploy_dialog import (
    NO_COMMAND_TEXT,
    NO_SELECTION_TEXT,
    NOT_DEPLOYED_REASON,
    DeployResultView,
    Preflight,
    PreflightRow,
    build_result,
    run_preflight,
    selected_names,
)
from app.services.deploy_lock import hold_deploy_lock
from app.services.deploy_types import DeployRequest, DeployRequestError
from app.services.docker_cli import DockerCli
from app.services.hook_runner import NOT_CHANGED_REASON
from app.services.hooks import PostDeployResult
from app.services.service_detail import ServiceHeader, load_service_header
from app.tests.deploy_helpers import Harness, RecordingRunner, set_environment
from app.tests.dist_helpers import link_dist, write_dist
from app.tests.fake_docker import FakeDocker

TUNING_DIST = "environments/.env.web.tuning-dist"


def folder_of(h: Harness) -> Path:
    """The environments folder of the staging deployment."""
    return h.roots["staging"] / "environments"


async def header_for(h: Harness, user: str, service_id: int) -> ServiceHeader:
    """The service as a seeded user sees it."""
    header = await load_service_header(h.db, await h.scope_of(user), service_id)
    assert header is not None
    return header


async def preflight_of(
    h: Harness,
    *,
    user: str = "admin",
    service_id: int | None = None,
    cli: DockerCli | None = None,
    selected: set[int] | None = None,
    versions: dict[int, int] | None = None,
    force: bool = False,
    tmp_path: Path | None = None,
) -> Preflight:
    """The pre-flight of a service (staging web by default) as a seeded user."""
    service = service_id or h.seed.services.staging_web.id
    docker = cli or FakeDocker((tmp_path or Path("/nonexistent")) / "docker").cli(mounted=False)
    return await run_preflight(
        h.db,
        h.engine,
        docker,
        await h.scope_of(user),
        AuditActor.for_user(getattr(h.seed.users, user)),
        await header_for(h, user, service),
        selected_ids=selected,
        versions=versions or {},
        force=force,
    )


def row_of(preflight: Preflight, name: str) -> PreflightRow:
    """The row of a file by name."""
    return next(row for row in preflight.rows if row.name == name)


async def unlink_dists(h: Harness) -> None:
    """Take the dist links off every staging file so no check depends on a missing template."""
    for environment in (
        h.seed.envs.staging_web,
        h.seed.figures.tuning,
        h.seed.figures.replica,
        h.seed.figures.ca,
    ):
        await link_dist(h.db, environment, None)


async def test_the_preflight_lists_every_file_in_scope_with_the_dry_run_outcome(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    preflight = await preflight_of(h, tmp_path=tmp_path)

    assert [row.name for row in preflight.rows] == sorted(row.name for row in preflight.rows)
    assert {row.name for row in preflight.rows} == {
        ".env.web",
        ".env.web.tuning",
        ".env.web.replica",
        "web-ca.pem",
    }
    assert all(row.badge == "proceed" for row in preflight.rows)
    tuning = row_of(preflight, ".env.web.tuning")
    assert (tuning.version, tuning.newest) == (2, 2)
    assert tuning.dist_text == "Skipped, no dist file linked"
    assert tuning.drift_text == "Drift: Target missing"
    assert row_of(preflight, "web-ca.pem").dist_text == "Skipped, not env format"


async def test_pending_and_never_deployed_files_are_preselected_by_default(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    preflight = await preflight_of(h, tmp_path=tmp_path)

    chosen = {row.name for row in preflight.rows if row.selected}
    assert chosen == {".env.web.tuning", "web-ca.pem"}
    assert preflight.selected == 2
    assert preflight.summary == (
        "Dry run: 2 environments would be written, 0 warnings, 0 blocked. "
        "Nothing has been changed yet."
    )


async def test_an_explicit_choice_replaces_the_default_and_none_is_allowed(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    web = h.seed.envs.staging_web
    only_web = await preflight_of(h, selected={web.id}, tmp_path=tmp_path)
    assert [row.name for row in only_web.rows if row.selected] == [".env.web"]
    assert "1 environment would be written" in only_web.summary

    nothing = await preflight_of(h, selected=set(), tmp_path=tmp_path)
    assert nothing.selected == 0
    assert nothing.summary == NO_SELECTION_TEXT
    assert nothing.preview.state == "idle"


async def test_a_dry_run_is_recorded_and_changes_nothing_on_disk(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    await preflight_of(h, tmp_path=tmp_path)
    assert list(folder_of(h).iterdir()) == []
    assert h.runner.requests == []


async def test_missing_dist_keys_read_as_will_block_and_force_turns_it_into_a_warning(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    write_dist(h.roots["staging"], TUNING_DIST, "WORKERS=\nEXTRA_KEY=\n")
    await link_dist(h.db, h.seed.figures.tuning, TUNING_DIST)

    blocked = await preflight_of(h, tmp_path=tmp_path)
    tuning = row_of(blocked, ".env.web.tuning")
    assert (tuning.badge, tuning.dist_text) == ("block", "1 missing key, policy: block")
    assert "1 blocked" in blocked.summary

    forced = await preflight_of(h, force=True, tmp_path=tmp_path)
    tuning = row_of(forced, ".env.web.tuning")
    assert (tuning.badge, tuning.badge_label) == ("warnout", None)
    assert forced.force is True
    assert "1 warning" in forced.summary


async def test_the_warn_policy_reads_as_will_warn(harness: Harness, tmp_path: Path) -> None:
    h = harness
    await unlink_dists(h)
    write_dist(h.roots["staging"], TUNING_DIST, "WORKERS=\nEXTRA_KEY=\nOTHER_KEY=\n")
    await link_dist(h.db, h.seed.figures.tuning, TUNING_DIST, policy=DistMissingPolicy.WARN)

    tuning = row_of(await preflight_of(h, tmp_path=tmp_path), ".env.web.tuning")
    assert tuning.badge == "warnout"
    assert tuning.dist_text == "2 missing keys, policy: warn"


async def test_a_drifted_target_reads_as_will_halt_and_an_identical_one_as_unchanged(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    (folder_of(h) / ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")
    (folder_of(h) / ".env.web").write_text("# placeholder\n", encoding="utf-8")
    web = h.seed.envs.staging_web
    await h.deploy(h.seed.services.staging_web.id, environments=[web], force=True)

    preflight = await preflight_of(h, tmp_path=tmp_path)
    tuning = row_of(preflight, ".env.web.tuning")
    assert (tuning.badge, tuning.drift_text) == (
        "halt",
        "Drift: Target changed outside the app",
    )
    assert row_of(preflight, ".env.web").badge == "unchanged"
    assert "1 halted" in preflight.summary


async def test_a_file_that_cannot_be_deployed_reads_as_will_fail_with_its_reason(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    await set_environment(
        h.db, h.seed.figures.tuning.id, target_path="no-such-folder/.env.web.tuning"
    )

    tuning = row_of(await preflight_of(h, tmp_path=tmp_path), ".env.web.tuning")
    assert (tuning.badge, tuning.badge_label) == ("failed", "Will fail")
    assert tuning.problem


async def test_a_chosen_version_is_the_only_selected_file_and_is_noted_when_older(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    tuning = h.seed.figures.tuning
    preflight = await preflight_of(h, versions={tuning.id: 1}, tmp_path=tmp_path)

    assert [row.name for row in preflight.rows if row.selected] == [".env.web.tuning"]
    row = row_of(preflight, ".env.web.tuning")
    assert (row.version, row.newest) == (1, 2)
    assert preflight.older_version == (
        "Deploying version 1 of .env.web.tuning, which is not the newest version (v2)."
    )
    other = row_of(preflight, ".env.web")
    assert other.version == other.newest


async def test_an_unknown_version_is_refused(harness: Harness, tmp_path: Path) -> None:
    h = harness
    with pytest.raises(DeployRequestError):
        await preflight_of(h, versions={h.seed.figures.tuning.id: 40}, tmp_path=tmp_path)


async def test_a_scoped_user_sees_only_the_files_in_scope(harness: Harness, tmp_path: Path) -> None:
    h = harness
    preflight = await preflight_of(
        h, user="env_scoped_admin", service_id=h.seed.services.mysql.id, tmp_path=tmp_path
    )
    assert [row.name for row in preflight.rows] == [".env.db"]


async def test_a_user_who_may_deploy_nothing_gets_no_preflight(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    for user in ("viewer", "env_scoped_viewer"):
        with pytest.raises(DeployRequestError):
            await preflight_of(h, user=user, service_id=h.seed.services.mysql.id, tmp_path=tmp_path)


async def test_the_post_deploy_preview_runs_once_when_docker_is_usable(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    cli = FakeDocker(tmp_path / "docker").cli()

    preview = (await preflight_of(h, cli=cli, user="superadmin")).preview
    assert preview.state == "run"
    assert preview.command == "docker compose up -d --no-deps {service_name}"

    admin = (await preflight_of(h, cli=cli, user="admin")).preview
    assert admin.state == "run" and admin.command is None


async def test_the_preview_is_skipped_with_the_reason_when_docker_is_not_usable(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    preview = (await preflight_of(h, user="superadmin", tmp_path=tmp_path)).preview
    assert preview.state == "skipped"
    assert preview.reason
    assert preview.command is not None


async def test_the_preview_is_skipped_when_a_chosen_file_would_not_be_written(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    (folder_of(h) / ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")
    cli = FakeDocker(tmp_path / "docker").cli()
    preview = (await preflight_of(h, cli=cli)).preview
    assert (preview.state, preview.reason) == ("skipped", NOT_DEPLOYED_REASON)


async def test_the_preview_is_skipped_when_nothing_changes_and_the_command_runs_only_then(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    web = h.seed.envs.staging_web
    await h.deploy(h.seed.services.staging_web.id, environments=[web])
    cli = FakeDocker(tmp_path / "docker").cli()

    preview = (await preflight_of(h, cli=cli, selected={web.id})).preview
    assert (preview.state, preview.reason) == ("skipped", NOT_CHANGED_REASON)

    forced = (await preflight_of(h, cli=cli, selected={web.id}, force=True)).preview
    assert forced.state == "run"


async def test_a_service_without_a_command_has_no_post_deploy_preview(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    await unlink_dists(h)
    hook = await h.db.scalar(
        select(ServiceHook).where(
            ServiceHook.service_id == h.seed.services.staging_web.id,
            ServiceHook.type == HookType.POST_DEPLOY,
        )
    )
    assert hook is not None
    await h.db.delete(hook)
    await h.db.commit()
    preview = (await preflight_of(h, tmp_path=tmp_path)).preview
    assert (preview.state, preview.reason, preview.command) == ("none", NO_COMMAND_TEXT, None)


async def deploy_view(
    h: Harness,
    environments: list[Environment],
    *,
    user: str = "admin",
    force: bool = False,
    versions: dict[int, int] | None = None,
) -> DeployResultView:
    """Deploy files of staging web as a seeded user and build the result view."""
    request = DeployRequest(
        service_id=h.seed.services.staging_web.id,
        environment_ids=tuple(environment.id for environment in environments),
        versions=versions or {},
        force=force,
    )
    outcome = await h.engine.run(
        h.db,
        await h.scope_of(user),
        AuditActor.for_user(getattr(h.seed.users, user)),
        request,
    )
    return await build_result(
        h.db, await header_for(h, user, h.seed.services.staging_web.id), outcome
    )


async def test_a_clean_deploy_reads_as_success_with_the_post_deploy_run(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    view = await deploy_view(h, [h.seed.figures.tuning, h.seed.figures.ca])

    assert (view.tone, view.title) == ("ok", "Success")
    assert view.message == "2 environments written. The post-deploy command ran once."
    assert [(row.name, row.badge) for row in view.rows] == [
        (".env.web.tuning", "success"),
        ("web-ca.pem", "success"),
    ]
    assert view.rows[0].detail == "Created environments/.env.web.tuning"
    assert (view.post_deploy.badge, view.post_deploy.text) == ("ran", "Ran once · exit code 0")
    assert view.post_deploy.output == "done"
    assert view.audit_entry_id is not None
    assert not view.retry and view.drift_environment is None and view.dist_environment is None


async def test_a_deploy_of_matching_files_says_nothing_needed_writing(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    web = h.seed.envs.staging_web
    await deploy_view(h, [web])
    view = await deploy_view(h, [web])
    assert view.message.startswith("Nothing needed writing: 1 environment already matched.")
    assert view.rows[0].badge == "unchanged"


async def test_a_halted_deploy_names_the_drifted_file_and_links_the_drift_view(
    harness: Harness,
) -> None:
    h = harness
    await unlink_dists(h)
    (folder_of(h) / ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")
    tuning = h.seed.figures.tuning
    view = await deploy_view(h, [tuning, h.seed.figures.ca])

    assert (view.tone, view.title) == ("warn", "Halted: drift")
    assert view.message == (
        ".env.web.tuning changed outside the app. The drift policy is halt, so it was not "
        "written. 1 other environment written."
    )
    assert view.drift_environment == tuning.id
    assert next(row for row in view.rows if row.name == ".env.web.tuning").badge == "halted"
    assert view.post_deploy.badge == "skipped"
    assert view.post_deploy.text == "Not run because an environment was not deployed."


async def test_a_blocked_deploy_names_the_missing_keys_and_links_the_dist_report(
    harness: Harness,
) -> None:
    h = harness
    await unlink_dists(h)
    write_dist(h.roots["staging"], TUNING_DIST, "WORKERS=\nA_KEY=\nB_KEY=\n")
    tuning = h.seed.figures.tuning
    await link_dist(h.db, tuning, TUNING_DIST)
    view = await deploy_view(h, [tuning])

    assert (view.tone, view.title) == ("err", "Blocked: missing keys")
    assert view.message == (
        ".env.web.tuning is missing 2 keys from its dist file. Policy is block. "
        "Nothing was written."
    )
    assert view.rows[0].detail == "Missing A_KEY, B_KEY"
    assert view.dist_environment == tuning.id


async def test_a_failed_deploy_says_why_and_offers_a_retry(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    tuning = h.seed.figures.tuning
    await set_environment(h.db, tuning.id, target_path="no-such-folder/.env.web.tuning")
    view = await deploy_view(h, [tuning])

    assert (view.tone, view.title) == ("err", "Failed")
    assert view.message.startswith(".env.web.tuning could not be written. ")
    assert view.rows[0].badge == "failed" and view.retry


async def test_a_failed_post_deploy_command_reads_as_deployed_post_deploy_failed(
    harness: Harness,
) -> None:
    h = harness
    await unlink_dists(h)
    h.runner.result = PostDeployResult(
        PostDeployStatus.FAILED,
        exit_code=1,
        output="Error response from daemon",
        reason="The command exited with code 1.",
    )
    view = await deploy_view(h, [h.seed.figures.tuning])

    assert (view.tone, view.title) == ("warn", "Deployed, post-deploy failed")
    assert view.message == (
        "1 environment written. The post-deploy command exited with code 1, so the workload "
        "may still run the old files."
    )
    assert view.post_deploy.badge == "failed"
    assert view.post_deploy.text == "Failed · exit code 1"
    assert view.post_deploy.output == "Error response from daemon"


async def test_a_refused_deploy_reads_as_failed_without_an_audit_link(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    with hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        view = await deploy_view(h, [h.seed.figures.tuning])
    assert (view.tone, view.title) == ("err", "Failed")
    assert view.message.startswith("Another deploy is running")
    assert view.rows == () and view.audit_entry_id is None and view.retry


async def test_a_skipped_command_is_explained_in_the_success_banner(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    h.runner.result = PostDeployResult(PostDeployStatus.SKIPPED, reason="Docker is not available.")
    view = await deploy_view(h, [h.seed.figures.tuning])
    assert view.message == (
        "1 environment written. The post-deploy command was skipped. Docker is not available."
    )
    assert (view.post_deploy.badge, view.post_deploy.text) == (
        "skipped",
        "Docker is not available.",
    )


async def test_a_deploy_of_a_pinned_version_writes_that_version(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    tuning = h.seed.figures.tuning
    view = await deploy_view(h, [tuning], versions={tuning.id: 1})
    assert view.rows[0].version == 1
    assert (folder_of(h) / ".env.web.tuning").read_text(encoding="utf-8") == "WORKERS=2\n"


async def test_a_forced_deploy_notes_what_was_overridden_and_the_backup(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    (folder_of(h) / ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")
    view = await deploy_view(h, [h.seed.figures.tuning], force=True)
    assert view.title == "Success"
    assert view.rows[0].detail == (
        "Written to environments/.env.web.tuning, previous file backed up, forced past the checks"
    )


async def test_the_selected_names_refuse_a_file_the_caller_may_not_deploy(
    harness: Harness,
) -> None:
    h = harness
    mysql = h.seed.services.mysql.id
    header = await header_for(h, "env_scoped_admin", mysql)
    scope = await h.scope_of("env_scoped_admin")
    names = await selected_names(h.db, scope, header, {h.seed.envs.db.id})
    assert names == [".env.db"]
    with pytest.raises(DeployRequestError):
        await selected_names(h.db, scope, header, {h.seed.envs.db_secrets.id})
    with pytest.raises(DeployRequestError):
        await selected_names(h.db, scope, header, {999999})


async def test_the_result_holds_no_output_for_a_group_that_may_not_read_it(
    harness: Harness,
) -> None:
    h = harness
    await unlink_dists(h)
    h.engine.hook_runner = RecordingRunner(
        PostDeployResult(PostDeployStatus.RAN, exit_code=0, output="secret")
    )
    service_id = h.seed.services.staging_web.id
    outcome = await h.engine.run(
        h.db,
        await h.scope_of("admin"),
        AuditActor.for_user(h.seed.users.admin),
        DeployRequest(service_id=service_id, environment_ids=(h.seed.figures.tuning.id,)),
    )
    admin_view = await build_result(h.db, await header_for(h, "admin", service_id), outcome)
    assert admin_view.post_deploy.output == "secret"

    viewer_header = await header_for(h, "viewer", service_id)
    assert viewer_header.can_view_output is False
    viewer_view = await build_result(h.db, viewer_header, outcome)
    assert viewer_view.post_deploy.output is None
    assert viewer_view.post_deploy.text == "Ran once"


async def test_a_halted_deploy_that_wrote_nothing_does_not_repeat_itself(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    (folder_of(h) / ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")
    view = await deploy_view(h, [h.seed.figures.tuning])
    assert view.message == (
        ".env.web.tuning changed outside the app. The drift policy is halt, so it was not written."
    )


async def test_a_failed_command_names_which_of_several_commands_failed(harness: Harness) -> None:
    h = harness
    await unlink_dists(h)
    h.runner.result = PostDeployResult(
        PostDeployStatus.FAILED, exit_code=1, reason="Command 2 of 2 exited with code 1."
    )
    view = await deploy_view(h, [h.seed.figures.tuning])
    assert view.post_deploy.text == "Failed · exit code 1 · Command 2 of 2 exited with code 1."
