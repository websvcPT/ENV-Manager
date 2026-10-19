"""Tests for allowed path validation: normalization, containment, symlinks and mount status."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.seeds import SeedData
from app.services.paths import (
    AllowedPaths,
    PathNotAllowedError,
    PathStatus,
    PathValidationError,
    is_within,
    load_allowed_paths,
    normalize_path,
    path_status,
    read_mount_points,
    resolve_real_path,
    validate_allowed_entry,
)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A real directory standing in for the host, with real (symlink free) path."""
    root = (tmp_path / "host").resolve()
    root.mkdir()
    return root


@pytest.fixture
def data_path(tmp_path: Path) -> Path:
    """A stand-in for the application data volume."""
    path = tmp_path / "data"
    path.mkdir()
    return path


def make_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def test_normalize_collapses_slashes_and_dots_and_refuses_traversal() -> None:
    assert normalize_path(" /opt//xxx1/./sub/ ") == Path("/opt/xxx1/sub")
    assert normalize_path("//opt/xxx1") == Path("/opt/xxx1")
    for bad in ("", "   ", "opt/xxx1", "./opt", "/opt/../etc", "/opt/xxx1/..", "/opt/a\x00b"):
        with pytest.raises(PathValidationError):
            normalize_path(bad)


def test_resolve_follows_symlinks_and_dotdot_after_them(host: Path) -> None:
    real = make_dir(host / "real" / "inner")
    (host / "link").symlink_to(real)
    assert resolve_real_path(host / "link") == real
    assert resolve_real_path(host / "link" / ".." / "inner") == real
    assert resolve_real_path(host / "missing" / "file") == host / "missing" / "file"
    with pytest.raises(PathValidationError):
        resolve_real_path("relative/path")


def test_resolve_refuses_symlink_loops(host: Path) -> None:
    (host / "a").symlink_to(host / "b")
    (host / "b").symlink_to(host / "a")
    with pytest.raises(PathValidationError):
        resolve_real_path(host / "a" / "file")


def test_containment_compares_whole_components() -> None:
    assert is_within(Path("/opt/xxx1"), Path("/opt/xxx1"))
    assert is_within(Path("/opt/xxx1/a/b"), Path("/opt/xxx1"))
    assert not is_within(Path("/opt/xxx10"), Path("/opt/xxx1"))
    assert not is_within(Path("/opt/xxx10/a"), Path("/opt/xxx1"))
    assert not is_within(Path("/opt"), Path("/opt/xxx1"))


def test_prefix_sibling_is_not_allowed(host: Path) -> None:
    allowed = make_dir(host / "xxx1")
    sibling = make_dir(host / "xxx10")
    paths = AllowedPaths([allowed])
    assert paths.contains(allowed)
    assert paths.contains(allowed / "deep" / "target.env")
    assert not paths.contains(sibling)
    assert not paths.contains(sibling / "target.env")
    with pytest.raises(PathNotAllowedError):
        paths.resolve(sibling)


def test_dotdot_traversal_out_of_an_allowed_path_is_refused(host: Path) -> None:
    allowed = make_dir(host / "xxx1")
    make_dir(host / "xxx2")
    paths = AllowedPaths([allowed])
    assert not paths.contains(allowed / ".." / "xxx2")
    assert not paths.contains(allowed / ".." / ".." / "etc")
    assert paths.contains(allowed / "a" / ".." / "b")


def test_symlink_inside_an_allowed_path_cannot_escape(host: Path) -> None:
    allowed = make_dir(host / "xxx1")
    outside = make_dir(host / "outside")
    (allowed / "escape").symlink_to(outside)
    (allowed / "escape_file").symlink_to(outside / "secret.env")
    paths = AllowedPaths([allowed])
    assert not paths.contains(allowed / "escape")
    assert not paths.contains(allowed / "escape" / "nested" / "target.env")
    assert not paths.contains(allowed / "escape_file")


def test_symlink_that_stays_inside_is_allowed(host: Path) -> None:
    allowed = make_dir(host / "xxx1")
    make_dir(allowed / "real")
    (allowed / "alias").symlink_to(allowed / "real")
    assert AllowedPaths([allowed]).resolve(allowed / "alias" / "t.env") == (
        allowed / "real" / "t.env"
    )


def test_symlink_pointing_into_another_allowed_path_is_allowed(host: Path) -> None:
    first = make_dir(host / "xxx1")
    second = make_dir(host / "xxx3")
    (first / "to_second").symlink_to(second)
    assert AllowedPaths([first, second]).contains(first / "to_second")


def test_entry_swapped_for_a_symlink_is_ignored_until_saved_again(host: Path) -> None:
    allowed = make_dir(host / "xxx1")
    outside = make_dir(host / "outside")
    paths = AllowedPaths([allowed])
    assert paths.contains(allowed)
    allowed.rmdir()
    allowed.symlink_to(outside)
    assert paths.usable_roots() == []
    assert not paths.contains(outside)
    assert not paths.contains(allowed)


def test_empty_list_allows_nothing(host: Path) -> None:
    assert not AllowedPaths([]).contains(host)


def test_relative_and_unresolvable_paths_are_not_contained(host: Path) -> None:
    paths = AllowedPaths([host])
    assert not paths.contains("relative/path")
    with pytest.raises(PathValidationError):
        paths.resolve("relative/path")


def test_removing_the_allowed_path_deletes_nothing_and_blocks_access(host: Path) -> None:
    allowed = make_dir(host / "xxx1")
    target = allowed / "t.env"
    target.write_text("A=1\n")
    assert AllowedPaths([allowed]).contains(target)
    assert not AllowedPaths([]).contains(target)
    assert target.read_text() == "A=1\n"


def test_entry_validation_returns_the_real_path_with_status(host: Path, data_path: Path) -> None:
    real = make_dir(host / "vhosts")
    (host / "alias").symlink_to(real)
    status = validate_allowed_entry(f"{host}/alias/", data_path=data_path, mount_points=[host])
    assert status.path == real
    assert status.mounted
    assert status.writable


def test_entry_validation_refuses_bad_entries(host: Path, data_path: Path) -> None:
    mounts = [host, data_path.parent]
    file_entry = host / "file.txt"
    file_entry.write_text("x")
    cases = {
        "/": "root",
        str(data_path): "data volume",
        str(data_path / "backups"): "data volume",
        str(data_path.parent): "data volume",
        str(host / "missing"): "does not exist",
        str(file_entry): "not a directory",
        "relative": "absolute",
        f"{host}/../host": "'..'",
    }
    for entry, message in cases.items():
        with pytest.raises(PathValidationError, match=message):
            validate_allowed_entry(entry, data_path=data_path, mount_points=mounts)


def test_entry_pointing_at_the_data_volume_through_a_symlink_is_refused(
    host: Path, data_path: Path
) -> None:
    (host / "sneaky").symlink_to(data_path)
    with pytest.raises(PathValidationError, match="data volume"):
        validate_allowed_entry(str(host / "sneaky"), data_path=data_path, mount_points=[host])


def test_entry_outside_every_mount_is_refused(host: Path, data_path: Path) -> None:
    make_dir(host / "vhosts")
    with pytest.raises(PathValidationError, match="not inside a mounted"):
        validate_allowed_entry(str(host / "vhosts"), data_path=data_path, mount_points=[])
    with pytest.raises(PathValidationError, match="not inside a mounted"):
        validate_allowed_entry(
            str(host / "vhosts"), data_path=data_path, mount_points=[Path("/"), Path("/proc")]
        )


def test_mount_may_be_broader_than_the_entry(host: Path, data_path: Path) -> None:
    entry = make_dir(host / "opt" / "xxx1")
    status = validate_allowed_entry(str(entry), data_path=data_path, mount_points=[host / "opt"])
    assert status.mounted
    assert not path_status(host, [host / "opt"]).mounted


def test_status_reports_read_only_directories(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_dir(host / "ro")
    assert path_status(folder, [host]) == PathStatus(path=folder, mounted=True, writable=True)
    monkeypatch.setattr("app.services.paths.os.access", lambda path, mode: False)
    assert path_status(folder, [host]) == PathStatus(path=folder, mounted=True, writable=False)


def test_mount_points_are_read_from_mountinfo_with_escapes(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "30 22 8:2 /vhosts /var/www/vhosts rw - ext4 /dev/sda2 rw\n"
        "31 22 8:3 / /mnt/with\\040space rw - ext4 /dev/sda3 rw\n"
        "garbage\n"
    )
    assert read_mount_points(mountinfo) == [
        Path("/"),
        Path("/var/www/vhosts"),
        Path("/mnt/with space"),
    ]
    assert read_mount_points(tmp_path / "missing") == []


async def test_seeded_allowed_paths_cover_the_seeded_deployment_roots(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    async with create_session_factory(seeded_database)() as db:
        allowed = await load_allowed_paths(db)
    assert allowed.roots == (Path("/opt/billing"), Path("/opt/rentals"))
    for deployment in (
        seed_data.deployments.fancy_prod,
        seed_data.deployments.fancy_staging,
        seed_data.deployments.billing_main,
    ):
        assert allowed.contains(deployment.root_path)
        assert allowed.contains(f"{deployment.root_path}/environments/.env")
    assert not allowed.contains("/opt/rentals10/prod")
    assert not allowed.contains("/opt/rentals/../other")
    assert not allowed.contains("/opt")
