"""Tests for the TOTP service, including encryption of the stored secret."""

import pyotp
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_session_factory
from app.models import User
from app.services import totp
from app.tests.auth_helpers import make_user


async def test_stored_secret_is_ciphertext_and_round_trips(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """The column holds versioned ciphertext, never the base32 secret, and it decrypts back."""
    factory = create_session_factory(engine)
    secret = totp.generate_secret()
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        totp.store_secret(user, secret, crypto)
        await db.commit()
    async with factory() as db:
        raw = (await db.execute(text("SELECT totp_secret FROM users"))).scalar_one()
        stored = (await db.execute(select(User))).scalar_one()
    assert raw.startswith(crypto.tag)
    assert secret not in raw
    assert totp.read_secret(stored, crypto) == secret


def test_secret_is_bound_to_its_owner(crypto: CryptoService) -> None:
    """Copying one user's ciphertext to another user does not yield a usable secret."""
    owner, other = make_user("alice"), make_user("bob")
    owner.id, other.id = 1, 2
    totp.store_secret(owner, totp.generate_secret(), crypto)
    other.totp_secret = owner.totp_secret
    assert totp.read_secret(other, crypto) is None


def test_secret_is_unreadable_with_another_key(crypto: CryptoService) -> None:
    """A different master key cannot open the stored secret."""
    user = make_user()
    user.id = 1
    totp.store_secret(user, totp.generate_secret(), crypto)
    foreign = CryptoService(parse_master_key(generate_master_key()))
    assert totp.read_secret(user, foreign) is None
    assert totp.verify_code(user, "123456", foreign) is False


def test_enable_requires_a_valid_code(crypto: CryptoService) -> None:
    """Storing a secret does not enforce it; a correct code enables it, a wrong one does not."""
    user = make_user()
    user.id = 1
    secret = totp.generate_secret()
    totp.store_secret(user, secret, crypto)
    assert user.totp_enabled is False
    assert totp.enable(user, "000000x", crypto) is False
    assert user.totp_enabled is False
    assert totp.enable(user, pyotp.TOTP(secret).now(), crypto) is True
    assert user.totp_enabled is True


def test_disable_discards_the_secret(crypto: CryptoService) -> None:
    """Removing the second factor clears the secret and the flag."""
    user = make_user()
    user.id = 1
    totp.store_secret(user, totp.generate_secret(), crypto)
    user.totp_enabled = True
    totp.disable(user)
    assert (user.totp_secret, user.totp_enabled) == (None, False)
