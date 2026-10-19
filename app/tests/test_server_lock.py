"""The server lock of the data folder: held by the server while it runs, by a key rotation too."""

import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.crypto import generate_master_key
from app.main import create_app
from app.services.server_lock import (
    ServerLockBusyError,
    ServerLockUnavailableError,
    acquire_server_lock,
)

HOLD_AND_CRASH = """
import os, sys
from pathlib import Path
from app.services.server_lock import acquire_server_lock
acquire_server_lock(Path(sys.argv[1]), "server")
os._exit(0)
"""
HOLD_UNTIL_CLOSED = """
import sys
from pathlib import Path
from app.services.server_lock import acquire_server_lock
acquire_server_lock(Path(sys.argv[1]), "server")
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point the settings at a temporary data folder with a fresh key."""
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_a_second_holder_is_refused_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "server.lock"
    first = acquire_server_lock(path, "server")
    try:
        with pytest.raises(ServerLockBusyError) as refused:
            acquire_server_lock(path, "key rotation")
        assert refused.value.holder is not None and refused.value.holder.startswith("server pid=")
    finally:
        first.release()
    again = acquire_server_lock(path, "key rotation")
    again.release()


def test_the_holder_writes_a_line_about_itself_and_empties_the_file_when_it_lets_go(
    tmp_path: Path,
) -> None:
    path = tmp_path / "server.lock"
    held = acquire_server_lock(path, "key rotation")
    line = path.read_text(encoding="utf-8")
    assert line.startswith("key rotation pid=") and f"pid={os.getpid()}" in line
    assert held.previous is None
    held.release()
    held.release()
    assert path.read_text(encoding="utf-8") == ""


def test_the_lock_file_is_private_and_is_not_followed_through_a_link(tmp_path: Path) -> None:
    path = tmp_path / "server.lock"
    acquire_server_lock(path, "server").release()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("kept", encoding="utf-8")
    link = tmp_path / "linked.lock"
    link.symlink_to(elsewhere)
    with pytest.raises(ServerLockUnavailableError):
        acquire_server_lock(link, "server")
    assert elsewhere.read_text(encoding="utf-8") == "kept"


def test_a_lock_file_that_cannot_be_created_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ServerLockUnavailableError):
        acquire_server_lock(blocker / "server.lock", "server")


def test_a_lock_left_by_a_crashed_holder_is_stale_and_taken_over(tmp_path: Path) -> None:
    """A process that dies without letting go leaves its line behind; the kernel drops the lock."""
    path = tmp_path / "server.lock"
    crashed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", HOLD_AND_CRASH, str(path)],
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert crashed.returncode == 0
    assert path.read_text(encoding="utf-8").startswith("server pid=")
    held = acquire_server_lock(path, "key rotation")
    try:
        assert held.previous is not None and held.previous.startswith("server pid=")
        assert path.read_text(encoding="utf-8").startswith("key rotation pid=")
    finally:
        held.release()


def test_a_holder_in_another_process_is_refused_until_it_ends(tmp_path: Path) -> None:
    """The lock holds between processes, as it must between containers on one data folder."""
    path = tmp_path / "server.lock"
    other = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", HOLD_UNTIL_CLOSED, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    try:
        assert other.stdout is not None and other.stdout.readline().strip() == "held"
        with pytest.raises(ServerLockBusyError):
            acquire_server_lock(path, "key rotation")
    finally:
        other.communicate()
    acquire_server_lock(path, "key rotation").release()


async def test_the_server_holds_the_lock_for_its_whole_life(configured: Path) -> None:
    lock_file = configured / "server.lock"
    application = create_app()
    async with application.router.lifespan_context(application):
        assert lock_file.read_text(encoding="utf-8").startswith("server pid=")
        with pytest.raises(ServerLockBusyError):
            acquire_server_lock(lock_file, "key rotation")
    assert lock_file.read_text(encoding="utf-8") == ""
    acquire_server_lock(lock_file, "key rotation").release()


async def test_a_second_server_on_the_same_data_folder_refuses_to_start(configured: Path) -> None:
    first = create_app()
    async with first.router.lifespan_context(first):
        second = create_app()
        with pytest.raises(ServerLockBusyError):
            async with second.router.lifespan_context(second):
                pytest.fail("the second server must not start")
        assert (configured / "server.lock").read_text(encoding="utf-8").startswith("server pid=")
    third = create_app()
    async with third.router.lifespan_context(third):
        pass


async def test_a_server_does_not_start_while_a_rotation_holds_the_folder(configured: Path) -> None:
    held = acquire_server_lock(configured / "server.lock", "key rotation")
    try:
        application = create_app()
        with pytest.raises(ServerLockBusyError) as refused:
            async with application.router.lifespan_context(application):
                pytest.fail("the server must not start during a rotation")
        assert refused.value.holder is not None
        assert refused.value.holder.startswith("key rotation pid=")
        assert not (configured / "envmanager.db").exists()
    finally:
        held.release()


async def test_a_server_that_fails_to_start_lets_go_of_the_lock(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = create_app()
    async with first.router.lifespan_context(first):
        pass
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    get_settings.cache_clear()
    wrong_key = create_app()
    with pytest.raises(Exception, match="does not match"):
        async with wrong_key.router.lifespan_context(wrong_key):
            pass
    acquire_server_lock(configured / "server.lock", "key rotation").release()
