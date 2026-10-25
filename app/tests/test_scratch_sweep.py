"""Claiming the scratch folder of the Docker CLI and sweeping the ones a killed process left.

Folders in use are told from stale ones by a real lock, so the tests run a second process that
holds one, and kill it the hard way.
"""

import fcntl
import gc
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.docker_cli import DockerCli
from app.services.scratch_sweep import (
    LOCK_FILE_NAME,
    LOCKLESS_GRACE_SECONDS,
    SCRATCH_PREFIX,
    claim_scratch,
    sweep_stale_scratch,
)

HOLDER = (
    "import fcntl, os, sys, time\n"
    "descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)\n"
    "fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


def leftover(root: Path, name: str = SCRATCH_PREFIX + "stale", runs: int = 1) -> Path:
    """A scratch folder with its lock file and ``runs`` run folders that each hold a config."""
    folder = root / name
    folder.mkdir(mode=0o700)
    (folder / LOCK_FILE_NAME).touch(mode=0o600)
    for number in range(runs):
        run = folder / f"run-{number}"
        run.mkdir(mode=0o700)
        (run / "config.json").write_text('{"auths": {}}', encoding="utf-8")
    return folder


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty temporary directory that the scratch folders of the Docker CLI are made in."""
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture
def holder(temp_root: Path) -> Iterator[subprocess.Popen[str]]:
    """A second process that holds the lock of a scratch folder until it is killed."""
    folder = temp_root / (SCRATCH_PREFIX + "other")
    folder.mkdir(mode=0o700)
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", "-c", HOLDER, str(folder / LOCK_FILE_NAME)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def test_a_folder_that_no_process_holds_is_removed_with_the_run_folders_it_still_has(
    temp_root: Path,
) -> None:
    stale = leftover(temp_root, runs=2)
    assert sweep_stale_scratch(temp_root) == 2
    assert not stale.exists()


def test_a_stale_folder_without_run_folders_is_removed_and_counts_nothing(
    temp_root: Path,
) -> None:
    stale = leftover(temp_root, runs=0)
    assert sweep_stale_scratch(temp_root) == 0
    assert not stale.exists()


def test_an_empty_or_unreadable_temporary_directory_is_a_sweep_of_nothing(
    temp_root: Path,
) -> None:
    assert sweep_stale_scratch(temp_root) == 0
    assert sweep_stale_scratch(temp_root / "missing") == 0


def test_the_folder_of_a_live_docker_cli_is_kept_until_the_object_is_gone(temp_root: Path) -> None:
    cli = DockerCli()
    scratch = cli.scratch
    assert scratch.parent == temp_root and scratch.name.startswith(SCRATCH_PREFIX)
    run = scratch / "run-0"
    run.mkdir(mode=0o700)
    (run / "config.json").write_text("{}", encoding="utf-8")
    assert sweep_stale_scratch(temp_root) == 0
    assert (run / "config.json").exists()
    del cli
    gc.collect()
    assert not scratch.exists()
    assert sweep_stale_scratch(temp_root) == 0


def test_a_scratch_folder_is_private_and_its_lock_file_is_owned_by_the_object(
    temp_root: Path,
) -> None:
    cli = DockerCli()
    lock = cli.scratch / LOCK_FILE_NAME
    assert lock.stat().st_mode & 0o777 == 0o600 and cli.scratch.stat().st_mode & 0o077 == 0
    descriptor = os.open(lock, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_a_given_scratch_folder_is_not_claimed(tmp_path: Path) -> None:
    cli = DockerCli(scratch=tmp_path / "scratch")
    assert cli.scratch == tmp_path / "scratch" and list(cli.scratch.iterdir()) == []


def test_a_claimed_folder_is_kept_while_the_claim_is_held_and_swept_after_it_is_dropped(
    temp_root: Path,
) -> None:
    folder = leftover(temp_root, runs=1)
    claim = claim_scratch(folder)
    assert sweep_stale_scratch(temp_root) == 0 and folder.exists()
    claim.close()
    assert sweep_stale_scratch(temp_root) == 1 and not folder.exists()


def test_a_folder_held_by_another_process_is_kept_and_swept_once_that_process_is_killed(
    temp_root: Path, holder: subprocess.Popen[str]
) -> None:
    folder = temp_root / (SCRATCH_PREFIX + "other")
    run = folder / "run-0"
    run.mkdir(mode=0o700)
    (run / "config.json").write_text("{}", encoding="utf-8")
    assert sweep_stale_scratch(temp_root) == 0 and (run / "config.json").exists()
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait()
    assert sweep_stale_scratch(temp_root) == 1
    assert not folder.exists()


def test_a_link_a_file_and_other_names_are_left_alone(temp_root: Path, tmp_path: Path) -> None:
    target = leftover(tmp_path, name="not-a-scratch-folder")
    (temp_root / (SCRATCH_PREFIX + "link")).symlink_to(target, target_is_directory=True)
    (temp_root / (SCRATCH_PREFIX + "file")).write_text("not a folder", encoding="utf-8")
    other = leftover(temp_root, name="something-else")
    assert sweep_stale_scratch(temp_root) == 0
    assert (target / "run-0" / "config.json").exists() and (other / "run-0").exists()
    assert (temp_root / (SCRATCH_PREFIX + "link")).is_symlink()
    assert (temp_root / (SCRATCH_PREFIX + "file")).is_file()


def test_a_folder_of_another_user_is_left_alone(temp_root: Path) -> None:
    foreign = leftover(temp_root)
    assert sweep_stale_scratch(temp_root, user_id=os.getuid() + 1) == 0
    assert (foreign / "run-0" / "config.json").exists()
    assert sweep_stale_scratch(temp_root, user_id=os.getuid()) == 1


def test_a_lock_file_that_is_a_link_proves_nothing_and_keeps_the_folder(
    temp_root: Path, tmp_path: Path
) -> None:
    folder = leftover(temp_root)
    (folder / LOCK_FILE_NAME).unlink()
    (folder / LOCK_FILE_NAME).symlink_to(tmp_path)
    assert sweep_stale_scratch(temp_root) == 0 and folder.exists()


def test_a_folder_without_a_lock_file_waits_for_the_grace_period(temp_root: Path) -> None:
    folder = leftover(temp_root)
    (folder / LOCK_FILE_NAME).unlink()
    now = time.time()
    assert sweep_stale_scratch(temp_root, now=now) == 0 and folder.exists()
    assert sweep_stale_scratch(temp_root, now=now + LOCKLESS_GRACE_SECONDS - 1) == 0
    assert sweep_stale_scratch(temp_root, now=now + LOCKLESS_GRACE_SECONDS + 1) == 1
    assert not folder.exists()
