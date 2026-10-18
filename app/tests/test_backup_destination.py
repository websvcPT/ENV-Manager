"""The extra destination: checked when saved, checked again and confined when a backup is copied."""

import stat
from pathlib import Path

import pytest

from app.services.backup import backup_file_name
from app.services.backup_destination import (
    OUTSIDE_MESSAGE,
    ExtraDestinationError,
    check_extra_destination,
    copy_to_extra_destination,
)
from app.services.paths import AllowedPaths


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    """An allowed folder with a destination inside it, a folder outside and a backup file."""
    root = (tmp_path / "allowed").resolve()
    destination = root / "backups"
    destination.mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    name = backup_file_name("manual")
    (backup_dir / name).write_bytes(b"backup bytes")
    return {
        "root": root,
        "destination": destination,
        "outside": outside,
        "backup_dir": backup_dir,
        "file": backup_dir / name,
    }


def test_a_folder_inside_an_allowed_path_is_accepted(world: dict[str, Path]) -> None:
    """The real path is returned, and a symlink to a folder inside the list resolves to it."""
    allowed = AllowedPaths([world["root"]])
    assert check_extra_destination(allowed, str(world["destination"])) == world["destination"]
    link = world["root"] / "link"
    link.symlink_to(world["destination"])
    assert check_extra_destination(allowed, str(link)) == world["destination"]


@pytest.mark.parametrize("kind", ["outside", "symlink_out", "similar_name", "root_itself"])
def test_a_folder_outside_the_allowed_paths_is_refused(world: dict[str, Path], kind: str) -> None:
    """Outside, through a symlink, a path that only shares a prefix, and a parent of the list."""
    allowed = AllowedPaths([world["root"]])
    if kind == "outside":
        candidate = world["outside"]
    elif kind == "symlink_out":
        candidate = world["root"] / "escape"
        candidate.symlink_to(world["outside"])
    elif kind == "similar_name":
        candidate = Path(f"{world['root']}-copy")
        candidate.mkdir()
    else:
        candidate = world["root"].parent
    with pytest.raises(ExtraDestinationError) as refused:
        check_extra_destination(allowed, str(candidate))
    assert str(refused.value) == OUTSIDE_MESSAGE


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("relative/path", "Path must be absolute."),
        ("/allowed/../etc", "Path must not contain '..' components."),
    ],
)
def test_a_path_that_is_not_absolute_and_normal_is_refused(
    world: dict[str, Path], typed: str, message: str
) -> None:
    """The typed text is validated before the file system is asked."""
    with pytest.raises(ExtraDestinationError) as refused:
        check_extra_destination(AllowedPaths([world["root"]]), typed)
    assert str(refused.value) == message


def test_a_missing_folder_and_a_file_are_refused(world: dict[str, Path]) -> None:
    """Only an existing folder will do; nothing is created."""
    allowed = AllowedPaths([world["root"]])
    with pytest.raises(ExtraDestinationError, match="does not exist"):
        check_extra_destination(allowed, str(world["root"] / "later"))
    assert not (world["root"] / "later").exists()
    plain = world["root"] / "plain.txt"
    plain.write_text("x")
    with pytest.raises(ExtraDestinationError, match="not a folder"):
        check_extra_destination(allowed, str(plain))


def test_a_folder_that_cannot_be_written_is_refused(world: dict[str, Path]) -> None:
    """A folder the application has no write access to is refused with a plain message."""
    locked = world["root"] / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(ExtraDestinationError, match="cannot write"):
            check_extra_destination(AllowedPaths([world["root"]]), str(locked))
    finally:
        locked.chmod(0o700)


def test_a_backup_is_copied_with_mode_0600(world: dict[str, Path]) -> None:
    """The copy has the same bytes, is private and leaves the source where it is."""
    allowed = AllowedPaths([world["root"]])
    copy_to_extra_destination(
        world["backup_dir"], world["file"].name, str(world["destination"]), allowed
    )
    copied = world["destination"] / world["file"].name
    assert copied.read_bytes() == b"backup bytes"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert world["file"].exists()
    assert [entry.name for entry in world["destination"].iterdir()] == [copied.name]


def test_the_destination_is_checked_again_at_copy_time(world: dict[str, Path]) -> None:
    """A destination that left the allowed list since it was saved gets nothing."""
    with pytest.raises(ExtraDestinationError) as refused:
        copy_to_extra_destination(
            world["backup_dir"],
            world["file"].name,
            str(world["destination"]),
            AllowedPaths([world["outside"]]),
        )
    assert "outside the allowed paths" in str(refused.value)
    assert list(world["destination"].iterdir()) == []


def test_a_destination_swapped_for_a_symlink_out_is_not_followed(world: dict[str, Path]) -> None:
    """The saved folder was replaced by a link out of the list: nothing is written there."""
    world["destination"].rmdir()
    world["destination"].symlink_to(world["outside"])
    with pytest.raises(ExtraDestinationError) as refused:
        copy_to_extra_destination(
            world["backup_dir"],
            world["file"].name,
            str(world["destination"]),
            AllowedPaths([world["root"]]),
        )
    assert "outside the allowed paths" in str(refused.value)
    assert list(world["outside"].iterdir()) == []


def test_a_symlink_at_the_file_name_is_not_written_through(world: dict[str, Path]) -> None:
    """A link planted at the copy's name is refused and its target stays untouched."""
    victim = world["outside"] / "victim.txt"
    victim.write_text("precious")
    (world["destination"] / world["file"].name).symlink_to(victim)
    with pytest.raises(ExtraDestinationError):
        copy_to_extra_destination(
            world["backup_dir"],
            world["file"].name,
            str(world["destination"]),
            AllowedPaths([world["root"]]),
        )
    assert victim.read_text() == "precious"


def test_nothing_in_the_destination_is_removed(world: dict[str, Path]) -> None:
    """Existing files stay: the copy only adds."""
    keep = world["destination"] / "other.db"
    keep.write_bytes(b"old")
    copy_to_extra_destination(
        world["backup_dir"],
        world["file"].name,
        str(world["destination"]),
        AllowedPaths([world["root"]]),
    )
    assert keep.read_bytes() == b"old"


def test_a_missing_backup_file_is_a_fixed_message(world: dict[str, Path]) -> None:
    """Without the backup file there is nothing to copy, and the message names no path."""
    with pytest.raises(ExtraDestinationError) as refused:
        copy_to_extra_destination(
            world["backup_dir"],
            backup_file_name("manual"),
            str(world["destination"]),
            AllowedPaths([world["root"]]),
        )
    assert "/" not in str(refused.value)
