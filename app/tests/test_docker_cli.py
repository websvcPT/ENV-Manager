"""The Docker status check and the environment given to child processes."""

import gc
import shutil
import time
from pathlib import Path

import pytest

from app.services import docker_cli
from app.services.docker_cli import (
    CLI_MISSING_REASON,
    COMPOSE_MISSING_REASON,
    DAEMON_UNREACHABLE_REASON,
    NOT_MOUNTED_REASON,
    DockerCli,
    DockerStatus,
)
from app.tests.fake_docker import FakeDocker

APP_SECRETS = {
    "ENVMANAGER_KEY": "key-material-0123456789",
    "ENVMANAGER_CSRF_SECRET": "csrf-material-0123456789",
    "ENVMANAGER_SENTRY_DSN": "https://secret@sentry.invalid/1",
    "UNRELATED_PARENT_VARIABLE": "parent-value",
    "DOCKER_HOST": "tcp://elsewhere.invalid:2375",
    "DOCKER_CONFIG": "/somewhere/else",
}


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker whose probes succeed."""
    return FakeDocker(tmp_path / "fake")


async def test_a_missing_socket_is_reported_without_starting_any_process(fake: FakeDocker) -> None:
    status = await fake.cli(mounted=False).status()
    assert status == DockerStatus(False, False, None, NOT_MOUNTED_REASON)
    assert not status.available
    assert not (fake.folder / "calls").exists()


async def test_a_path_that_is_not_a_socket_does_not_count_as_mounted(fake: FakeDocker) -> None:
    fake.socket_path.write_text("", encoding="utf-8")
    cli = fake.cli(mounted=False)
    assert cli.socket_mounted is False
    assert (await cli.status()).reason == NOT_MOUNTED_REASON


async def test_a_working_socket_daemon_and_compose_plugin_are_available(fake: FakeDocker) -> None:
    cli = fake.cli()
    assert cli.socket_mounted is True
    status = await cli.status()
    assert status == DockerStatus(True, True, "2.40.0", None)
    assert status.available


async def test_a_daemon_that_does_not_answer_is_reported_with_the_compose_version(
    fake: FakeDocker,
) -> None:
    fake.switch("no-daemon")
    status = await fake.cli().status()
    assert status == DockerStatus(True, False, "2.40.0", DAEMON_UNREACHABLE_REASON)
    assert not status.available


async def test_a_missing_compose_plugin_makes_the_feature_unavailable(fake: FakeDocker) -> None:
    fake.switch("no-compose")
    status = await fake.cli().status()
    assert status == DockerStatus(True, True, None, COMPOSE_MISSING_REASON)
    assert not status.available


async def test_a_missing_docker_command_is_reported(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    cli = DockerCli(socket_path=fake.socket_path, scratch=fake.folder / "scratch")
    fake.mount_socket()
    assert (await cli.status()).reason == CLI_MISSING_REASON


async def test_a_probe_that_hangs_is_stopped_and_counts_as_unreachable(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.switch("slow-daemon")
    monkeypatch.setattr(docker_cli, "PROBE_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    status = await fake.cli().status()
    assert status.reason == DAEMON_UNREACHABLE_REASON and status.mounted
    assert time.monotonic() - started < 5


class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def timed(fake: FakeDocker, clock: Clock) -> DockerCli:
    """A cli on the fake docker that reads the given clock."""
    fake.mount_socket()
    return DockerCli(
        executable=str(fake.executable),
        socket_path=fake.socket_path,
        scratch=fake.folder / "scratch",
        clock=clock,
    )


async def test_two_views_inside_the_cache_time_probe_once_and_a_later_one_probes_again(
    fake: FakeDocker,
) -> None:
    clock = Clock()
    cli = timed(fake, clock)
    first = await cli.status()
    clock.now += docker_cli.STATUS_TTL_SECONDS - 0.1
    second = await cli.status()
    assert first == second and fake.probes() == 1
    clock.now += 0.1
    await cli.status()
    assert fake.probes() == 2


async def test_a_check_that_decides_always_probes_and_its_answer_is_what_views_then_show(
    fake: FakeDocker,
) -> None:
    clock = Clock()
    cli = timed(fake, clock)
    assert (await cli.status()).available and fake.probes() == 1
    fake.switch("no-daemon")
    assert (await cli.status()).available, "a view inside the cache time may be stale"
    assert fake.probes() == 1
    decided = await cli.status(fresh=True)
    assert not decided.available and fake.probes() == 2
    assert await cli.status() == decided and fake.probes() == 2


async def test_the_cheap_socket_test_is_never_cached(fake: FakeDocker) -> None:
    clock = Clock()
    cli = timed(fake, clock)
    assert (await cli.status()).available and fake.probes() == 1
    fake.socket_path.unlink()
    status = await cli.status()
    assert status.reason == NOT_MOUNTED_REASON and fake.probes() == 1


def test_the_child_environment_is_built_from_fixed_values_only(
    fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in APP_SECRETS.items():
        monkeypatch.setenv(name, value)
    cli = fake.cli()
    environment = cli.environment()
    assert set(environment) == {"PATH", "HOME", "LANG", "DOCKER_CONFIG", "DOCKER_HOST"}
    assert environment["DOCKER_HOST"] == f"unix://{fake.socket_path}"
    assert environment["HOME"] == environment["DOCKER_CONFIG"] == str(cli.scratch)
    assert environment["PATH"] == docker_cli.CHILD_PATH and environment["LANG"] == "C.UTF-8"
    assert not any(value in " ".join(environment.values()) for value in APP_SECRETS.values())


def test_the_scratch_directory_is_private_made_on_first_use_and_removed_with_the_object(
    tmp_path: Path,
) -> None:
    cli = DockerCli(socket_path=tmp_path / "none.sock")
    scratch = cli.scratch
    assert scratch.is_dir() and scratch.stat().st_mode & 0o077 == 0
    assert cli.scratch == scratch
    del cli
    gc.collect()
    assert not scratch.exists()
