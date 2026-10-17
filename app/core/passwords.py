"""Argon2id password hashing and the rules for user-chosen passwords."""

from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

_HASHER = PasswordHash((Argon2Hasher(),))


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
