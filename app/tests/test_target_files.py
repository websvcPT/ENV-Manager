"""Target file access: resolving, reading, backing up and atomically writing without symlinks."""

import os
import secrets
import stat
from pathlib import Path

import pytest

from app.services import target_files
from app.services.paths import AllowedPaths
from app.services.target_files import (
    BackupRequest,
    Ownership,
    TargetError,
    TargetLocation,
    TargetProblem,
    locate_target,
    parse_mode,
    read_target_file,
    resolve_ownership,
    write_target_file,
)

SECRET = "TARGET_SECRET=Hq3-sentinel-value\n"


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """The allowed folder; the deployment root is ``host/app`` and ``host/other`` is a sibling."""
    real = tmp_path.resolve() / "host"
    (real / "app" / "environments").mkdir(parents=True)
    (real / "other").mkdir()
    return real


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A folder that is not allowed."""
    path = tmp_path.resolve() / "outside"
    path.mkdir()
    return path


@pytest.fixture
def allowed(host: Path) -> AllowedPaths:
    """Only ``host`` is allowed."""
    return AllowedPaths([host])


def locate(
    allowed: AllowedPaths, host: Path, target: str = "environments/.env.api"
) -> TargetLocation:
    """The location of a target below the deployment root."""
    return locate_target(allowed, str(host / "app"), target)


def swap_for_symlink(path: Path, destination: Path) -> None:
    """Replace a folder or file by a symlink to ``destination``, keeping the original aside."""
    path.rename(path.with_name(path.name + "-original"))
    path.symlink_to(destination)


def test_locate_resolves_the_folder_and_keeps_the_name(allowed: AllowedPaths, host: Path) -> None:
    location = locate(allowed, host)
    assert location.root == host / "app"
    assert location.directory == host / "app" / "environments"
    assert (location.name, location.relative) == (".env.api", "environments/.env.api")


def test_locate_keeps_a_final_symlink_unresolved(allowed: AllowedPaths, host: Path) -> None:
    (host / "app" / "environments" / ".env.api").symlink_to(host / "other" / "elsewhere")
    assert locate(allowed, host).name == ".env.api"


@pytest.mark.parametrize("target", ["", "/etc/passwd", "../x", "environments/../../x", "a\x00b"])
def test_locate_refuses_paths_that_are_not_relative(
    allowed: AllowedPaths, host: Path, target: str
) -> None:
    with pytest.raises(TargetError) as error:
        locate(allowed, host, target)
    assert error.value.problem is TargetProblem.NOT_RELATIVE


def test_locate_refuses_a_root_outside_the_allowed_paths(host: Path, outside: Path) -> None:
    with pytest.raises(TargetError) as error:
        locate_target(AllowedPaths([host]), str(outside), "environments/.env.api")
    assert error.value.problem is TargetProblem.ROOT_OUTSIDE
    assert str(outside) not in str(error.value)


@pytest.mark.parametrize("leaves_to", ["other", "outside"])
def test_locate_refuses_a_folder_symlink_leaving_the_root(
    allowed: AllowedPaths, host: Path, outside: Path, leaves_to: str
) -> None:
    destination = host / "other" if leaves_to == "other" else outside
    (host / "app" / "linked").symlink_to(destination)
    with pytest.raises(TargetError) as error:
        locate(allowed, host, "linked/.env.api")
    assert error.value.problem is TargetProblem.LEAVES_ROOT


def test_read_returns_none_for_a_missing_file_and_the_bytes_for_a_file(
    allowed: AllowedPaths, host: Path
) -> None:
    location = locate(allowed, host)
    assert read_target_file(location, allowed) is None
    (host / "app" / "environments" / ".env.api").write_text(SECRET, encoding="utf-8")
    assert read_target_file(location, allowed) == SECRET.encode()


def test_read_refuses_a_symlink_a_folder_and_a_missing_folder(
    allowed: AllowedPaths, host: Path
) -> None:
    folder = host / "app" / "environments"
    (host / "other" / "real").write_text(SECRET, encoding="utf-8")
    (folder / "link").symlink_to(host / "other" / "real")
    (folder / "dir").mkdir()
    for name, problem in (("link", TargetProblem.SYMLINK), ("dir", TargetProblem.NOT_A_FILE)):
        with pytest.raises(TargetError) as error:
            read_target_file(locate(allowed, host, f"environments/{name}"), allowed)
        assert error.value.problem is problem
    with pytest.raises(TargetError) as missing:
        read_target_file(locate(allowed, host, "nowhere/.env.api"), allowed)
    assert missing.value.problem is TargetProblem.FOLDER_MISSING


def test_read_refuses_a_file_that_is_too_large(
    allowed: AllowedPaths, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(target_files, "MAX_TARGET_BYTES", 8)
    (host / "app" / "environments" / ".env.api").write_text(SECRET, encoding="utf-8")
    with pytest.raises(TargetError) as error:
        read_target_file(locate(allowed, host), allowed)
    assert error.value.problem is TargetProblem.TOO_LARGE


def test_write_creates_the_file_with_mode_0600_and_leaves_no_temporary_file(
    allowed: AllowedPaths, host: Path
) -> None:
    location = locate(allowed, host)
    assert write_target_file(location, allowed, SECRET.encode()) is None
    written = host / "app" / "environments" / ".env.api"
    assert written.read_text(encoding="utf-8") == SECRET
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert [path.name for path in written.parent.iterdir()] == [".env.api"]


def test_write_replaces_a_file_and_sets_the_mode_whatever_the_umask(
    allowed: AllowedPaths, host: Path
) -> None:
    written = host / "app" / "environments" / ".env.api"
    written.write_text("OLD=1\n", encoding="utf-8")
    written.chmod(0o666)
    previous_umask = os.umask(0o077)
    try:
        write_target_file(locate(allowed, host), allowed, SECRET.encode(), mode=0o640)
    finally:
        os.umask(previous_umask)
    assert written.read_text(encoding="utf-8") == SECRET
    assert stat.S_IMODE(written.stat().st_mode) == 0o640


def test_write_replaces_by_rename_so_a_reader_keeps_the_old_file(
    allowed: AllowedPaths, host: Path
) -> None:
    written = host / "app" / "environments" / ".env.api"
    written.write_text("OLD=1\n", encoding="utf-8")
    inode = written.stat().st_ino
    with written.open("rb") as reader:
        write_target_file(locate(allowed, host), allowed, SECRET.encode())
        assert reader.read() == b"OLD=1\n"
    assert written.stat().st_ino != inode


def test_backup_is_written_next_to_the_target_with_mode_0600(
    allowed: AllowedPaths, host: Path
) -> None:
    written = host / "app" / "environments" / ".env.api"
    written.write_text("OLD=1\n", encoding="utf-8")
    previous_umask = os.umask(0)
    try:
        name = write_target_file(
            locate(allowed, host),
            allowed,
            SECRET.encode(),
            backup=BackupRequest(b"OLD=1\n", "20261018T101500Z"),
        )
    finally:
        os.umask(previous_umask)
    assert name is not None and name.startswith(".env.api.bak-20261018T101500Z-")
    backup = written.parent / name
    assert backup.read_text(encoding="utf-8") == "OLD=1\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert written.read_text(encoding="utf-8") == SECRET


def test_backup_never_follows_a_symlink_planted_at_its_name(
    allowed: AllowedPaths, host: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = host / "app" / "environments"
    (folder / ".env.api").write_text("OLD=1\n", encoding="utf-8")
    victim = outside / "victim"
    victim.write_text("UNTOUCHED\n", encoding="utf-8")
    monkeypatch.setattr(secrets, "token_hex", lambda _count: "abcd")
    (folder / ".env.api.bak-STAMP-abcd").symlink_to(victim)
    with pytest.raises(TargetError) as error:
        write_target_file(
            locate(allowed, host),
            allowed,
            SECRET.encode(),
            backup=BackupRequest(b"OLD=1\n", "STAMP"),
        )
    assert error.value.problem is TargetProblem.BACKUP_FAILED
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"
    assert (folder / ".env.api").read_text(encoding="utf-8") == "OLD=1\n"


def test_a_failed_owner_change_leaves_the_target_and_removes_temporary_files(
    allowed: AllowedPaths, host: Path
) -> None:
    folder = host / "app" / "environments"
    (folder / ".env.api").write_text("OLD=1\n", encoding="utf-8")

    def deny(_descriptor: int, _uid: int, _gid: int) -> None:
        raise PermissionError

    with pytest.raises(TargetError) as error:
        write_target_file(
            locate(allowed, host),
            allowed,
            SECRET.encode(),
            ownership=Ownership(0, 0),
            backup=BackupRequest(b"OLD=1\n", "STAMP"),
            change_owner=deny,
        )
    assert error.value.problem is TargetProblem.OWNER_DENIED
    assert "privilege" in str(error.value)
    assert (folder / ".env.api").read_text(encoding="utf-8") == "OLD=1\n"
    assert [path.name for path in folder.iterdir()] == [".env.api"]


def test_ownership_for_the_current_user_is_applied_without_privilege(
    allowed: AllowedPaths, host: Path
) -> None:
    ownership = resolve_ownership(str(os.getuid()), str(os.getgid()))
    assert ownership == Ownership(os.getuid(), os.getgid())
    write_target_file(locate(allowed, host), allowed, SECRET.encode(), ownership=ownership)
    info = (host / "app" / "environments" / ".env.api").stat()
    assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid())


def test_resolve_ownership_reads_names_ids_and_refuses_unknown_names() -> None:
    assert resolve_ownership(None, None) is None
    assert resolve_ownership("", "") is None
    assert resolve_ownership("root", None) == Ownership(0, -1)
    assert resolve_ownership(None, "0") == Ownership(-1, 0)
    with pytest.raises(TargetError) as error:
        resolve_ownership("no-such-account-xyz", None)
    assert error.value.problem is TargetProblem.OWNER_UNKNOWN


def test_parse_mode_defaults_to_0600() -> None:
    assert parse_mode(None) == 0o600
    assert parse_mode("") == 0o600
    assert parse_mode("0640") == 0o640
    assert parse_mode("4755") == 0o755


def test_a_target_that_is_a_symlink_is_refused_and_the_link_target_untouched(
    allowed: AllowedPaths, host: Path, outside: Path
) -> None:
    victim = outside / "victim"
    victim.write_text("UNTOUCHED\n", encoding="utf-8")
    link = host / "app" / "environments" / ".env.api"
    link.symlink_to(victim)
    with pytest.raises(TargetError) as error:
        write_target_file(locate(allowed, host), allowed, SECRET.encode())
    assert error.value.problem is TargetProblem.SYMLINK
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"
    assert [path.name for path in link.parent.iterdir()] == [".env.api"]


def test_a_folder_swapped_for_a_symlink_after_the_check_writes_nothing_outside(
    allowed: AllowedPaths, host: Path, outside: Path
) -> None:
    location = locate(allowed, host)
    swap_for_symlink(host / "app" / "environments", outside)
    with pytest.raises(TargetError) as error:
        write_target_file(location, allowed, SECRET.encode())
    assert error.value.problem is TargetProblem.NOT_A_FOLDER
    assert list(outside.iterdir()) == []
    assert list((host / "app" / "environments-original").iterdir()) == []


@pytest.mark.parametrize("destination", ["other", "outside"])
def test_a_parent_swapped_for_a_symlink_after_the_check_writes_nothing_outside(
    allowed: AllowedPaths, host: Path, outside: Path, destination: str
) -> None:
    folder = host / "app" / "environments" / "conf"
    folder.mkdir()
    location = locate(allowed, host, "environments/conf/app.env")
    landing = host / "other" if destination == "other" else outside
    (landing / "conf").mkdir()
    swap_for_symlink(host / "app" / "environments", landing)
    with pytest.raises(TargetError) as error:
        write_target_file(location, allowed, SECRET.encode())
    assert error.value.problem is TargetProblem.LEAVES_ROOT
    assert list((landing / "conf").iterdir()) == []


def test_the_root_swapped_for_a_symlink_after_the_check_writes_nothing_outside(
    allowed: AllowedPaths, host: Path, outside: Path
) -> None:
    location = locate(allowed, host)
    (outside / "environments").mkdir()
    swap_for_symlink(host / "app", outside)
    with pytest.raises(TargetError) as error:
        write_target_file(location, allowed, SECRET.encode())
    assert error.value.problem is TargetProblem.LEAVES_ROOT
    assert list((outside / "environments").iterdir()) == []


def test_a_target_swapped_for_a_symlink_after_the_read_is_not_written_through(
    allowed: AllowedPaths, host: Path, outside: Path
) -> None:
    written = host / "app" / "environments" / ".env.api"
    written.write_text("OLD=1\n", encoding="utf-8")
    location = locate(allowed, host)
    assert read_target_file(location, allowed) == b"OLD=1\n"
    victim = outside / "victim"
    victim.write_text("UNTOUCHED\n", encoding="utf-8")
    swap_for_symlink(written, victim)
    with pytest.raises(TargetError) as error:
        write_target_file(location, allowed, SECRET.encode())
    assert error.value.problem is TargetProblem.SYMLINK
    assert victim.read_text(encoding="utf-8") == "UNTOUCHED\n"


def test_a_removed_allowed_path_stops_the_write(allowed: AllowedPaths, host: Path) -> None:
    location = locate(allowed, host)
    with pytest.raises(TargetError) as error:
        write_target_file(location, AllowedPaths([host / "other"]), SECRET.encode())
    assert error.value.problem is TargetProblem.LEAVES_ROOT
    assert not (host / "app" / "environments" / ".env.api").exists()
