"""The single sign-on routes and the sign-in card, on the seeded application with a fake provider.

A sign in goes through the real link and callback; the provider is the in-process one of
``sso_helpers``. The refusals are read as the person sees them: only fixed sentences, never what
the provider or the address carried.
"""

import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.config import get_settings
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.sessions import SESSION_COOKIE_NAME, SSO_BINDING_COOKIE_NAME
from app.models import AuditEntry, LoginCounter, LoginCounterKind, User, UserSession
from app.seeds import DEV_PASSWORD, seed_email
from app.services import sso_refusal as reasons
from app.services.sso_refusal import MESSAGES
from app.tests.conftest import sign_in
from app.tests.sso_helpers import BASE_URL, CODE, EMAIL, ISSUER, World
from app.tests.sso_web_helpers import (
    audit_entries,
    begin,
    come_back,
    link,
    sign_in_with_sso,
    sso_world,
    visible,
)


@pytest.fixture(autouse=True)
def base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address the application is reached at, before the application starts."""
    monkeypatch.setenv("ENVMANAGER_BASE_URL", BASE_URL)


@pytest.fixture
def world(web_app: FastAPI) -> World:
    """The application's side of a sign in against a fresh fake provider."""
    return sso_world(web_app)


async def _users(web_app: FastAPI) -> list[User]:
    async with web_app.state.session_factory() as db:
        return list((await db.execute(select(User))).scalars())


async def _failures(web_app: FastAPI) -> list[AuditEntry]:
    return await audit_entries(web_app, "login.failure")


def _bound_cookie(response: httpx.Response) -> str:
    """The Set-Cookie line that sets the binding cookie of the attempt."""
    lines = [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{SSO_BINDING_COOKIE_NAME}=")
    ]
    assert len(lines) == 1
    return lines[0].lower()


async def test_the_link_sends_the_browser_to_the_provider_with_a_bound_cookie(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    response = await web_client.get("/login/sso")
    target = urlsplit(response.headers["location"])
    assert response.status_code == 303
    assert f"{target.scheme}://{target.netloc}{target.path}" == (
        f"{ISSUER}/protocol/openid-connect/auth"
    )
    assert response.headers["cache-control"] == "no-store"
    cookie = _bound_cookie(response)
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "path=/login/sso" in cookie and "max-age=300" in cookie
    assert "secure" not in cookie


async def test_the_binding_cookie_is_secure_under_an_https_base_url(
    web_client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.configure()
    monkeypatch.setenv("ENVMANAGER_BASE_URL", "https://app.example.test")
    get_settings.cache_clear()
    response = await web_client.get("/login/sso")
    assert response.status_code == 303
    assert "secure" in _bound_cookie(response).split("; ")


async def test_the_link_goes_back_to_the_card_when_sign_in_is_off(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    response = await web_client.get("/login/sso")
    assert (response.status_code, response.headers["location"]) == (303, "/login?sso=off")
    assert SSO_BINDING_COOKIE_NAME not in web_client.cookies
    card = await web_client.get(response.headers["location"])
    assert MESSAGES[reasons.NOT_CONFIGURED] in visible(card.text)
    assert 'href="/login/sso"' not in card.text
    assert await _failures(web_app) == []


async def test_the_off_message_is_only_shown_while_sign_in_is_off(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    card = await web_client.get("/login?sso=off")
    assert 'data-fragment="login-message"' not in card.text
    assert 'href="/login/sso"' in card.text


async def test_the_link_for_a_signed_in_user_goes_to_projects(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await sign_in(web_client, "root")
    await world.configure()
    response = await web_client.get("/login/sso")
    assert (response.status_code, response.headers["location"]) == (303, "/projects")


async def test_the_link_reports_a_provider_that_cannot_be_reached(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    world.idp.down = True
    response = await web_client.get("/login/sso")
    assert response.status_code == 503
    assert MESSAGES[reasons.PROVIDER_UNAVAILABLE] in visible(response.text)
    assert SSO_BINDING_COOKIE_NAME not in web_client.cookies
    assert [entry.details["reason"] for entry in await _failures(web_app)] == [
        reasons.PROVIDER_UNAVAILABLE
    ]


async def test_a_sign_in_creates_the_session_and_lands_on_projects(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    response = await sign_in_with_sso(web_client, world)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    cookies = response.headers.get_list("set-cookie")
    session = next(line for line in cookies if line.startswith(f"{SESSION_COOKIE_NAME}="))
    assert "httponly" in session.lower() and "samesite=lax" in session.lower()
    used_up = next(line for line in cookies if line.startswith(f"{SSO_BINDING_COOKIE_NAME}="))
    assert "max-age=0" in used_up.lower()
    assert (await web_client.get("/projects")).status_code == 200
    created = [user for user in await _users(web_app) if user.email == EMAIL]
    assert [user.group.value for user in created] == ["admin"]
    success = await audit_entries(web_app, "login.success")
    assert success[-1].details == {"method": "sso"}
    async with web_app.state.session_factory() as db:
        assert (await db.execute(select(UserSession))).scalars().first() is not None


async def test_a_linked_seeded_account_signs_in_without_its_password(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    await link(web_app, "viewer", "sub-viewer")
    response = await sign_in_with_sso(
        web_client,
        world,
        sub="sub-viewer",
        email=seed_email("viewer"),
        resource_access={"env-manager": {"roles": ["env-viewer"]}},
    )
    assert response.status_code == 303
    assert (await web_client.get("/projects")).status_code == 200


async def test_a_replayed_callback_is_refused(web_client: httpx.AsyncClient, world: World) -> None:
    await world.configure()
    query = await begin(web_client, world)
    first = await come_back(web_client, world, query)
    web_client.cookies.delete(SESSION_COOKIE_NAME)
    replay = await come_back(web_client, world, query)
    assert first.status_code == 303
    assert replay.status_code == 401
    assert MESSAGES[reasons.STATE_INVALID] in visible(replay.text)
    assert SESSION_COOKIE_NAME not in web_client.cookies


async def test_a_missing_or_wrong_binding_cookie_is_refused(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    query = await begin(web_client, world)
    web_client.cookies.delete(SSO_BINDING_COOKIE_NAME, path="/login/sso")
    missing = await come_back(web_client, world, query)
    query = await begin(web_client, world)
    web_client.cookies.delete(SSO_BINDING_COOKIE_NAME, path="/login/sso")
    web_client.cookies.set(SSO_BINDING_COOKIE_NAME, "another-browser", path="/login/sso")
    wrong = await come_back(web_client, world, query)
    for response in (missing, wrong):
        assert response.status_code == 401
        assert MESSAGES[reasons.BINDING_MISMATCH] in visible(response.text)
    assert SESSION_COOKIE_NAME not in web_client.cookies
    assert [entry.details["reason"] for entry in await _failures(web_app)] == [
        reasons.BINDING_MISMATCH,
        reasons.BINDING_MISMATCH,
    ]


async def test_the_provider_error_is_never_shown(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    query = await begin(web_client, world)
    hostile = "<script>alert('x')</script>"
    response = await web_client.get(
        "/login/sso/callback",
        params={
            "state": query["state"][0],
            "error": hostile,
            "error_description": "secret detail of the provider",
            "code": CODE,
        },
    )
    page = visible(response.text)
    assert response.status_code == 401
    assert MESSAGES[reasons.PROVIDER_REFUSED] in page
    for echoed in (hostile, "alert(", "secret detail", query["state"][0], CODE):
        assert echoed not in page


async def test_a_callback_does_not_echo_what_it_was_given(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    response = await web_client.get(
        "/login/sso/callback", params={"state": "<b>marker-state</b>", "code": "marker-code"}
    )
    assert response.status_code == 401
    assert MESSAGES[reasons.STATE_INVALID] in visible(response.text)
    assert "marker-state" not in response.text and "marker-code" not in response.text


async def test_a_callback_while_sign_in_is_off_shows_the_fixed_sentence(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    response = await web_client.get("/login/sso/callback", params={"state": "x", "code": "y"})
    assert response.status_code == 503
    assert MESSAGES[reasons.NOT_CONFIGURED] in visible(response.text)
    assert await _failures(web_app) == []


Scenario = Callable[
    [httpx.AsyncClient, World, FastAPI, dict[str, list[str]]], Awaitable[httpx.Response]
]


async def _plain(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(http, world, query)


async def _bad_state(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(http, world, query, state="not-a-state")


async def _provider_error(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(http, world, query, error="access_denied", code=None)


async def _token_refused(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    world.idp.token_status = 500
    return await come_back(http, world, query)


async def _wrong_audience(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(http, world, query, changes={"aud": "another-client"})


async def _no_email(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(http, world, query, changes={"email": None})


async def _no_role(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    roles = {"env-manager": {"roles": ["something-else"]}}
    return await come_back(http, world, query, changes={"resource_access": roles})


async def _disabled(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    async with app.state.session_factory() as db:
        user = (await db.execute(select(User).where(User.name == "viewer"))).scalar_one()
        user.is_active = False
        await db.commit()
    return await come_back(
        http, world, query, changes={"sub": "sub-viewer", "email": seed_email("viewer")}
    )


async def _no_creation(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    await world.configure(create_users=False)
    return await come_back(http, world, query)


async def _unverified(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(
        http, world, query, changes={"email": seed_email("admin"), "email_verified": False}
    )


async def _unverified_new(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    return await come_back(http, world, query, changes={"email_verified": False})


async def _no_linking(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    await world.configure(link_by_email=False)
    return await come_back(http, world, query, changes={"email": seed_email("admin")})


async def _linked_elsewhere(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    await link(app, "admin", "sub-admin")
    return await come_back(http, world, query, changes={"email": seed_email("admin")})


async def _email_taken(
    http: httpx.AsyncClient, world: World, app: FastAPI, query: dict[str, list[str]]
) -> httpx.Response:
    await link(app, "admin", "sub-admin")
    return await come_back(
        http, world, query, changes={"sub": "sub-admin", "email": seed_email("viewer")}
    )


REFUSALS: list[tuple[str, int, Scenario]] = [
    (reasons.STATE_INVALID, 401, _bad_state),
    (reasons.PROVIDER_REFUSED, 401, _provider_error),
    (reasons.TOKEN_EXCHANGE_FAILED, 401, _token_refused),
    (reasons.TOKEN_INVALID, 401, _wrong_audience),
    (reasons.EMAIL_MISSING, 401, _no_email),
    (reasons.NO_MATCHING_ROLE, 401, _no_role),
    (reasons.USER_DISABLED, 401, _disabled),
    (reasons.CREATION_OFF, 401, _no_creation),
    (reasons.EMAIL_UNVERIFIED, 401, _unverified),
    (reasons.CREATION_UNVERIFIED, 401, _unverified_new),
    (reasons.LINK_OFF, 401, _no_linking),
    (reasons.ALREADY_LINKED, 401, _linked_elsewhere),
    (reasons.EMAIL_COLLISION, 401, _email_taken),
]


@pytest.mark.parametrize(("reason", "status", "scenario"), REFUSALS)
async def test_every_refusal_shows_its_fixed_sentence(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    world: World,
    reason: str,
    status: int,
    scenario: Scenario,
) -> None:
    await world.configure()
    query = await begin(web_client, world)
    response = await scenario(web_client, world, web_app, query)
    assert response.status_code == status
    page = visible(response.text)
    assert 'data-fragment="login-message"' in page
    assert "Single sign-on failed" in page
    assert MESSAGES[reason] in page
    assert SESSION_COOKIE_NAME not in web_client.cookies
    assert SSO_BINDING_COOKIE_NAME not in web_client.cookies
    last = (await _failures(web_app))[-1]
    assert last.details["method"] == "sso" and last.details["reason"] == reason


async def test_refusals_count_toward_the_lockout_of_the_address(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    web_app.state.login_ip_max_failures = 3
    await world.configure()
    for _ in range(2):
        refused = await web_client.get(
            "/login/sso/callback", params={"state": "unknown", "code": CODE}
        )
        assert refused.status_code == 401
    locking = await web_client.get("/login/sso/callback", params={"state": "unknown", "code": CODE})
    assert locking.status_code == 429
    assert "Account temporarily locked" in locking.text
    assert 0 < int(locking.headers["retry-after"]) <= 900
    async with web_app.state.session_factory() as db:
        counters = (await db.execute(select(LoginCounter))).scalars().all()
    assert [(c.kind, c.subject) for c in counters] == [(LoginCounterKind.IP, "127.0.0.1")]
    assert len(await audit_entries(web_app, "login.lockout")) == 1


async def test_a_locked_address_is_refused_by_both_routes(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    web_app.state.login_ip_max_failures = 2
    await world.configure()
    query = await begin(web_client, world)
    for _ in range(2):
        await web_client.get("/login/sso/callback", params={"state": "unknown"})
    link_answer = await web_client.get("/login/sso")
    assert link_answer.status_code == 429
    assert "Account temporarily locked" in link_answer.text
    assert "location" not in link_answer.headers
    valid = await come_back(web_client, world, query)
    assert valid.status_code == 429
    assert SESSION_COOKIE_NAME not in web_client.cookies
    blocked = [
        entry for entry in await _failures(web_app) if entry.details.get("reason") == "locked"
    ]
    assert len(blocked) == 2


async def test_a_good_sign_in_before_the_limit_does_not_count_as_a_failure(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    await sign_in_with_sso(web_client, world)
    async with web_app.state.session_factory() as db:
        assert (await db.execute(select(LoginCounter))).scalars().all() == []


async def test_the_card_offers_the_link_and_keeps_the_password_form_for_superadmins(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    page = (await web_client.get("/login")).text
    assert "Sign in with SSO" in page
    link_tag = re.search(r"<a [^>]*href=\"/login/sso\"[^>]*>", page)
    assert link_tag is not None
    assert "hx-" not in link_tag.group(0) and "<form" not in link_tag.group(0)
    assert re.search(r"<summary[^>]*>Sign in with password<", page) is not None
    assert 'id="user"' in page and 'id="pw"' in page
    assert "<details" in page and " open" not in page.split("<details", 1)[1].split(">", 1)[0]
    card = page.split('id="login-card"', 1)[1]
    assert "uperadmin" not in card
    assert "There is no password recovery." not in card


@pytest.mark.parametrize("create_users", [True, False])
async def test_the_card_never_names_who_may_use_the_password_form(
    web_client: httpx.AsyncClient, world: World, create_users: bool
) -> None:
    await world.configure(create_users=create_users)
    card = (await web_client.get("/login")).text.split('id="login-card"', 1)[1]
    assert "uperadmin" not in card
    assert "Accounts are created" not in card


async def test_the_card_is_unchanged_while_sign_in_is_off(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure(enabled=False)
    page = (await web_client.get("/login")).text
    assert "/login/sso" not in page and "<details" not in page and "SSO" not in page
    assert "Accounts are created by a superadmin. There is no password recovery." in page


async def test_the_password_form_stays_open_after_a_failed_password_attempt(
    web_client: httpx.AsyncClient, world: World
) -> None:
    await world.configure()
    await web_client.get("/healthz")
    response = await web_client.post(
        "/login",
        data={
            "identifier": "root@example.test",
            "password": "wrong",
            "csrf_token": web_client.cookies[CSRF_COOKIE_NAME],
        },
    )
    page = response.text
    assert response.status_code == 401 and "Invalid credentials" in page
    assert " open" in page.split("<details", 1)[1].split(">", 1)[0]
    assert 'href="/login/sso"' in page


async def test_sso_refusals_keep_the_sso_card(web_client: httpx.AsyncClient, world: World) -> None:
    await world.configure()
    response = await web_client.get("/login/sso/callback", params={"state": "x"})
    assert 'href="/login/sso"' in response.text
    assert "<details" in response.text


async def test_the_link_reports_a_provider_that_names_another_issuer(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    world.idp.discovery_issuer = "https://another.example.test/realms/other"
    response = await web_client.get("/login/sso")
    assert response.status_code == 503
    assert MESSAGES[reasons.ISSUER_MISMATCH] in visible(response.text)
    assert (await _failures(web_app))[-1].details["reason"] == reasons.ISSUER_MISMATCH


async def test_the_link_reports_a_provider_answer_that_cannot_be_read(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    await world.configure()
    world.idp.answers[f"{ISSUER}/.well-known/openid-configuration"] = lambda: httpx.Response(
        200, content=b"this is not json"
    )
    response = await web_client.get("/login/sso")
    assert response.status_code == 503
    assert MESSAGES[reasons.PROVIDER_INVALID] in visible(response.text)
    assert (await _failures(web_app))[-1].details["reason"] == reasons.PROVIDER_INVALID


async def test_sign_in_without_a_stored_secret_is_not_offered_and_passwords_keep_working(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    """Switched on but unable to work: no dead link, and nobody is shut out of the password."""
    await world.configure(client_secret="")
    page = (await web_client.get("/login")).text
    assert "/login/sso" not in page and "<details" not in page
    redirect = await web_client.get("/login/sso")
    assert (redirect.status_code, redirect.headers["location"]) == (303, "/login?sso=off")
    await web_client.get("/healthz")
    signed = await web_client.post(
        "/login",
        data={
            "identifier": seed_email("viewer"),
            "password": DEV_PASSWORD,
            "csrf_token": web_client.cookies[CSRF_COOKIE_NAME],
        },
    )
    assert (signed.status_code, signed.headers["location"]) == (303, "/")
    assert await _failures(web_app) == []


async def test_sso_refusals_lock_the_password_form_of_the_same_address(
    web_client: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    """One counter for the address: the lock the SSO refusals start also stops a password form."""
    web_app.state.login_ip_max_failures = 2
    await world.configure()
    for _ in range(2):
        await web_client.get("/login/sso/callback", params={"state": "unknown"})
    await web_client.get("/healthz")
    refused = await web_client.post(
        "/login",
        data={
            "identifier": seed_email("root"),
            "password": DEV_PASSWORD,
            "csrf_token": web_client.cookies[CSRF_COOKIE_NAME],
        },
    )
    assert refused.status_code == 429
    assert SESSION_COOKIE_NAME not in web_client.cookies
