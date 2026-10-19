"""Master key files for the key rotation and for restoring a backup made under an older key.

An existing key file must hold a valid key. A new one is created only when its path does not
exist, with a fresh random key, mode 0600 from the moment it exists, and never over another
file. Messages say what is wrong with a file and never repeat its content.
"""

import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from app.core.crypto import (
    KeyConfigurationError,
    generate_master_key,
    load_master_key,
    parse_master_key,
)

NEW_KEY_FILE_MODE = 0o600


@dataclass(frozen=True)
class NewKey:
    """The key to rotate to and whether this call wrote its file."""

    key: bytes
    created: bool


def read_key_file(path: Path) -> bytes:
    """The master key in an existing file, refusing a file that cannot be read or is no key."""
    return load_master_key(None, path)


def same_key(first: bytes, second: bytes) -> bool:
    """Whether two master keys are the same key."""
    return hmac.compare_digest(first, second)


def _create_key_file(path: Path) -> bytes:
    """Write a fresh key to a file that does not exist yet, owner-only from the start."""
    encoded = generate_master_key()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        NEW_KEY_FILE_MODE,
    )
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return parse_master_key(encoded)


def prepare_new_key(path: Path, current: bytes) -> NewKey:
    """Use the key in ``path``, or create the file with a new key when the path is free.

    The folder of the file must exist, so a key is never written to a folder that is not mounted
    and would vanish with a one-off container. An existing file must hold a valid key that is not
    the current one. Raises ``KeyConfigurationError`` with a message that is safe to show.
    """
    if path.is_symlink():
        raise KeyConfigurationError("the new key file is a symbolic link")
    if path.exists():
        if not path.is_file():
            raise KeyConfigurationError("the new key path is not a file")
        key = read_key_file(path)
        if same_key(key, current):
            raise KeyConfigurationError("the new key file holds the key that is in use now")
        return NewKey(key, created=False)
    if not path.parent.is_dir():
        raise KeyConfigurationError(
            "the folder of the new key file does not exist; mount a folder for it so the key "
            "outlives this command"
        )
    try:
        return NewKey(_create_key_file(path), created=True)
    except OSError as error:
        raise KeyConfigurationError("the new key file cannot be created") from error
