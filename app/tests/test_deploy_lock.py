"""The deploy lock: keyed on the real root path, never waited for, held across processes."""

import subprocess
import sys
from pathlib import Path

import pytest

from app.services.deploy_lock import (
    DeployLockBusyError,
    DeployLockUnavailableError,
    hold_deploy_lock,
    lock_key,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A real deployment root."""
    path = tmp_path.resolve() / "stack"
    path.mkdir()
    return path


def test_a_second_holder_is_refused_at_once_and_the_lock_is_released_afterwards(
    tmp_path: Path, root: Path
) -> None:
    lock_dir = tmp_path / "locks"
    with (
        hold_deploy_lock(lock_dir, str(root)),
        pytest.raises(DeployLockBusyError),
        hold_deploy_lock(lock_dir, str(root)),
    ):
        pytest.fail("the second holder must not get the lock")
    with hold_deploy_lock(lock_dir, str(root)):
        pass


def test_the_lock_is_released_when_the_body_fails(tmp_path: Path, root: Path) -> None:
    lock_dir = tmp_path / "locks"
    with pytest.raises(RuntimeError), hold_deploy_lock(lock_dir, str(root)):
        raise RuntimeError
    with hold_deploy_lock(lock_dir, str(root)):
        pass


def test_two_paths_to_one_real_root_share_one_lock(tmp_path: Path, root: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    assert lock_key(str(alias)) == lock_key(str(root))
    lock_dir = tmp_path / "locks"
    with (
        hold_deploy_lock(lock_dir, str(root)),
        pytest.raises(DeployLockBusyError),
        hold_deploy_lock(lock_dir, str(alias)),
    ):
        pytest.fail("a symlink to the same root must share the lock")


def test_different_roots_do_not_block_each_other(tmp_path: Path, root: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    lock_dir = tmp_path / "locks"
    with hold_deploy_lock(lock_dir, str(root)), hold_deploy_lock(lock_dir, str(other)):
        assert lock_key(str(root)) != lock_key(str(other))


def test_the_key_falls_back_to_the_stored_text_when_the_path_cannot_be_resolved() -> None:
    assert lock_key("relative/path") == lock_key("relative/path")
    assert lock_key("relative/path") != lock_key("another/path")


def test_a_lock_folder_that_cannot_be_used_is_reported(tmp_path: Path, root: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(DeployLockUnavailableError), hold_deploy_lock(blocker, str(root)):
        pytest.fail("no lock without a lock folder")


def test_another_process_is_refused_while_this_one_holds_the_lock(
    tmp_path: Path, root: Path
) -> None:
    lock_dir = tmp_path / "locks"
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from app.services.deploy_lock import DeployLockBusyError, hold_deploy_lock\n"
        "try:\n"
        "    with hold_deploy_lock(Path(sys.argv[1]), sys.argv[2]):\n"
        "        sys.exit(0)\n"
        "except DeployLockBusyError:\n"
        "    sys.exit(7)\n"
    )
    command = [sys.executable, "-c", probe, str(lock_dir), str(root)]
    with hold_deploy_lock(lock_dir, str(root)):
        busy = subprocess.run(command, check=False)  # noqa: S603
    free = subprocess.run(command, check=False)  # noqa: S603
    assert (busy.returncode, free.returncode) == (7, 0)
