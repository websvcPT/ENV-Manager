"""The read-only docker query: stdout only, a fixed environment, bounded, and fixed failures."""

import shutil
import time
from pathlib import Path

import pytest

from app.services.docker_cli import CLI_MISSING_REASON, DAEMON_UNREACHABLE_REASON, DockerCli
from app.services.docker_query import (
    FAILED_MESSAGE,
    TIMED_OUT_MESSAGE,
    TOO_LARGE_MESSAGE,
    DockerQueryError,
    QueryFailure,
    run_docker_query,
)
from app.tests.fake_docker import FakeDocker
from app.tests.test_hook_process import wait_until_gone


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker that prints the arguments it ran with."""
    return FakeDocker(tmp_path / "fake")


async def test_standard_output_is_returned_and_a_warning_line_stays_out_of_it(
    fake: FakeDocker,
) -> None:
    fake.behavior("echo 'WARN[0000] the variable X is not set' >&2\necho '[{\"Name\": \"web\"}]'")
    text = await run_docker_query(fake.cli(), ["compose", "ls", "--format", "json"])
    assert text == '[{"Name": "web"}]\n'


async def test_the_command_runs_in_the_scratch_folder_with_the_fixed_environment(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", "csrf-material-0123456789")
    cli = fake.cli()
    await run_docker_query(cli, ["ps", "-a", "--format", "{{.ID}}"])
    (call,) = fake.calls()
    assert call.arguments == ("ps", "-a", "--format", "{{.ID}}")
    assert call.working_directory == str(cli.scratch)
    fixed = cli.environment()
    assert {key: call.environment.get(key) for key in fixed} == fixed
    assert "ENVMANAGER_CSRF_SECRET" not in call.environment
    assert "COMPOSE_EXPERIMENTAL_GIT_REMOTE" not in call.environment


async def test_a_compose_command_runs_with_the_remote_sources_off(fake: FakeDocker) -> None:
    await run_docker_query(fake.cli(), ["compose", "ls"])
    (call,) = fake.calls()
    assert call.environment["COMPOSE_EXPERIMENTAL_GIT_REMOTE"] == "false"
    assert call.environment["COMPOSE_EXPERIMENTAL_OCI_REMOTE"] == "false"


async def test_output_of_exactly_the_limit_is_returned_whole(fake: FakeDocker) -> None:
    fake.behavior("head -c 500 /dev/zero | tr '\\0' x")
    text = await run_docker_query(fake.cli(), ["ps"], max_bytes=500)
    assert text == "x" * 500


async def test_output_past_the_limit_is_an_error_and_never_truncated(fake: FakeDocker) -> None:
    pid_file = fake.folder / "child.pid"
    fake.behavior(f"sleep 300 & echo $! > {pid_file}\nhead -c 501 /dev/zero | tr '\\0' x\nwait")
    started = time.monotonic()
    with pytest.raises(DockerQueryError) as caught:
        await run_docker_query(fake.cli(), ["ps"], max_bytes=500)
    assert caught.value.failure is QueryFailure.TOO_LARGE
    assert str(caught.value) == TOO_LARGE_MESSAGE
    assert time.monotonic() - started < 10
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_a_command_that_runs_past_the_deadline_is_killed_with_its_group(
    fake: FakeDocker,
) -> None:
    pid_file = fake.folder / "child.pid"
    fake.behavior(f"sleep 300 & echo $! > {pid_file}\nwait")
    started = time.monotonic()
    with pytest.raises(DockerQueryError) as caught:
        await run_docker_query(fake.cli(), ["ps"], timeout=1.0)
    assert caught.value.failure is QueryFailure.TIMED_OUT
    assert str(caught.value) == TIMED_OUT_MESSAGE
    assert time.monotonic() - started < 10
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_a_failing_command_gives_a_fixed_sentence_without_its_own_text(
    fake: FakeDocker,
) -> None:
    fake.behavior("echo 'password=hunter2 leaked' >&2\necho partial\nexit 3")
    with pytest.raises(DockerQueryError) as caught:
        await run_docker_query(fake.cli(), ["ps"])
    assert caught.value.failure is QueryFailure.FAILED
    assert str(caught.value) == FAILED_MESSAGE and "hunter2" not in str(caught.value)


async def test_a_daemon_that_does_not_answer_gets_its_own_sentence(fake: FakeDocker) -> None:
    fake.behavior(
        "echo 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock.' >&2\nexit 1"
    )
    with pytest.raises(DockerQueryError) as caught:
        await run_docker_query(fake.cli(), ["ps"])
    assert caught.value.failure is QueryFailure.DAEMON
    assert str(caught.value) == DAEMON_UNREACHABLE_REASON


async def test_a_missing_docker_command_is_reported(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    cli = DockerCli(socket_path=fake.socket_path, scratch=fake.folder / "scratch")
    with pytest.raises(DockerQueryError) as caught:
        await run_docker_query(cli, ["ps"])
    assert caught.value.failure is QueryFailure.MISSING
    assert str(caught.value) == CLI_MISSING_REASON


async def test_a_docker_command_that_cannot_start_is_reported_as_missing(
    fake: FakeDocker, tmp_path: Path
) -> None:
    cli = DockerCli(
        executable=str(tmp_path / "absent"),
        socket_path=fake.socket_path,
        scratch=fake.folder / "scratch",
    )
    with pytest.raises(DockerQueryError) as caught:
        await run_docker_query(cli, ["ps"])
    assert caught.value.failure is QueryFailure.MISSING


async def test_output_that_is_not_utf8_is_decoded_with_replacement(fake: FakeDocker) -> None:
    fake.behavior("printf 'a\\377b\\n'")
    assert await run_docker_query(fake.cli(), ["ps"]) == "a�b\n"
