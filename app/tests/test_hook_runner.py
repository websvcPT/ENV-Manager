"""The post-deploy runner: when it runs, what it runs, where, and with which environment."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from app.models import PostDeployStatus
from app.services import hook_runner
from app.services.docker_cli import DAEMON_UNREACHABLE_REASON, NOT_MOUNTED_REASON, DockerCli
from app.services.hook_process import ProcessResult
from app.services.hook_runner import (
    NOT_A_FOLDER_MESSAGE,
    NOT_CHANGED_REASON,
    NOT_STARTED_REASON,
    OUTSIDE_PATHS_MESSAGE,
    REFUSED_REASON,
    DockerHookRunner,
)
from app.services.hooks import PostDeployHook, PostDeployRequest, PostDeployResult
from app.services.output_scrub import OutputScrubber
from app.services.paths import AllowedPaths
from app.tests.fake_docker import FakeDocker
from app.tests.test_hook_process import wait_until_gone

COMMAND = "docker compose up -d --no-deps --force-recreate {service_name}"
APP_ENVIRONMENT = {
    "ENVMANAGER_KEY": "key-material-0123456789",
    "ENVMANAGER_CSRF_SECRET": "csrf-material-0123456789",
    "ENVMANAGER_SENTRY_DSN": "https://secret@sentry.invalid/1",
    "UNRELATED_PARENT_VARIABLE": "parent-value",
    "DOCKER_HOST": "tcp://elsewhere.invalid:2375",
}


class Setup:
    """A fake docker, an allowed folder with a deployment root and a request builder."""

    def __init__(self, tmp_path: Path) -> None:
        """Create the folders: ``host`` is the only allowed path, ``host/stack`` the root."""
        self.fake = FakeDocker(tmp_path / "fake")
        self.host = (tmp_path / "host").resolve()
        self.root = self.host / "stack"
        self.root.mkdir(parents=True)
        self.allowed = AllowedPaths([self.host])
        self.runner = DockerHookRunner(self.fake.cli())

    def request(
        self,
        commands: str | None = COMMAND,
        *,
        changed: bool = True,
        force: bool = False,
        only_if_changed: bool = True,
        timeout: int = 30,
        working_directory: str | None = None,
        root: Path | None = None,
        deployment: str = "stack",
        service: str = "api",
        scrubber: OutputScrubber | None = None,
    ) -> PostDeployRequest:
        """A request for the service; ``commands`` None means the service has no hook."""
        hook = (
            None
            if commands is None
            else PostDeployHook(commands, working_directory, timeout, only_if_changed)
        )
        return PostDeployRequest(
            service_id=1,
            service_name=service,
            deployment_id=1,
            deployment_name=deployment,
            deployment_root=root or self.root,
            hook=hook,
            changed=changed,
            force=force,
            allowed=self.allowed,
            scrubber=scrubber or OutputScrubber(),
        )

    async def run(self, commands: str | None = COMMAND, **options: Any) -> PostDeployResult:
        """Run the request through the runner."""
        return await self.runner.run_post_deploy(self.request(commands, **options))


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    """The fake docker with its socket mounted and a deployment root."""
    return Setup(tmp_path)


async def test_a_service_without_a_command_has_nothing_to_run(setup: Setup) -> None:
    assert await setup.run(None) == PostDeployResult(PostDeployStatus.NONE)
    assert await setup.run("  \n ") == PostDeployResult(PostDeployStatus.NONE)
    assert setup.fake.calls() == []


async def test_the_command_runs_once_in_the_deployment_root_with_placeholders_filled(
    setup: Setup,
) -> None:
    result = await setup.run()
    assert result.status is PostDeployStatus.RAN and result.exit_code == 0
    assert result.output == "ran compose up -d --no-deps --force-recreate api\n"
    (call,) = setup.fake.calls()
    assert call.arguments == ("compose", "up", "-d", "--no-deps", "--force-recreate", "api")
    assert call.working_directory == str(setup.root)


async def test_every_placeholder_is_filled_with_its_validated_value(setup: Setup) -> None:
    command = "docker run --label d={deployment_name} --label p={deployment_path} {service_name}"
    await setup.run(command, service="web", deployment="stack")
    (call,) = setup.fake.calls()
    assert call.arguments == (
        "run",
        "--label",
        "d=stack",
        "--label",
        f"p={setup.root}",
        "web",
    )


async def test_a_deployment_path_with_spaces_stays_one_argument(setup: Setup) -> None:
    spaced = setup.host / "my stack"
    spaced.mkdir()
    await setup.run("docker compose --project-directory {deployment_path} up", root=spaced)
    (call,) = setup.fake.calls()
    assert call.arguments == ("compose", "--project-directory", str(spaced), "up")


async def test_a_docker_template_and_unknown_braces_reach_docker_unchanged(setup: Setup) -> None:
    await setup.run("docker ps --format {{.Names}} --filter name={unknown}")
    (call,) = setup.fake.calls()
    assert call.arguments[-3:] == ("{{.Names}}", "--filter", "name={unknown}")


async def test_commands_run_in_order_and_the_first_failure_stops_the_rest(setup: Setup) -> None:
    setup.fake.behavior('echo "step $n"; [ "$n" = 2 ] && exit 3; exit 0')
    result = await setup.run("docker one\ndocker two\ndocker three")
    assert result.status is PostDeployStatus.FAILED and result.exit_code == 3
    assert result.reason == "Command 2 of 3 exited with code 3."
    assert result.output == "[command 1 of 3]\nstep 1\n[command 2 of 3]\nstep 2\n"
    assert [call.arguments for call in setup.fake.calls()] == [("one",), ("two",)]


async def test_several_commands_that_succeed_report_the_last_exit_code(setup: Setup) -> None:
    result = await setup.run("docker one\ndocker two")
    assert result.status is PostDeployStatus.RAN and result.exit_code == 0
    assert result.output == "[command 1 of 2]\nran one\n[command 2 of 2]\nran two\n"


async def test_a_single_failing_command_reports_its_exit_code_and_output(setup: Setup) -> None:
    setup.fake.behavior('echo "no such service" >&2; exit 5')
    result = await setup.run()
    assert result.status is PostDeployStatus.FAILED and result.exit_code == 5
    assert result.reason == "The command exited with code 5."
    assert result.output == "no such service\n"


async def test_long_output_is_cut_to_the_first_and_last_four_kilobytes(setup: Setup) -> None:
    setup.fake.behavior("seq 1 6000; exit 1")
    result = await setup.run()
    assert result.output is not None
    assert result.output.startswith("1\n2\n3\n") and result.output.endswith("5999\n6000\n")
    assert "bytes omitted" in result.output and len(result.output) < 8400


async def test_a_command_that_prints_nothing_has_no_output(setup: Setup) -> None:
    setup.fake.behavior("exit 0")
    result = await setup.run()
    assert result.status is PostDeployStatus.RAN and result.output is None


async def test_nothing_changed_skips_unless_forced_or_the_setting_is_off(setup: Setup) -> None:
    skipped = await setup.run(changed=False)
    assert skipped == PostDeployResult(PostDeployStatus.SKIPPED, reason=NOT_CHANGED_REASON)
    assert setup.fake.calls() == []
    assert (await setup.run(changed=False, force=True)).status is PostDeployStatus.RAN
    assert (await setup.run(changed=False, only_if_changed=False)).status is PostDeployStatus.RAN
    assert (await setup.run(changed=True)).status is PostDeployStatus.RAN
    assert len(setup.fake.calls()) == 3


async def test_a_missing_socket_skips_with_the_reason_and_runs_nothing(
    setup: Setup, tmp_path: Path
) -> None:
    setup.runner = DockerHookRunner(
        DockerCli(executable=str(setup.fake.executable), socket_path=tmp_path / "gone.sock")
    )
    result = await setup.run()
    assert result == PostDeployResult(PostDeployStatus.SKIPPED, reason=NOT_MOUNTED_REASON)
    assert setup.fake.calls() == []


async def test_a_daemon_that_does_not_answer_skips_with_the_reason(setup: Setup) -> None:
    setup.fake.switch("no-daemon")
    result = await setup.run()
    assert result == PostDeployResult(PostDeployStatus.SKIPPED, reason=DAEMON_UNREACHABLE_REASON)
    assert setup.fake.calls() == []


async def test_availability_is_checked_again_for_every_run(setup: Setup) -> None:
    assert (await setup.run()).status is PostDeployStatus.RAN
    setup.fake.switch("no-daemon")
    assert (await setup.run()).status is PostDeployStatus.SKIPPED
    setup.fake.switch("no-daemon", on=False)
    assert (await setup.run()).status is PostDeployStatus.RAN


@pytest.mark.parametrize(
    "commands",
    [
        "sh -c 'id'",
        "docker ps | grep x",
        "docker ps && docker ps",
        "docker ps\nrm -rf x",
        "docker ps 'unclosed",
    ],
)
async def test_a_stored_command_that_is_not_valid_is_refused_when_it_runs(
    setup: Setup, commands: str
) -> None:
    result = await setup.run(commands)
    assert result.status is PostDeployStatus.FAILED and result.exit_code is None
    assert result.reason is not None and result.reason.startswith(REFUSED_REASON)
    assert setup.fake.calls() == []


async def test_a_custom_working_directory_inside_the_allowed_paths_is_used(setup: Setup) -> None:
    other = setup.host / "compose-files"
    other.mkdir()
    result = await setup.run(working_directory=str(other))
    assert result.status is PostDeployStatus.RAN
    assert setup.fake.calls()[0].working_directory == str(other)


@pytest.mark.parametrize("kind", ["outside", "relative", "traversal", "symlink", "missing", "file"])
async def test_a_working_directory_that_fails_the_check_at_run_time_is_refused(
    setup: Setup, tmp_path: Path, kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (setup.host / "a-file").write_text("x", encoding="utf-8")
    (setup.host / "escape").symlink_to(outside)
    directory = {
        "outside": str(outside),
        "relative": "stack",
        "traversal": f"{setup.host}/stack/../../outside",
        "symlink": str(setup.host / "escape"),
        "missing": str(setup.host / "not-there"),
        "file": str(setup.host / "a-file"),
    }[kind]
    result = await setup.run(working_directory=directory)
    message = NOT_A_FOLDER_MESSAGE if kind in {"missing", "file"} else OUTSIDE_PATHS_MESSAGE
    assert result.status is PostDeployStatus.FAILED
    assert result.reason == f"{REFUSED_REASON} {message}"
    assert setup.fake.calls() == []


async def test_a_deployment_root_outside_the_allowed_paths_is_refused(
    setup: Setup, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    result = await setup.run(root=outside)
    assert result.status is PostDeployStatus.FAILED and result.reason is not None
    assert result.reason.startswith(REFUSED_REASON)
    assert setup.fake.calls() == []


async def test_a_name_that_could_become_an_option_is_refused(setup: Setup) -> None:
    result = await setup.run(service="--all")
    assert result.status is PostDeployStatus.FAILED
    assert setup.fake.calls() == []


async def test_the_child_never_sees_the_environment_of_the_app(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in APP_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    result = await setup.run()
    assert result.status is PostDeployStatus.RAN
    (call,) = setup.fake.calls()
    environment = call.environment
    shell_added = {"PWD", "OLDPWD", "SHLVL", "_"}
    allowed_names = {"PATH", "HOME", "LANG", "DOCKER_CONFIG", "DOCKER_HOST"} | shell_added
    assert set(environment) <= allowed_names
    assert {"PATH", "HOME", "LANG", "DOCKER_CONFIG", "DOCKER_HOST"} <= set(environment)
    assert environment["DOCKER_HOST"] == f"unix://{setup.fake.socket_path}"
    assert not any(name in environment for name in APP_ENVIRONMENT if name != "DOCKER_HOST")
    text = "\n".join(f"{key}={value}" for key, value in environment.items())
    assert not any(value in text for value in APP_ENVIRONMENT.values())


async def test_a_timeout_kills_the_process_group_and_counts_as_failed(setup: Setup) -> None:
    pid_file = setup.fake.folder / "child.pid"
    setup.fake.behavior(f"sleep 300 & echo $! > {pid_file}; echo started; wait")
    started = time.monotonic()
    result = await setup.run(timeout=1)
    assert result.status is PostDeployStatus.FAILED and result.exit_code is None
    assert result.reason == "Timed out after 1 seconds and was stopped."
    assert result.output == "started\n"
    assert time.monotonic() - started < 15
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_one_deadline_covers_all_commands(setup: Setup) -> None:
    setup.fake.behavior("sleep 1.5")
    started = time.monotonic()
    result = await setup.run("docker one\ndocker two", timeout=2)
    assert result.status is PostDeployStatus.FAILED and result.exit_code is None
    assert result.reason == "Timed out after 2 seconds and was stopped."
    assert [call.arguments for call in setup.fake.calls()] == [("one",), ("two",)]
    assert time.monotonic() - started < 10


async def test_cancelling_the_deploy_kills_the_running_command(setup: Setup) -> None:
    pid_file = setup.fake.folder / "child.pid"
    setup.fake.behavior(f"sleep 300 & echo $! > {pid_file}; wait")
    task = asyncio.create_task(setup.run(timeout=60))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_a_command_that_cannot_be_started_fails_with_a_fixed_reason(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def refuse_to_start(*arguments: object, **options: object) -> ProcessResult:
        raise PermissionError

    monkeypatch.setattr(hook_runner, "run_process", refuse_to_start)
    caplog.set_level(logging.DEBUG)
    result = await setup.run()
    assert result.status is PostDeployStatus.FAILED and result.exit_code is None
    assert result.reason == NOT_STARTED_REASON and result.output is None
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize("situation", ["exit", "timeout", "refused", "skipped"])
async def test_expected_outcomes_never_log_at_error_level_or_leak_the_command(
    setup: Setup, caplog: pytest.LogCaptureFixture, situation: str
) -> None:
    caplog.set_level(logging.DEBUG)
    command = "docker secret-subcommand-xyz {service_name}"
    if situation == "exit":
        setup.fake.behavior("echo failed; exit 9")
    if situation == "timeout":
        setup.fake.behavior("exec sleep 300")
    if situation == "skipped":
        setup.fake.switch("no-daemon")
    if situation == "refused":
        command = "sh secret-subcommand-xyz"
    result = await setup.run(command, timeout=1)
    assert result.status is not PostDeployStatus.RAN
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "secret-subcommand-xyz" not in caplog.text
    assert "secret-subcommand-xyz" not in f"{result.reason} {result.output}"


async def test_the_scrubber_of_the_request_removes_values_from_the_output(setup: Setup) -> None:
    setup.fake.behavior('echo "password=seed-secret port=3306 pool=4"; exit 0')
    result = await setup.run(scrubber=OutputScrubber(["seed-secret", "3306", "4"]))
    assert result.status is PostDeployStatus.RAN
    assert result.output == "password=**** port=**** pool=4\n"


async def test_a_failed_command_output_is_scrubbed_too(setup: Setup) -> None:
    setup.fake.behavior('echo "bad seed-secret" >&2; exit 2')
    result = await setup.run(scrubber=OutputScrubber(["seed-secret"]))
    assert result.status is PostDeployStatus.FAILED and result.output == "bad ****\n"


async def test_a_value_split_by_the_cut_of_long_output_is_scrubbed_at_both_edges(
    setup: Setup,
) -> None:
    setup.fake.behavior(
        "head -c 4090 /dev/zero | tr '\\0' x; printf seed-secret; "
        "head -c 100 /dev/zero | tr '\\0' z; printf seed-secret; "
        "head -c 4091 /dev/zero | tr '\\0' y; exit 0"
    )
    result = await setup.run(scrubber=OutputScrubber(["seed-secret"]))
    assert result.output is not None and "bytes omitted" in result.output
    assert "seed" not in result.output and "ecret" not in result.output
    assert result.output.count("****") == 2
