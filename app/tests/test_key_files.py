"""Reading existing key files and creating the new one: what is refused and what is written."""

import stat
from pathlib import Path

import pytest

from app.core.crypto import KeyConfigurationError, generate_master_key, parse_master_key
from app.services.key_files import prepare_new_key, read_key_file, same_key

CURRENT = parse_master_key(generate_master_key())


def test_a_missing_file_is_created_with_a_fresh_key_that_only_its_owner_can_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "new.key"
    created = prepare_new_key(path, CURRENT)
    assert created.created and len(created.key) == 32 and not same_key(created.key, CURRENT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_key_file(path) == created.key


def test_two_new_keys_are_never_the_same(tmp_path: Path) -> None:
    first = prepare_new_key(tmp_path / "one.key", CURRENT)
    second = prepare_new_key(tmp_path / "two.key", CURRENT)
    assert not same_key(first.key, second.key)


def test_an_existing_valid_key_file_is_used_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "new.key"
    text = generate_master_key() + "\n"
    path.write_text(text, encoding="ascii")
    path.chmod(0o640)
    used = prepare_new_key(path, CURRENT)
    assert not used.created and used.key == parse_master_key(text)
    assert path.read_text(encoding="ascii") == text
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize("content", ["", "not a key", "dG9vIHNob3J0", "\xe9" * 44])
def test_an_existing_file_that_is_not_a_key_is_refused_and_left_alone(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "new.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(KeyConfigurationError):
        prepare_new_key(path, CURRENT)
    assert path.read_text(encoding="utf-8") == content


def test_the_key_in_use_now_is_refused_as_the_new_key(tmp_path: Path) -> None:
    path = tmp_path / "same.key"
    path.write_text(generate_master_key(), encoding="ascii")
    other = parse_master_key(path.read_text(encoding="ascii"))
    with pytest.raises(KeyConfigurationError, match="in use now"):
        prepare_new_key(path, other)


def test_a_missing_folder_is_refused_so_the_key_cannot_vanish_with_a_container(
    tmp_path: Path,
) -> None:
    with pytest.raises(KeyConfigurationError, match="folder of the new key file"):
        prepare_new_key(tmp_path / "unmounted" / "new.key", CURRENT)
    assert not (tmp_path / "unmounted").exists()


def test_a_link_or_a_folder_is_not_a_key_file(tmp_path: Path) -> None:
    real = tmp_path / "real.key"
    real.write_text(generate_master_key(), encoding="ascii")
    link = tmp_path / "link.key"
    link.symlink_to(real)
    with pytest.raises(KeyConfigurationError, match="symbolic link"):
        prepare_new_key(link, CURRENT)
    with pytest.raises(KeyConfigurationError, match="not a file"):
        prepare_new_key(tmp_path, CURRENT)
    dangling = tmp_path / "dangling.key"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(KeyConfigurationError, match="symbolic link"):
        prepare_new_key(dangling, CURRENT)


def test_a_folder_that_cannot_be_written_is_reported_without_a_path() -> None:
    """A file cannot be created below /proc by anyone, root included."""
    with pytest.raises(KeyConfigurationError, match="cannot be created") as refused:
        prepare_new_key(Path("/proc/new.key"), CURRENT)
    assert "/proc" not in str(refused.value)
