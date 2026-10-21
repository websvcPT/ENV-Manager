"""The deploy command of the CLI, against a real temporary database and a real folder."""

import asyncio
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli.main import _print_deploy as print_deploy
from app.cli.main import app
from app.config import get_settings
from app.core.db import create_engine, create_session_factory
from app.models import (
    AuditActorType,
    AuditEntry,
    Deployment,
    DeployResult,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    PostDeployStatus,
)
from app.models.types import utc_now
from app.services.deploy_lock import hold_deploy_lock
from app.services.deploy_policy import HALT_DRIFT_MESSAGE
from app.services.deploy_recovery import INTERRUPTED_REASON
from app.services.deploy_types import DeployOutcome
from app.services.hooks import PostDeployResult
from app.tests.dist_helpers import relocate_deployments
from app.tests.template_databases import SeededTemplate, copy_database

runner = CliRunner()


@pytest.fixture
def roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seeded_template: SeededTemplate,
    master_key: str,
) -> Iterator[dict[str, Path]]:
    """A seeded database in the CLI's data folder, with the deployments on a real folder."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(data_path))
    monkeypatch.setenv("ENVMANAGER_KEY", master_key)
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    monkeypatch.setenv("ENVMANAGER_DOCKER_SOCKET", str(tmp_path / "no-docker.sock"))
    get_settings.cache_clear()
    settings = get_settings()
    copy_database(seeded_template.path, settings.database_path)

    async def prepare() -> dict[str, Path]:
        engine = create_engine(settings.database_path)
        try:
            async with create_session_factory(engine)() as db:
                return await relocate_deployments(db, tmp_path / "host")
        finally:
            await engine.dispose()

    yield asyncio.run(prepare())
    get_settings.cache_clear()


def read_rows[Row: (AuditEntry, DeployRun, DeployResult)](model: type[Row]) -> list[Row]:
    """Rows of one model read back from the CLI's database."""

    async def read() -> list[Row]:
        engine = create_engine(get_settings().database_path)
        try:
            async with create_session_factory(engine)() as db:
                return list((await db.execute(select(model))).scalars())
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_deploy_writes_the_named_files_as_the_cli_actor(roots: dict[str, Path]) -> None:
    result = runner.invoke(
        app,
        [
            "deploy",
            "Fancy Proj. Name/staging/web",
            "--env",
            ".env.web.tuning",
            "--env",
            "web-ca.pem",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("deploy of Fancy Proj. Name/staging/web: success (run ")
    assert any(line.startswith("  .env.web.tuning (v2): written. Created.") for line in lines)
    assert any(line.startswith("  web-ca.pem (v1): written.") for line in lines)
    assert "  post-deploy: skipped (The Docker socket is not mounted.)" in lines
    folder = roots["staging"] / "environments"
    assert sorted(path.name for path in folder.iterdir()) == [".env.web.tuning", "web-ca.pem"]
    assert stat.S_IMODE((folder / ".env.web.tuning").stat().st_mode) == 0o600
    assert "WORKERS" not in result.output

    (run,) = [run for run in read_rows(DeployRun) if run.trigger is DeployTrigger.CLI]
    assert (run.triggered_by_user_id, run.triggered_by_token_id, run.dry_run) == (None, None, False)
    (entry,) = [entry for entry in read_rows(AuditEntry) if entry.action == "service.deploy"]
    assert (entry.actor_type, entry.actor_name) == (AuditActorType.CLI, "cli")
    results = [row for row in read_rows(DeployResult) if row.run_id == run.id]
    assert {row.environment_name for row in results} == {".env.web.tuning", "web-ca.pem"}


def test_deploy_without_file_names_deploys_every_file_of_the_service(
    roots: dict[str, Path],
) -> None:
    result = runner.invoke(app, ["deploy", "Fancy Proj. Name/staging/web"])
    assert result.exit_code == 0, result.output
    assert len(list((roots["staging"] / "environments").iterdir())) == 4


def test_a_dry_run_reports_and_writes_nothing(roots: dict[str, Path]) -> None:
    result = runner.invoke(app, ["deploy", "Fancy Proj. Name/staging/web", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith(
        "dry run of Fancy Proj. Name/staging/web: success"
    )
    assert "would_write" in result.output and "post-deploy" not in result.output
    assert list((roots["staging"] / "environments").iterdir()) == []
    (run,) = [run for run in read_rows(DeployRun) if run.trigger is DeployTrigger.CLI]
    assert run.dry_run
    actions = {entry.action for entry in read_rows(AuditEntry)}
    assert "service.deploy_dry_run" in actions


def test_a_drifted_target_halts_with_exit_code_1_and_force_overwrites_it(
    roots: dict[str, Path],
) -> None:
    target = roots["staging"] / "environments" / ".env.web"
    target.write_text("EDITED=1\n", encoding="utf-8")
    halted = runner.invoke(app, ["deploy", "Fancy Proj. Name/staging/web", "--env", ".env.web"])
    assert halted.exit_code == 1
    assert "halted" in halted.output and HALT_DRIFT_MESSAGE in halted.output
    assert target.read_text(encoding="utf-8") == "EDITED=1\n"

    forced = runner.invoke(
        app, ["deploy", "Fancy Proj. Name/staging/web", "--env", ".env.web", "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert "Forced past drift." in forced.output and "backed up" in forced.output
    assert target.read_text(encoding="utf-8") != "EDITED=1\n"
    (backup,) = [path for path in target.parent.iterdir() if ".bak-" in path.name]
    assert backup.read_text(encoding="utf-8") == "EDITED=1\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    forced_runs = [run for run in read_rows(DeployRun) if run.force]
    assert len(forced_runs) == 1


def test_a_deploy_while_the_root_is_locked_exits_with_the_refusal(roots: dict[str, Path]) -> None:
    with hold_deploy_lock(get_settings().lock_path, str(roots["staging"])):
        result = runner.invoke(app, ["deploy", "Fancy Proj. Name/staging/web"])
    assert result.exit_code == 1
    assert "Another deploy is running" in result.output
    assert list((roots["staging"] / "environments").iterdir()) == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["deploy", "Fancy Proj. Name/staging"], "name the service as project/deployment/service"),
        (["deploy", "Fancy Proj. Name//web"], "name the service as project/deployment/service"),
        (["deploy", "Fancy Proj. Name/staging/nope"], "no service Fancy Proj. Name/staging/nope"),
        (
            ["deploy", "Fancy Proj. Name/staging/web", "--env", "nope"],
            "no file nope in Fancy Proj. Name/staging/web",
        ),
    ],
)
def test_a_target_that_does_not_exist_is_an_error(
    roots: dict[str, Path], arguments: list[str], message: str
) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 1
    assert message in result.output


def test_a_failed_post_deploy_prints_its_exit_code_and_truncated_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = DeployOutcome(
        run_id=7,
        status=DeployStatus.POST_DEPLOY_FAILED,
        dry_run=False,
        force=False,
        environments=(),
        post_deploy=PostDeployResult(
            PostDeployStatus.FAILED,
            exit_code=3,
            output="line one\nline two\n",
            reason="The command exited with code 3.",
        ),
    )
    print_deploy("p/d/s", outcome)
    assert capsys.readouterr().out.splitlines() == [
        "deploy of p/d/s: post_deploy_failed (run 7)",
        "  post-deploy: failed (The command exited with code 3.)",
        "  post-deploy exit code: 3",
        "  post-deploy output (truncated):",
        "    line one",
        "    line two",
    ]


def test_a_deploy_first_closes_runs_that_a_dead_process_left_running(
    roots: dict[str, Path],
) -> None:
    async def add_running_run() -> int:
        engine = create_engine(get_settings().database_path)
        try:
            async with create_session_factory(engine)() as db:
                run = DeployRun(
                    service_name="web",
                    deployment_name="staging",
                    trigger=DeployTrigger.CLI,
                    status=DeployStatus.SUCCESS,
                    post_deploy_status=PostDeployStatus.RUNNING,
                    started_at=utc_now(),
                )
                db.add(run)
                await db.commit()
                return run.id
        finally:
            await engine.dispose()

    run_id = asyncio.run(add_running_run())
    result = runner.invoke(app, ["deploy", "Fancy Proj. Name/staging/web", "--dry-run"])
    assert result.exit_code == 0, result.output
    (closed,) = [run for run in read_rows(DeployRun) if run.id == run_id]
    assert closed.post_deploy_status is PostDeployStatus.FAILED
    assert closed.status is DeployStatus.POST_DEPLOY_FAILED
    assert closed.post_deploy_reason == INTERRUPTED_REASON


def test_a_deployment_name_with_spaces_and_capitals_is_named_on_the_command_line(
    roots: dict[str, Path],
) -> None:
    async def rename() -> None:
        engine = create_engine(get_settings().database_path)
        try:
            async with create_session_factory(engine)() as db:
                staging = await db.scalar(select(Deployment).where(Deployment.name == "staging"))
                assert staging is not None
                staging.name = "Staging EU"
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(rename())
    result = runner.invoke(app, ["deploy", "Fancy Proj. Name/Staging EU/web", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("dry run of Fancy Proj. Name/Staging EU/web")
