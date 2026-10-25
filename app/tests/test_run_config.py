"""The private Docker config folder of one command run."""

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from app.services.registries import RegistryLogin
from app.services.run_config import RunConfigError, private_docker_config

TOKEN = "tok-secret-for-the-run"
LOGINS = (RegistryLogin("registry.example.com", "bot", TOKEN),)


def mode_of(path: Path) -> int:
    """The permission bits of a file or folder."""
    return stat.S_IMODE(path.stat().st_mode)


def test_the_folder_holds_the_logins_privately_and_is_removed_afterwards(tmp_path: Path) -> None:
    with private_docker_config(tmp_path, LOGINS) as folder:
        assert folder.parent == tmp_path and folder.name.startswith("run-")
        assert (mode_of(folder), mode_of(folder / "config.json")) == (0o700, 0o600)
        written = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        credentials = base64.b64encode(f"bot:{TOKEN}".encode()).decode("ascii")
        assert written == {"auths": {"registry.example.com": {"auth": credentials}}}
        assert [path.name for path in folder.iterdir()] == ["config.json"]
    assert not folder.exists() and list(tmp_path.iterdir()) == []


def test_every_run_gets_a_folder_of_its_own(tmp_path: Path) -> None:
    with (
        private_docker_config(tmp_path, LOGINS) as first,
        private_docker_config(tmp_path, LOGINS) as second,
    ):
        assert first != second and first.is_dir() and second.is_dir()
    assert list(tmp_path.iterdir()) == []


def test_the_modes_do_not_depend_on_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0)
    try:
        with private_docker_config(tmp_path, LOGINS) as folder:
            modes = (mode_of(folder), mode_of(folder / "config.json"))
    finally:
        os.umask(previous)
    assert modes == (0o700, 0o600)


def test_the_file_is_created_private_and_is_never_opened_up_and_closed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[tuple[int, int]] = []
    real_open = os.open

    def spy(path: Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(path).name == "config.json":
            created.append((flags, mode))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def refuse(*arguments: object, **options: object) -> None:
        raise AssertionError("the mode of the file was changed after it was written")

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(os, "chmod", refuse)
    with private_docker_config(tmp_path, LOGINS):
        pass
    ((flags, mode),) = created
    assert mode == 0o600 and flags & os.O_EXCL and flags & os.O_CREAT


def test_the_folder_is_removed_when_the_block_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), private_docker_config(tmp_path, LOGINS) as folder:
        raise RuntimeError
    assert not folder.exists() and list(tmp_path.iterdir()) == []


def test_a_file_that_cannot_be_written_leaves_nothing_behind_and_names_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = os.open

    def refuse(path: Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if Path(path).name == "config.json":
            raise PermissionError(f"{tmp_path}/{TOKEN}")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(RunConfigError) as raised, private_docker_config(tmp_path, LOGINS):
        pytest.fail("the block must not run")
    assert str(tmp_path) not in str(raised.value) and TOKEN not in str(raised.value)
    assert list(tmp_path.iterdir()) == []


def test_a_scratch_folder_that_is_missing_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(RunConfigError), private_docker_config(tmp_path / "missing", LOGINS):
        pytest.fail("the block must not run")
