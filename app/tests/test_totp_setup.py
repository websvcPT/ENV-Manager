"""The pending second factor setup: sealed, bound to its owner and session, and short lived."""

from datetime import timedelta

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.models import User, UserSession
from app.models.types import utc_now
from app.services import totp
from app.services.totp_setup import SETUP_LIFETIME, begin_setup, read_setup
from app.tests.auth_helpers import make_user


def owner_and_session(user_id: int = 1, session_id: int = 7) -> tuple[User, UserSession]:
    """An unsaved user and session with fixed ids."""
    user = make_user()
    user.id = user_id
    session = UserSession(user_id=user_id, token_hash="x" * 64, expires_at=utc_now())
    session.id = session_id
    return user, session


def test_a_setup_round_trips_through_its_sealed_value(crypto: CryptoService) -> None:
    user, session = owner_and_session()
    setup, sealed = begin_setup(user, session, crypto)
    assert setup.secret not in sealed and setup.setup_id not in sealed
    assert len(setup.secret) == 32 and setup.expires_at - utc_now() <= SETUP_LIFETIME
    assert read_setup(user, session, sealed, crypto) == setup


def test_every_setup_has_a_new_secret_and_id(crypto: CryptoService) -> None:
    user, session = owner_and_session()
    first, _ = begin_setup(user, session, crypto)
    second, _ = begin_setup(user, session, crypto)
    assert first.secret != second.secret and first.setup_id != second.setup_id


def test_a_setup_is_useless_to_another_user_or_session_or_key(crypto: CryptoService) -> None:
    user, session = owner_and_session()
    _, sealed = begin_setup(user, session, crypto)
    other_user, _ = owner_and_session(user_id=2)
    _, other_session = owner_and_session(session_id=8)
    assert read_setup(other_user, session, sealed, crypto) is None
    assert read_setup(user, other_session, sealed, crypto) is None
    foreign = CryptoService(parse_master_key(generate_master_key()))
    assert read_setup(user, session, sealed, foreign) is None


def test_a_setup_expires(crypto: CryptoService) -> None:
    user, session = owner_and_session()
    started = utc_now()
    _, sealed = begin_setup(user, session, crypto, now=started)
    assert read_setup(user, session, sealed, crypto, now=started + SETUP_LIFETIME / 2)
    late = started + SETUP_LIFETIME + timedelta(seconds=1)
    assert read_setup(user, session, sealed, crypto, now=late) is None


def test_missing_or_garbage_values_read_as_no_setup(crypto: CryptoService) -> None:
    user, session = owner_and_session()
    for value in (None, "", "garbage", "v1$AAAA", crypto.encrypt("not json", b"totp-setup:1:7")):
        assert read_setup(user, session, value, crypto) is None


def test_the_qr_code_is_an_svg_of_the_provisioning_uri_without_style_attributes() -> None:
    user = make_user()
    uri = totp.provisioning_uri(user, "JBSWY3DPEHPK3PXP")
    svg = totp.qr_svg(uri).decode()
    assert svg.startswith("<?xml") and "<svg" in svg and "viewBox" in svg
    assert 'width="' not in svg.split("<svg", 1)[1].split(">", 1)[0]
    assert "style=" not in svg and "<script" not in svg
    assert svg != totp.qr_svg(totp.provisioning_uri(user, "KRSXG5CTMVRXEZLU")).decode()
    assert "JBSWY3DPEHPK3PXP" not in svg
