"""The post-deploy runner: when it runs, what it runs, where, and with which environment."""

import asyncio
import base64
import json
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
    LOGINS_NOT_WRITTEN_REASON,
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
from app.services.registries import RegistryLogin
from app.tests.fake_docker import Call, FakeDocker
from app.tests.test_hook_process import wait_until_gone

COMMAND = "docker compose up -d --no-deps --force-recreate api"
APP_ENVIRONMENT = {
    "ENVMANAGER_KEY": "key-material-0123456789",
    "ENVMANAGER_CSRF_SECRET": "csrf-material-0123456789",
    "ENVMANAGER_SENTRY_DSN": "https://secret@sentry.invalid/1",
    "UNRELATED_PARENT_VARIABLE": "parent-value",
    "DOCKER_HOST": "tcp://elsewhere.invalid:2375",
}
TOKEN = "dckr_pat_Zq81-secret.token_value"
HUB_TOKEN = "ghp_hub-token-4471"
LOGINS = (
    RegistryLogin("registry.example.com", "bot", TOKEN),
    RegistryLogin("docker.io", "hub-user", HUB_TOKEN),
)
SHELL_ADDED = {"PWD", "OLDPWD", "SHLVL", "_"}


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
        scrubber: OutputScrubber | None = None,
        logins: tuple[RegistryLogin, ...] = (),
    ) -> PostDeployRequest:
        """A request for the service; ``commands`` None means the service has no hook."""
        hook = (
            None
            if commands is None
            else PostDeployHook(commands, working_directory, timeout, only_if_changed)
        )
        return PostDeployRequest(
            service_id=1,
            deployment_id=1,
            deployment_root=root or self.root,
            hook=hook,
            changed=changed,
            force=force,
            allowed=self.allowed,
            scrubber=scrubber or OutputScrubber(),
            registry_logins=logins,
        )

    @property
    def scratch(self) -> Path:
        """The scratch folder of the docker CLI, where per-run folders are made."""
        return self.fake.folder / "scratch"

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


async def test_the_command_runs_once_in_the_deployment_root_as_stored(
    setup: Setup,
) -> None:
    result = await setup.run()
    assert result.status is PostDeployStatus.RAN and result.exit_code == 0
    assert result.output == "ran compose up -d --no-deps --force-recreate api\n"
    (call,) = setup.fake.calls()
    assert call.arguments == ("compose", "up", "-d", "--no-deps", "--force-recreate", "api")
    assert call.working_directory == str(setup.root)


async def test_names_that_used_to_be_placeholders_reach_docker_as_typed(setup: Setup) -> None:
    command = "docker run --label d={deployment_name} --label p={deployment_path} {service_name}"
    await setup.run(command)
    (call,) = setup.fake.calls()
    assert call.arguments == (
        "run",
        "--label",
        "d={deployment_name}",
        "--label",
        "p={deployment_path}",
        "{service_name}",
    )


async def test_a_quoted_argument_with_spaces_stays_one_argument(setup: Setup) -> None:
    await setup.run("docker run --name 'a stack' --label \"x=y z\" alpine")
    (call,) = setup.fake.calls()
    assert call.arguments == ("run", "--name", "a stack", "--label", "x=y z", "alpine")


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
        "docker compose down",
        "docker system prune -af",
        "docker compose rm -sfv api",
        "docker ps\n" + "docker ps\n" * 10,
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


@pytest.mark.parametrize(
    ("commands", "rule"),
    [
        ("docker compose down", "docker compose down is not allowed"),
        ("docker system prune -af", "prune commands are not allowed"),
        ("docker ps\n\ndocker compose rm -sfv api", "Line 3: the -v and --volumes options of rm"),
        ("docker ps\n" + "docker ps\n" * 10, "Use at most 10 commands"),
        ("docker ps\ndocker login registry.example.com", "Line 2: docker login and docker logout"),
        ("docker -H tcp://host:2375 logout", "Line 1: docker login and docker logout"),
        ("docker --config=/x compose up -d", "Line 1: the docker --config option is not allowed"),
    ],
)
async def test_a_stored_command_that_breaks_the_safety_net_fails_with_the_rule_and_runs_nothing(
    setup: Setup, commands: str, rule: str
) -> None:
    result = await setup.run(commands)
    assert result.status is PostDeployStatus.FAILED and result.exit_code is None
    assert result.reason is not None
    assert result.reason.startswith(f"{REFUSED_REASON} ") and rule in result.reason
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
    command = "docker secret-subcommand-xyz api"
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


def without_shell_variables(call: Call) -> dict[str, str]:
    """The environment a call recorded, minus the variables the shell added itself."""
    return {name: value for name, value in call.environment.items() if name not in SHELL_ADDED}


def credentials(username: str, token: str) -> str:
    """The ``auth`` value Docker keeps for a login, written out here and not by the app."""
    return base64.b64encode(f"{username}:{token}".encode()).decode("ascii")


EXPECTED_CONFIG = {
    "auths": {
        "registry.example.com": {"auth": credentials("bot", TOKEN)},
        "https://index.docker.io/v1/": {"auth": credentials("hub-user", HUB_TOKEN)},
    }
}


async def test_a_run_with_logins_gets_a_private_config_folder_inside_the_scratch_folder(
    setup: Setup,
) -> None:
    setup.fake.capture_config()
    result = await setup.run(logins=LOGINS)
    assert result.status is PostDeployStatus.RAN
    (call,) = setup.fake.calls()
    folder = Path(call.environment["DOCKER_CONFIG"])
    assert folder.parent == setup.scratch and folder != setup.scratch
    assert folder.name.startswith("run-")
    environment = without_shell_variables(call)
    assert environment == {**setup.runner.cli.environment(), "DOCKER_CONFIG": str(folder)}


async def test_the_config_file_holds_every_login_and_is_private_while_the_command_runs(
    setup: Setup,
) -> None:
    setup.fake.capture_config()
    await setup.run(logins=LOGINS)
    captured = setup.fake.captured_config()
    assert captured is not None
    assert captured.content == EXPECTED_CONFIG
    assert captured.file_mode == 0o600 and captured.folder_mode == 0o700


async def test_every_command_of_the_hook_runs_with_the_same_folder_and_logins(
    setup: Setup,
) -> None:
    setup.fake.capture_config()
    result = await setup.run("docker one\ndocker two", logins=LOGINS)
    assert result.status is PostDeployStatus.RAN
    first, second = setup.fake.calls()
    assert first.environment["DOCKER_CONFIG"] == second.environment["DOCKER_CONFIG"]
    assert setup.fake.captured_config(1) == setup.fake.captured_config(2)
    assert setup.fake.captured_config(2) is not None


@pytest.mark.parametrize(
    ("ending", "timeout", "status"),
    [
        ("exit 0", 30, PostDeployStatus.RAN),
        ("exit 3", 30, PostDeployStatus.FAILED),
        ("sleep 300", 1, PostDeployStatus.FAILED),
    ],
    ids=["success", "non-zero exit", "timeout"],
)
async def test_the_config_folder_is_removed_when_the_run_ends(
    setup: Setup, ending: str, timeout: int, status: PostDeployStatus
) -> None:
    setup.fake.capture_config(ending)
    result = await setup.run(logins=LOGINS, timeout=timeout)
    assert result.status is status
    assert setup.fake.captured_config() is not None, "the file was there while the command ran"
    (call,) = setup.fake.calls()
    assert not Path(call.environment["DOCKER_CONFIG"]).exists()
    assert list(setup.scratch.iterdir()) == []


async def test_a_failing_second_command_still_removes_the_config_folder(setup: Setup) -> None:
    setup.fake.capture_config('[ "$n" = 2 ] && exit 4; exit 0')
    result = await setup.run("docker one\ndocker two\ndocker three", logins=LOGINS)
    assert result.status is PostDeployStatus.FAILED and result.exit_code == 4
    assert len(setup.fake.calls()) == 2
    assert list(setup.scratch.iterdir()) == []


async def test_the_config_folder_is_removed_when_the_deploy_is_cancelled(setup: Setup) -> None:
    pid_file = setup.fake.folder / "child.pid"
    setup.fake.capture_config(f"sleep 300 & echo $! > {pid_file}; wait")
    task = asyncio.create_task(setup.run(logins=LOGINS, timeout=60))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.05)
    (call,) = setup.fake.calls()
    folder = Path(call.environment["DOCKER_CONFIG"])
    assert (folder / "config.json").is_file()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not folder.exists() and list(setup.scratch.iterdir()) == []
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_two_runs_at_the_same_time_use_two_folders_that_are_both_removed(
    setup: Setup,
) -> None:
    setup.fake.behavior(
        'echo "$DOCKER_CONFIG" >> "$dir/folders"\n'
        'cp "$DOCKER_CONFIG/config.json" "$dir/seen.$$"\n'
        'touch "$dir/started.$$"\n'
        "for _ in $(seq 1 100); do\n"
        '  [ "$(ls "$dir" | grep -c "^started\\.")" -ge 2 ] && break\n'
        "  sleep 0.05\n"
        "done"
    )
    first, second = await asyncio.gather(setup.run(logins=LOGINS), setup.run(logins=LOGINS[:1]))
    assert first.status is PostDeployStatus.RAN and second.status is PostDeployStatus.RAN
    folders = (setup.fake.folder / "folders").read_text(encoding="utf-8").splitlines()
    assert len(folders) == 2 and len(set(folders)) == 2
    assert {Path(folder).parent for folder in folders} == {setup.scratch}
    assert not any(Path(folder).exists() for folder in folders)
    assert list(setup.scratch.iterdir()) == []
    seen = [
        len(json.loads(path.read_text(encoding="utf-8"))["auths"])
        for path in setup.fake.folder.glob("seen.*")
    ]
    assert sorted(seen) == [1, 2], "each run saw the logins of its own request"


async def test_without_logins_nothing_is_written_and_the_environment_is_the_plain_one(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*arguments: object, **options: object) -> None:
        raise AssertionError("a config folder was made for a run without logins")

    monkeypatch.setattr(hook_runner, "private_docker_config", refuse)
    setup.fake.capture_config()
    result = await setup.run()
    assert result.status is PostDeployStatus.RAN
    (call,) = setup.fake.calls()
    environment = without_shell_variables(call)
    assert environment == setup.runner.cli.environment()
    assert call.environment["DOCKER_CONFIG"] == str(setup.scratch)
    assert setup.fake.captured_config() is None
    assert list(setup.scratch.iterdir()) == []


async def test_a_config_folder_that_cannot_be_made_stops_the_command_with_a_fixed_reason(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*arguments: object, **options: object) -> str:
        raise OSError(f"no room for {TOKEN} in /secret-folder")

    monkeypatch.setattr("app.services.run_config.tempfile.mkdtemp", refuse)
    caplog.set_level(logging.DEBUG)
    result = await setup.run(logins=LOGINS)
    assert result.status is PostDeployStatus.FAILED and result.exit_code is None
    assert result.reason == LOGINS_NOT_WRITTEN_REASON and result.output is None
    assert setup.fake.calls() == []
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert TOKEN not in caplog.text and "secret-folder" not in caplog.text


def test_the_request_keeps_its_logins_out_of_its_representation() -> None:
    request = PostDeployRequest(
        service_id=1,
        deployment_id=1,
        deployment_root=Path("/srv/stack"),
        hook=None,
        changed=True,
        force=False,
        allowed=AllowedPaths([Path("/srv")]),
        registry_logins=LOGINS,
    )
    assert request.registry_logins == LOGINS
    text = repr(request)
    assert "registry_logins" not in text
    assert TOKEN not in text and HUB_TOKEN not in text and "bot" not in text


def test_a_request_without_logins_holds_an_empty_tuple() -> None:
    request = PostDeployRequest(
        service_id=1,
        deployment_id=1,
        deployment_root=Path("/srv/stack"),
        hook=None,
        changed=True,
        force=False,
        allowed=AllowedPaths([Path("/srv")]),
    )
    assert request.registry_logins == ()


@pytest.mark.parametrize(
    ("commands", "only_if_changed", "changed", "force", "due"),
    [
        (COMMAND, True, True, False, True),
        (COMMAND, True, False, False, False),
        (COMMAND, True, False, True, True),
        (COMMAND, False, False, False, True),
        ("  \n ", False, True, True, False),
        ("", True, True, False, False),
    ],
)
def test_a_hook_is_due_when_it_has_a_command_and_something_changed_or_the_switch_is_off(
    commands: str, only_if_changed: bool, changed: bool, force: bool, due: bool
) -> None:
    hook = PostDeployHook(commands, None, 30, only_if_changed)
    assert hook.is_due(changed=changed, force=force) is due
    assert hook.has_command is bool(commands.strip())
