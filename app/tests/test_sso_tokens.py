"""Tests for what a sign in trusts from the provider: its metadata, its signing keys, the answer
of the token endpoint and every check of the ID token."""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.models import UserGroup
from app.models.types import utc_now
from app.services.login import LoginStatus
from app.services.sso_provider import SsoProvider
from app.services.sso_refusal import (
    ISSUER_MISMATCH,
    PROVIDER_INVALID,
    PROVIDER_UNAVAILABLE,
    TOKEN_EXCHANGE_FAILED,
    TOKEN_INVALID,
    SsoRefusal,
)
from app.tests.auth_helpers import make_user
from app.tests.sso_helpers import (
    CLIENT_ID,
    EMAIL,
    ISSUER,
    FakeIdp,
    World,
    build_world,
    ec_key,
    hmac_token,
    rsa_key,
    unsigned_token,
)

DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
SHARED_KEY = "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0"


@pytest.fixture
async def world(engine: AsyncEngine, crypto: CryptoService) -> World:
    """A fake provider, and a signed up account the tokens are for."""
    built = build_world(engine, crypto)
    await built.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await built.configure()
    return built


async def _refused(world: World, **kwargs: object) -> str:
    """The reason a sign in is refused with."""
    with pytest.raises(SsoRefusal) as info:
        await world.sign_in(**kwargs)
    return info.value.reason


def _later(seconds: int) -> int:
    """A time stamp that many seconds from now."""
    return int(time.time()) + seconds


async def test_a_good_token_signs_in(world: World) -> None:
    assert (await world.sign_in()).status is LoginStatus.SUCCESS


async def test_an_elliptic_curve_key_is_accepted_too(world: World) -> None:
    world.idp.keys = [ec_key("ec-1")]
    result = await world.sign_in(token=lambda c: world.idp.sign(c, alg="ES256", kid="ec-1"))
    assert result.status is LoginStatus.SUCCESS


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "another-client"},
        {"aud": ["another-client", "a-third"]},
        {"aud": None},
        {"aud": [CLIENT_ID, "another-client"], "azp": None},
        {"aud": [CLIENT_ID, "another-client"], "azp": "another-client"},
        {"azp": "another-client"},
        {"iss": "https://evil.example.test/realms/main"},
        {"iss": ISSUER + "/"},
        {"iss": None},
        {"nonce": "the-nonce-of-another-attempt"},
        {"nonce": None},
        {"exp": None},
        {"iat": None},
        {"sub": None},
        {"sub": ""},
        {"sub": "s" * 256},
    ],
)
async def test_a_token_that_fails_a_check_is_refused(
    world: World, changes: dict[str, object]
) -> None:
    assert await _refused(world, changes=changes) == TOKEN_INVALID


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": [CLIENT_ID, "another-client"]},
        {"aud": [CLIENT_ID], "azp": None},
    ],
)
async def test_the_audience_may_list_several_clients(
    world: World, changes: dict[str, object]
) -> None:
    assert (await world.sign_in(changes=changes)).status is LoginStatus.SUCCESS


@pytest.mark.parametrize(("claim", "seconds"), [("exp", -30), ("iat", 30)])
async def test_the_times_have_a_minute_of_leeway(world: World, claim: str, seconds: int) -> None:
    result = await world.sign_in(changes={claim: _later(seconds)})
    assert result.status is LoginStatus.SUCCESS


@pytest.mark.parametrize(("claim", "seconds"), [("exp", -90), ("iat", 90)])
async def test_a_time_beyond_the_leeway_is_refused(world: World, claim: str, seconds: int) -> None:
    assert await _refused(world, changes={claim: _later(seconds)}) == TOKEN_INVALID


async def test_the_clock_of_the_sign_in_decides_what_expired(world: World) -> None:
    begun = await world.begin()
    changes: dict[str, object] = {"exp": _later(60)}
    later = utc_now() + timedelta(minutes=3)
    with pytest.raises(SsoRefusal) as info:
        await world.finish(begun, changes=changes, now=later)
    assert info.value.reason == TOKEN_INVALID


async def test_a_token_with_no_signature_algorithm_is_refused(world: World) -> None:
    assert await _refused(world, token=unsigned_token) == TOKEN_INVALID


async def test_a_token_signed_with_a_shared_secret_is_refused(world: World) -> None:
    public = str(rsa_key("key-1").as_dict(private=False)).encode()
    assert await _refused(world, token=lambda c: hmac_token(c, public)) == TOKEN_INVALID
    assert await _refused(world, token=lambda c: hmac_token(c, b"guess")) == TOKEN_INVALID


async def test_a_signature_from_another_key_is_refused_without_fetching_the_keys_again(
    world: World,
) -> None:
    reason = await _refused(world, token=lambda c: world.idp.sign(c, key=rsa_key("attacker")))
    assert reason == TOKEN_INVALID
    assert world.idp.count(world.idp.jwks_uri) == 1


async def test_a_token_that_is_not_a_token_is_refused(world: World) -> None:
    for text in ("not.a.token", "a.b", "e30.e30.e30"):
        assert await _refused(world, token=lambda _, text=text: text) == TOKEN_INVALID


async def test_an_unknown_key_id_fetches_the_keys_once_more_and_then_signs_in(
    world: World,
) -> None:
    assert (await world.sign_in()).status is LoginStatus.SUCCESS
    assert world.idp.count(world.idp.jwks_uri) == 1
    world.idp.keys = [rsa_key("key-2")]
    rotated = rsa_key("key-2")
    result = await world.sign_in(token=lambda c: world.idp.sign(c, key=rotated, kid="key-2"))
    assert result.status is LoginStatus.SUCCESS
    assert world.idp.count(world.idp.jwks_uri) == 2


async def test_a_key_id_the_provider_does_not_have_is_refused_after_one_more_fetch(
    world: World,
) -> None:
    ghost = rsa_key("ghost")
    reason = await _refused(world, token=lambda c: world.idp.sign(c, key=ghost, kid="ghost"))
    assert reason == TOKEN_INVALID
    assert world.idp.count(world.idp.jwks_uri) == 2


async def test_the_token_endpoint_refusing_the_code_is_a_failed_exchange(world: World) -> None:
    assert await _refused(world, code="a-code-the-provider-never-gave") == TOKEN_EXCHANGE_FAILED


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_grant"}),
        (500, {"id_token": "x.y.z"}),
        (200, {"access_token": "only-an-access-token"}),
        (200, {"id_token": ""}),
        (200, {"id_token": 7}),
    ],
)
async def test_an_answer_without_a_usable_id_token_is_a_failed_exchange(
    world: World, status: int, body: dict[str, object]
) -> None:
    world.idp.token_status = status
    world.idp.token_body = body
    assert await _refused(world) == TOKEN_EXCHANGE_FAILED


async def test_a_provider_that_goes_down_after_the_redirect_is_unavailable(world: World) -> None:
    begun = await world.begin()
    world.idp.down = True
    with pytest.raises(SsoRefusal) as info:
        await world.finish(begun)
    assert info.value.reason == PROVIDER_UNAVAILABLE


async def test_start_reports_a_provider_that_is_down(world: World) -> None:
    world.idp.down = True
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == PROVIDER_UNAVAILABLE


async def test_a_discovery_document_for_another_issuer_is_refused(world: World) -> None:
    world.idp.discovery_issuer = "https://evil.example.test/realms/main"
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == ISSUER_MISMATCH


async def test_the_issuer_in_the_document_must_equal_the_configured_one_exactly(
    world: World,
) -> None:
    world.idp.discovery_issuer = ISSUER + "/"
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == ISSUER_MISMATCH


def _json(body: object, status: int = 200) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, json=body)


@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(200, text="<html>not json</html>"),
        _json(["not", "an", "object"]),
        _json({"issuer": ISSUER}),
        _json({"issuer": ISSUER, "authorization_endpoint": "javascript:alert(1)"}),
        _json(
            {
                "issuer": ISSUER,
                "authorization_endpoint": "ftp://idp.example.test/auth",
                "token_endpoint": "https://idp.example.test/token",
                "jwks_uri": "https://idp.example.test/keys",
            }
        ),
    ],
)
async def test_a_discovery_document_that_cannot_be_used_is_refused(
    world: World, answer: Callable[[], httpx.Response]
) -> None:
    world.idp.answers[DISCOVERY] = answer
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == PROVIDER_INVALID


@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(302, headers={"Location": "https://evil.example.test/"}),
        lambda: httpx.Response(404),
        lambda: httpx.Response(503),
    ],
)
async def test_a_redirect_or_an_error_from_the_provider_is_not_followed_or_trusted(
    world: World, answer: Callable[[], httpx.Response]
) -> None:
    world.idp.answers[DISCOVERY] = answer
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == PROVIDER_UNAVAILABLE
    assert [url for _, url in world.idp.seen] == [DISCOVERY]


async def test_an_answer_over_the_size_limit_is_refused(world: World) -> None:
    world.idp.answers[DISCOVERY] = lambda: httpx.Response(200, content=b" " * 1_100_000)
    with pytest.raises(SsoRefusal) as info:
        await world.begin()
    assert info.value.reason == PROVIDER_INVALID


@pytest.mark.parametrize(
    "answer",
    [
        _json({"keys": []}),
        _json({"keys": [{"kty": "oct", "k": SHARED_KEY, "kid": "shared"}]}),
        _json({"nothing": True}),
        _json({"keys": [{"kty": "RSA", "n": "x"}]}),
        lambda: httpx.Response(200, text="nope"),
    ],
)
async def test_signing_keys_that_cannot_be_used_are_refused(
    world: World, answer: Callable[[], httpx.Response]
) -> None:
    world.idp.answers[world.idp.jwks_uri] = answer
    assert await _refused(world) in {PROVIDER_INVALID, TOKEN_INVALID}


async def test_the_cached_metadata_and_keys_expire_after_their_lifetime() -> None:
    idp = FakeIdp()
    now = [datetime(2026, 1, 1, 12, 0)]
    provider = SsoProvider(idp.transport(), clock=lambda: now[0], ttl=timedelta(minutes=10))
    metadata = await provider.metadata(ISSUER)
    await provider.signing_keys(metadata)
    now[0] += timedelta(minutes=9)
    await provider.metadata(ISSUER)
    await provider.signing_keys(metadata)
    assert (idp.count(DISCOVERY), idp.count(idp.jwks_uri)) == (1, 1)
    now[0] += timedelta(minutes=2)
    await provider.metadata(ISSUER)
    await provider.signing_keys(metadata)
    assert (idp.count(DISCOVERY), idp.count(idp.jwks_uri)) == (2, 2)


async def test_two_providers_do_not_share_what_they_cached() -> None:
    first, second = FakeIdp(), FakeIdp()
    await SsoProvider(first.transport()).metadata(ISSUER)
    await SsoProvider(second.transport()).metadata(ISSUER)
    assert first.count(DISCOVERY) == 1 and second.count(DISCOVERY) == 1


def test_importing_the_sign_in_code_raises_no_deprecation_warning() -> None:
    """Authlib's httpx client warns on import, so the code uses the parts that do not."""
    script = (
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as seen:\n"
        "    warnings.simplefilter('always')\n"
        "    import app.services.sso\n"
        "    import app.services.sso_settings\n"
        "found = [str(w.message) for w in seen if issubclass(w.category, DeprecationWarning)\n"
        "         and ('authlib' in w.filename.lower() or 'joserfc' in w.filename.lower()\n"
        "              or 'authlib' in str(w.message).lower())]\n"
        "print(found, file=__import__('sys').stderr)\n"
        "raise SystemExit(1 if found else 0)\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=120
    )
    assert done.returncode == 0, done.stderr
