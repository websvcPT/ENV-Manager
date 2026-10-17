"""Tests for Argon2id hashing and password rules."""

import pytest

from app.core.passwords import (
    PasswordPolicyError,
    check_password_rules,
    hash_password,
    verify_password,
)


def test_hash_is_argon2id_and_salted() -> None:
    """Hashes use Argon2id and differ per call for the same password."""
    first = hash_password("a long enough secret")
    second = hash_password("a long enough secret")
    assert first.startswith("$argon2id$")
    assert first != second


def test_verify_accepts_right_and_rejects_wrong_password() -> None:
    """Only the original password verifies."""
    stored = hash_password("a long enough secret")
    assert verify_password("a long enough secret", stored)[0] is True
    assert verify_password("another secret", stored) == (False, None)


def test_unrecognised_stored_value_never_matches() -> None:
    """A stored value that is not a known hash fails closed."""
    assert verify_password("anything", "not-a-real-hash") == (False, None)


def test_minimum_length_is_enforced() -> None:
    """Short passwords are refused and long enough ones pass."""
    check_password_rules("x" * 12, 12)
    with pytest.raises(PasswordPolicyError):
        check_password_rules("x" * 11, 12)
