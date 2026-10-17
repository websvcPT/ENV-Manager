"""The confined text read: relative path rules, symlink and special file refusal, bounds."""

import os
from pathlib import Path, PurePosixPath

import pytest

from app.services.confined_read import (
    ConfinedReadError,
    PathProblem,
    read_confined_text,
    resolve_below_root,
    split_relative,
)
from app.services.paths import AllowedPaths


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A real directory standing in for the allowed host root, with a deployment folder."""
    root = (tmp_path / "host").resolve()
    (root / "deploy" / "environments").mkdir(parents=True)
    return root


def read(host: Path, relative: str, max_bytes: int = 1024) -> str:
    """Read below the deployment folder with the host folder as the only allowed path."""
    return read_confined_text(
        AllowedPaths([host]), str(host / "deploy"), relative, label="target", max_bytes=max_bytes
    )


def problem_of(host: Path, relative: str, max_bytes: int = 1024) -> PathProblem:
    """The problem a read is refused with."""
    with pytest.raises(ConfinedReadError) as caught:
        read(host, relative, max_bytes)
    return caught.value.problem


def test_reads_a_regular_file(host: Path) -> None:
    (host / "deploy" / "environments" / ".env").write_text("A=1\n")
    assert read(host, "environments/.env") == "A=1\n"
    assert read(host, "./environments/./.env") == "A=1\n"


@pytest.mark.parametrize("raw", ["", "  ", "/etc/passwd", "../x", "a/../../b", "a\x00b"])
def test_refuses_paths_that_are_not_relative(host: Path, raw: str) -> None:
    assert problem_of(host, raw) is PathProblem.NOT_RELATIVE
    with pytest.raises(ConfinedReadError):
        split_relative(raw, "target")


def test_follows_a_symlink_that_stays_inside_the_deployment(host: Path) -> None:
    (host / "deploy" / "real").write_text("A=1\n")
    (host / "deploy" / "link").symlink_to(host / "deploy" / "real")
    assert read(host, "link") == "A=1\n"


def test_refuses_a_symlink_to_a_file_outside_the_allowed_paths(host: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET=1\n")
    (host / "deploy" / "escape").symlink_to(outside)
    assert problem_of(host, "escape") is PathProblem.LEAVES_ROOT


def test_refuses_a_symlink_to_another_allowed_folder(host: Path) -> None:
    (host / "other").mkdir()
    (host / "other" / "secret").write_text("A=1\n")
    (host / "deploy" / "link").symlink_to(host / "other" / "secret")
    assert problem_of(host, "link") is PathProblem.LEAVES_ROOT


def test_refuses_a_deployment_outside_the_allowed_paths(host: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("A=1\n")
    with pytest.raises(ConfinedReadError) as caught:
        read_confined_text(
            AllowedPaths([host]), str(elsewhere), ".env", label="target", max_bytes=1024
        )
    assert caught.value.problem is PathProblem.ROOT_OUTSIDE


def test_refuses_missing_files_directories_and_the_root_itself(host: Path) -> None:
    assert problem_of(host, "environments/nope") is PathProblem.NOT_A_FILE
    assert problem_of(host, "environments") is PathProblem.NOT_A_FILE
    assert problem_of(host, ".") is PathProblem.LEAVES_ROOT


def test_refuses_a_fifo_without_hanging(host: Path) -> None:
    os.mkfifo(host / "deploy" / "pipe")
    assert problem_of(host, "pipe") is PathProblem.NOT_A_FILE


def test_refuses_a_directory_swapped_for_a_symlink_after_the_path_check(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("SECRET=1\n")
    (host / "deploy" / "swapped").symlink_to(outside)
    unswapped = host / "deploy" / "swapped" / "secret"
    # The first resolution still saw a folder inside the deployment; the open finds the swap.
    monkeypatch.setattr(
        "app.services.confined_read.resolve_below_root",
        lambda *args: (host / "deploy", unswapped),
    )
    assert problem_of(host, "swapped/secret") is PathProblem.LEAVES_ROOT


def test_refuses_binary_and_oversized_files(host: Path) -> None:
    (host / "deploy" / "binary").write_bytes(b"\xff\xfe\x00A=1")
    (host / "deploy" / "big").write_bytes(b"A" * 11)
    assert problem_of(host, "binary") is PathProblem.NOT_UTF8
    assert problem_of(host, "big", max_bytes=10) is PathProblem.TOO_LARGE
    (host / "deploy" / "exact").write_bytes(b"A" * 10)
    assert read(host, "exact", max_bytes=10) == "A" * 10


def test_resolve_below_root_allows_a_missing_file(host: Path) -> None:
    root, real = resolve_below_root(
        AllowedPaths([host]), str(host / "deploy"), PurePosixPath("new/dir/file"), "target"
    )
    assert root == host / "deploy" and real == host / "deploy" / "new" / "dir" / "file"
