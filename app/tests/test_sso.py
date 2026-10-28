"""Tests for starting a single sign in and finishing it: the stored attempt, the state and its
binding, and the session a success starts."""

import base64
import hashlib
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoError, CryptoService
from app.core.db import create_session_factory
from app.models import AuditEntry, AuditResult, SsoLogin, User, UserGroup, UserSession
from app.models.types import utc_now
from app.services.login import LoginStatus
from app.services.sessions import hash_token
from app.services.sso import redirect_uri
from app.services.sso_logins import verifier_context
from app.services.sso_refusal import (
    BINDING_MISMATCH,
    NOT_CONFIGURED,
    PROVIDER_REFUSED,
    STATE_INVALID,
    SsoRefusal,
)
from app.tests.auth_helpers import make_user
from app.tests.sso_helpers import (
    BASE_URL,
    CLIENT_ID,
    CODE,
    EMAIL,
    ISSUER,
    REDIRECT_URI,
    SUBJECT,
    World,
    build_world,
    good_claims,
)


@pytest.fixture
def world(engine: AsyncEngine, crypto: CryptoService) -> World:
    """The application's side of a sign in against a fresh fake provider."""
    return build_world(engine, crypto)


async def _audit(world: World, action: str) -> list[AuditEntry]:
    async with create_session_factory(world.engine)() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def _logins(world: World) -> list[SsoLogin]:
    async with create_session_factory(world.engine)() as db:
        return list((await db.execute(select(SsoLogin))).scalars())


def test_the_redirect_address_is_the_callback_of_the_base_url() -> None:
    assert redirect_uri("http://app.example.test/") == "http://app.example.test/login/sso/callback"
    assert redirect_uri(BASE_URL) == REDIRECT_URI


async def test_start_sends_the_browser_to_the_provider_with_pkce_state_and_nonce(
    world: World,
) -> None:
    await world.configure()
    begun = await world.begin()
    url = urlsplit(begun.started.url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/protocol/openid-connect/auth"
    query = begun.query
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["scope"] == ["openid profile email"]
    assert query["code_challenge_method"] == ["S256"]
    for name in ("state", "nonce", "code_challenge"):
        assert len(query[name][0]) >= 32
    assert begun.started.binding_token not in begun.started.url


async def test_the_stored_attempt_holds_hashes_and_a_verifier_sealed_to_its_own_row(
    world: World, crypto: CryptoService
) -> None:
    await world.configure()
    begun = await world.begin()
    state, nonce = begun.query["state"][0], begun.query["nonce"][0]
    [row] = await _logins(world)
    assert row.state_hash == hash_token(state)
    assert row.binding_hash == hash_token(begun.started.binding_token)
    assert row.nonce == nonce
    assert row.expires_at - row.created_at == timedelta(minutes=5)
    stored = " ".join([row.state_hash, row.binding_hash, row.nonce, row.code_verifier_encrypted])
    assert state not in stored and begun.started.binding_token not in stored
    verifier = crypto.decrypt_text(
        row.code_verifier_encrypted, context=verifier_context(row.state_hash)
    )
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = begun.query["code_challenge"][0]
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert verifier not in stored
    with pytest.raises(CryptoError):
        crypto.decrypt_text(row.code_verifier_encrypted, context=verifier_context("0" * 64))


async def test_every_attempt_has_its_own_state_nonce_and_binding(world: World) -> None:
    await world.configure()
    first, second = await world.begin(), await world.begin()
    for name in ("state", "nonce", "code_challenge"):
        assert first.query[name] != second.query[name]
    assert first.started.binding_token != second.started.binding_token


async def test_discovery_and_keys_are_fetched_once_and_then_served_from_memory(
    world: World,
) -> None:
    await world.configure()
    for _ in range(3):
        await world.sign_in()
    discovery = f"{ISSUER}/.well-known/openid-configuration"
    assert world.idp.count(discovery) == 1
    assert world.idp.count(world.idp.jwks_uri) == 1


async def test_start_is_refused_and_audited_when_sign_in_is_not_set_up(world: World) -> None:
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == NOT_CONFIGURED
    [entry] = await _audit(world, "login.failure")
    assert entry.result is AuditResult.FAILURE
    assert entry.details == {"method": "sso", "reason": NOT_CONFIGURED}
    assert await _logins(world) == []
    assert world.idp.seen == []


@pytest.mark.parametrize(
    "changes",
    [{"enabled": False}, {"client_secret": ""}],
)
async def test_start_needs_it_switched_on_and_a_stored_client_secret(
    world: World, changes: dict[str, object]
) -> None:
    await world.configure(**changes)
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == NOT_CONFIGURED


async def test_a_success_starts_a_session_and_audits_the_way_of_signing_in(
    world: World,
) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    result = await world.sign_in()
    assert result.status is LoginStatus.SUCCESS
    assert result.token and result.user is not None and result.user.email == EMAIL
    async with create_session_factory(world.engine)() as db:
        [session] = (await db.execute(select(UserSession))).scalars().all()
        user = (await db.execute(select(User))).scalar_one()
    assert session.token_hash == hash_token(result.token)
    assert session.ip_address == "203.0.113.9"
    assert user.last_login_at is not None
    [entry] = await _audit(world, "login.success")
    assert entry.details == {"method": "sso"}
    assert entry.result is AuditResult.SUCCESS and entry.actor_user_id == user.id
    assert await _logins(world) == []


async def test_a_state_can_be_used_only_once(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    begun = await world.begin()
    assert (await world.finish(begun)).status is LoginStatus.SUCCESS
    with pytest.raises(SsoRefusal) as info:
        await world.finish(begun)
    assert info.value.reason == STATE_INVALID
    async with create_session_factory(world.engine)() as db:
        sessions = (await db.execute(select(func.count()).select_from(UserSession))).scalar_one()
    assert sessions == 1


async def test_an_unknown_or_empty_state_is_refused(world: World) -> None:
    await world.configure()
    begun = await world.begin()
    for state in ("not-a-state", ""):
        with pytest.raises(SsoRefusal) as info:
            await world.finish(begun, state=state)
        assert info.value.reason == STATE_INVALID
    assert len(await _logins(world)) == 1


async def test_an_expired_state_is_refused_and_removed(world: World) -> None:
    await world.configure()
    begun = await world.begin(now=utc_now() - timedelta(minutes=6))
    with pytest.raises(SsoRefusal) as info:
        await world.finish(begun)
    assert info.value.reason == STATE_INVALID
    assert await _logins(world) == []


async def test_a_state_just_inside_its_five_minutes_is_accepted(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    begun = await world.begin(now=utc_now() - timedelta(minutes=4, seconds=50))
    assert (await world.finish(begun)).status is LoginStatus.SUCCESS


@pytest.mark.parametrize("binding", ["another-browsers-token", ""])
async def test_a_browser_that_did_not_start_the_attempt_is_refused(
    world: World, binding: str
) -> None:
    await world.configure()
    begun = await world.begin()
    with pytest.raises(SsoRefusal) as info:
        await world.finish(begun, binding=binding)
    assert info.value.reason == BINDING_MISMATCH
    assert await _logins(world) == []
    assert world.idp.token_requests == []


async def test_the_attempt_is_used_up_even_when_the_binding_is_wrong(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    begun = await world.begin()
    with pytest.raises(SsoRefusal):
        await world.finish(begun, binding="stolen-state-without-the-cookie")
    with pytest.raises(SsoRefusal) as info:
        await world.finish(begun)
    assert info.value.reason == STATE_INVALID


async def test_a_provider_error_or_a_missing_code_ends_the_attempt(world: World) -> None:
    await world.configure()
    for error in ("access_denied", None):
        begun = await world.begin()
        with pytest.raises(SsoRefusal) as info:
            await world.finish(begun, provider_error=error, code=None)
        assert info.value.reason == PROVIDER_REFUSED
    assert world.idp.token_requests == []
    assert await _logins(world) == []


async def test_the_provider_gets_the_code_the_client_secret_and_the_pkce_verifier(
    world: World,
) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    assert (await world.sign_in()).status is LoginStatus.SUCCESS
    [request] = world.idp.token_requests
    assert request.headers["Authorization"].startswith("Basic ")
    assert f"code={CODE}" in request.content.decode()
    assert dict(request.headers)["content-type"].startswith("application/x-www-form-urlencoded")


async def test_the_secret_goes_in_the_body_when_that_is_the_only_method_offered(
    world: World,
) -> None:
    world.idp.auth_methods = ["client_secret_post"]
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    assert (await world.sign_in()).status is LoginStatus.SUCCESS
    [request] = world.idp.token_requests
    assert "Authorization" not in request.headers
    assert "client_secret=" in request.content.decode()


async def test_a_refusal_audit_names_the_reason_and_the_email_and_nothing_the_provider_sent(
    world: World,
) -> None:
    await world.configure(create_users=False)
    begun = await world.begin()
    with pytest.raises(SsoRefusal):
        await world.finish(begun)
    [entry] = await _audit(world, "login.failure")
    assert entry.details == {"method": "sso", "reason": "creation_off", "email": EMAIL}
    assert entry.actor_name is None and entry.ip_address == "203.0.113.9"
    assert SUBJECT not in str(entry.details)


async def test_no_token_code_secret_or_state_reaches_any_audit_entry(
    world: World, crypto: CryptoService
) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.configure()
    good = await world.begin()
    await world.finish(good)
    bad = await world.begin()
    token = world.idp.sign(good_claims(bad.query["nonce"][0], aud="someone-else"))
    with pytest.raises(SsoRefusal):
        await world.finish(bad, token=lambda _: token)
    async with create_session_factory(world.engine)() as db:
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    text = " ".join(f"{entry.action} {entry.details} {entry.target_name}" for entry in entries)
    secrets_seen = [
        token,
        world.idp.id_token,
        CODE,
        good.query["state"][0],
        good.query["nonce"][0],
        bad.query["state"][0],
        good.started.binding_token,
        "client-secret-for-tests",
    ]
    for secret in secrets_seen:
        assert secret not in text
    assert len(entries) >= 3
