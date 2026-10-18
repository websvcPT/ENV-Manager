"""Tests for the TOTP service, including encryption of the stored secret."""

import pyotp
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_session_factory
from app.models import User
from app.models.types import utc_now
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


def test_matching_step_reports_the_time_step_inside_the_window(crypto: CryptoService) -> None:
    """The current and adjacent steps match and name their step; others and junk do not."""
    user = make_user()
    user.id = 1
    secret = totp.generate_secret()
    totp.store_secret(user, secret, crypto)
    generator = pyotp.TOTP(secret)
    moment = utc_now()
    current = generator.timecode(moment)
    assert totp.matching_step(user, generator.generate_otp(current), crypto, now=moment) == current
    for neighbour in (current - 1, current + 1):
        code = generator.generate_otp(neighbour)
        assert totp.matching_step(user, code, crypto, now=moment) == neighbour
    assert totp.matching_step(user, generator.generate_otp(current + 2), crypto, now=moment) is None
    assert totp.matching_step(user, "abcdef", crypto, now=moment) is None


def test_enable_marks_the_confirmation_code_as_used(crypto: CryptoService) -> None:
    """The code that turned the factor on is recorded; storing or removing a secret clears it."""
    user = make_user()
    user.id = 1
    secret = totp.generate_secret()
    totp.store_secret(user, secret, crypto)
    assert totp.enable(user, pyotp.TOTP(secret).now(), crypto) is True
    assert user.totp_last_step == pyotp.TOTP(secret).timecode(utc_now())
    totp.disable(user)
    assert user.totp_last_step is None


def test_enable_records_when_it_was_turned_on_and_disable_clears_it(
    crypto: CryptoService,
) -> None:
    """The date the account page shows is set by enabling and cleared by removing."""
    user = make_user()
    user.id = 1
    secret = totp.generate_secret()
    totp.store_secret(user, secret, crypto)
    assert user.totp_enabled_at is None
    moment = utc_now()
    assert totp.enable(user, pyotp.TOTP(secret).now(), crypto, now=moment) is True
    assert user.totp_enabled_at == moment
    totp.disable(user)
    assert user.totp_enabled_at is None


def test_activate_stores_the_confirmed_secret_as_ciphertext_and_marks_the_step_used(
    crypto: CryptoService,
) -> None:
    """A secret that no code confirmed yet is not on the user; activating puts it there sealed."""
    user = make_user()
    user.id = 1
    secret = totp.generate_secret()
    assert user.totp_secret is None
    moment = utc_now()
    step = totp.step_for_secret(secret, pyotp.TOTP(secret).at(moment), now=moment)
    assert step == pyotp.TOTP(secret).timecode(moment)
    totp.activate(user, secret, step or 0, crypto, now=moment)
    assert user.totp_enabled and user.totp_enabled_at == moment and user.totp_last_step == step
    assert user.totp_secret is not None and secret not in user.totp_secret
    assert totp.read_secret(user, crypto) == secret


def test_step_for_secret_needs_no_stored_secret_and_refuses_junk() -> None:
    """The window check works on a plain secret, for a setup that is still pending."""
    secret = totp.generate_secret()
    generator = pyotp.TOTP(secret)
    moment = utc_now()
    code = generator.at(moment)
    assert totp.step_for_secret(secret, f"{code[:3]} {code[3:]}", now=moment)
    assert totp.step_for_secret(secret, "12ab56", now=moment) is None
    assert totp.step_for_secret(secret, "", now=moment) is None


async def test_claim_step_accepts_a_newer_step_once(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """A step is claimed once; the same or an older step is refused afterwards."""
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        assert await totp.claim_step(db, user, 100) is True
        assert await totp.claim_step(db, user, 100) is False
        assert await totp.claim_step(db, user, 99) is False
        assert await totp.claim_step(db, user, 101) is True
