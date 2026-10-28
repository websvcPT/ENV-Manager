"""Argon2id password hashing and the rules for user-chosen passwords."""

import secrets
from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

_HASHER = PasswordHash((Argon2Hasher(),))

# Stored for an account that signs in through a provider and has no password. It is not the
# output of any hash function, so no password verifies against it.
UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105


class PasswordPolicyError(ValueError):
    """A user-chosen password does not meet the password rules."""


def hash_password(password: str) -> str:
    """Hash a password with Argon2id and a random salt."""
    return _HASHER.hash(password)


def verify_password(password: str, stored_hash: str) -> tuple[bool, str | None]:
    """Check a password against a stored hash.

    Returns whether it matches and, when the stored hash uses outdated parameters, a fresh hash
    the caller should persist. An unrecognised stored value never matches.
    """
    try:
        return _HASHER.verify_and_update(password, stored_hash)
    except UnknownHashError:
        return False, None


@lru_cache
def _decoy_hash() -> str:
    """A valid hash of a throwaway password, used to equalise timing for unknown users."""
    return hash_password("decoy-password-for-timing-only")


def verify_decoy(password: str) -> None:
    """Spend the same time as a real verification so unknown names are not distinguishable."""
    verify_password(password, _decoy_hash())


def check_password_rules(password: str, minimum_length: int) -> None:
    """Raise ``PasswordPolicyError`` when a user-chosen password is too short."""
    if len(password) < minimum_length:
        raise PasswordPolicyError(f"password must be at least {minimum_length} characters")


GENERATED_PASSWORD_LENGTH = 16
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SYMBOLS = "!@#%^*-_+="
_PASSWORD_CLASSES = (_LOWER, _UPPER, _DIGITS, _SYMBOLS)


def generate_password() -> str:
    """A random 16 character password from a mixed alphabet, using the secure generator.

    Visually ambiguous characters are left out and every character class is always present.
    """
    characters = [secrets.choice(group) for group in _PASSWORD_CLASSES]
    alphabet = "".join(_PASSWORD_CLASSES)
    characters += [secrets.choice(alphabet) for _ in range(GENERATED_PASSWORD_LENGTH - 4)]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)
