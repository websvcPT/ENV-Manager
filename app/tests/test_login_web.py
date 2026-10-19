"""Sign-in screens, session expiry redirects and the second factor step, on the seeded app."""

import re

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.core.sessions import PENDING_LOGIN_COOKIE_NAME, SESSION_COOKIE_NAME
from app.models import AuditEntry, LoginCounter, LoginCounterKind
from app.seeds import DEV_PASSWORD, DEV_TOTP_SECRET
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, assert_never_cached


async def post_form(
    http: httpx.AsyncClient, path: str, data: dict[str, str], *, htmx: bool = False
) -> httpx.Response:
    """POST a form with the CSRF field the application issued."""
    await http.get("/healthz")
    form = {**data, "csrf_token": http.cookies[CSRF_COOKIE_NAME]}
    return await http.post(path, data=form, headers=HTMX if htmx else None)


async def test_login_page_renders_the_card(web_client: httpx.AsyncClient) -> None:
    """The page has the product name, both fields, the note and no recovery link."""
    response = await web_client.get("/login")
    page = response.text
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    for marker in (
        'data-fragment="login-card"',
        "ENV Manager",
        'id="user"',
        'id="pw"',
        'name="csrf_token"',
        "Accounts are created by a superadmin. There is no password recovery.",
        'data-js="theme-toggle"',
    ):
        assert marker in page
    assert 'data-fragment="login-message"' not in page
    assert "recover" not in page.replace("no password recovery", "").lower()


async def test_login_page_for_a_signed_in_user_goes_to_projects(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get("/login")
    assert (response.status_code, response.headers["location"]) == (303, "/projects")


async def test_session_expired_state_on_the_login_page(web_client: httpx.AsyncClient) -> None:
    page = (await web_client.get("/login?expired=1")).text
    assert 'role="status" data-fragment="login-message"' in page
    assert "Your session expired. Sign in again to continue." in page


async def test_invalid_credentials_keep_the_email_and_drop_the_password(
    web_client: httpx.AsyncClient,
) -> None:
    """A plain post answers the whole page, with one generic message for every failure."""
    wrong = await post_form(
        web_client, "/login", {"identifier": "root@example.test", "password": "nope"}
    )
    unknown = await post_form(
        web_client, "/login", {"identifier": "nobody@example.test", "password": "nope"}
    )
    by_name = await post_form(
        web_client, "/login", {"identifier": "root", "password": DEV_PASSWORD}
    )
    assert by_name.status_code == 401 and "Invalid credentials" in by_name.text
    assert wrong.status_code == unknown.status_code == 401
    assert "Invalid credentials" in wrong.text and "Invalid credentials" in unknown.text
    assert 'role="alert" data-fragment="login-message"' in wrong.text
    assert 'value="root@example.test"' in wrong.text
    assert 'value="root"' in by_name.text
    assert "nope" not in wrong.text
    assert wrong.text.count("<html") == 1


async def test_htmx_failure_answers_only_the_card(web_client: httpx.AsyncClient) -> None:
    response = await post_form(
        web_client, "/login", {"identifier": "root@example.test", "password": "nope"}, htmx=True
    )
    assert response.status_code == 401
    assert response.text.lstrip().startswith('<div id="login-card"')
    assert "<html" not in response.text
    assert "HX-Request" in response.headers["vary"]


async def test_lockout_shows_the_time_left_and_disables_the_form(
    web_client: httpx.AsyncClient,
) -> None:
    for _ in range(5):
        response = await post_form(
            web_client,
            "/login",
            {"identifier": "viewer@example.test", "password": "nope"},
            htmx=True,
        )
    assert response.status_code == 429
    assert 0 < int(response.headers["retry-after"]) <= 900
    assert "Account temporarily locked" in response.text
    assert "Try again in 15:00." in response.text
    assert len(re.findall(r"\sdisabled[\s>]", response.text)) == 3
    refused = await post_form(
        web_client,
        "/login",
        {"identifier": "viewer@example.test", "password": DEV_PASSWORD},
        htmx=True,
    )
    assert refused.status_code == 429


async def test_successful_login_redirects(web_client: httpx.AsyncClient) -> None:
    """Plain posts get a 303, htmx gets HX-Redirect; both set a hardened session cookie."""
    form = {"identifier": "root@example.test", "password": DEV_PASSWORD}
    plain = await post_form(web_client, "/login", form)
    assert (plain.status_code, plain.headers["location"]) == (303, "/")
    web_client.cookies.delete(SESSION_COOKIE_NAME)
    fragment = await post_form(web_client, "/login", form, htmx=True)
    assert (fragment.status_code, fragment.headers["hx-redirect"]) == (200, "/")
    assert_never_cached(fragment)
    assert "httponly" in fragment.headers["set-cookie"].lower()
    assert (await web_client.get("/projects")).status_code == 200


CREDENTIALS = {"identifier": "totp_user@example.test", "password": DEV_PASSWORD}


async def ask_for_code(http: httpx.AsyncClient) -> httpx.Response:
    """Send the password of the second factor user and return the code card answer."""
    asked = await post_form(http, "/login", CREDENTIALS, htmx=True)
    assert asked.status_code == 200 and 'action="/login/totp"' in asked.text
    return asked


async def test_second_factor_step_carries_no_credentials(web_client: httpx.AsyncClient) -> None:
    """The code card holds neither name nor password; a hardened cookie holds the pending login."""
    asked = await ask_for_code(web_client)
    assert 'name="identifier"' not in asked.text and 'name="password"' not in asked.text
    assert DEV_PASSWORD not in asked.text
    assert SESSION_COOKIE_NAME not in web_client.cookies
    cookie = next(
        value
        for value in asked.headers.get_list("set-cookie")
        if value.startswith(PENDING_LOGIN_COOKIE_NAME)
    ).lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/login" in cookie and "max-age=300" in cookie


async def test_second_factor_step(web_client: httpx.AsyncClient) -> None:
    """A wrong code is a field error with a fresh pending login; the right code signs in."""
    await ask_for_code(web_client)
    first = web_client.cookies[PENDING_LOGIN_COOKIE_NAME]

    wrong = await post_form(web_client, "/login/totp", {"code": "000000"}, htmx=True)
    assert wrong.status_code == 401
    assert "The code is incorrect or expired." in wrong.text
    assert 'role="alert"' in wrong.text and 'id="code-e"' in wrong.text
    second = web_client.cookies[PENDING_LOGIN_COOKIE_NAME]
    assert second != first

    empty = await post_form(web_client, "/login/totp", {"code": ""}, htmx=True)
    assert empty.status_code == 422
    assert "Enter the 6-digit code." in empty.text
    assert web_client.cookies[PENDING_LOGIN_COOKIE_NAME] not in (first, second)

    code = pyotp.TOTP(DEV_TOTP_SECRET).now()
    done = await post_form(web_client, "/login/totp", {"code": code}, htmx=True)
    assert (done.status_code, done.headers["hx-redirect"]) == (200, "/")
    assert_never_cached(done)
    assert PENDING_LOGIN_COOKIE_NAME not in web_client.cookies
    assert (await web_client.get("/projects")).status_code == 200


async def test_second_factor_plain_post_redirects(web_client: httpx.AsyncClient) -> None:
    """Without htmx the answers are whole pages and the success is a 303."""
    asked = await post_form(web_client, "/login", CREDENTIALS)
    assert asked.status_code == 200 and "<html" in asked.text
    code = pyotp.TOTP(DEV_TOTP_SECRET).now()
    done = await post_form(web_client, "/login/totp", {"code": code})
    assert (done.status_code, done.headers["location"]) == (303, "/")


async def test_pending_login_is_required_and_single_use(web_client: httpx.AsyncClient) -> None:
    """No cookie, a made-up cookie or a used token all send the person back to the password."""
    code = pyotp.TOTP(DEV_TOTP_SECRET).now()
    missing = await post_form(web_client, "/login/totp", {"code": code}, htmx=True)
    assert missing.status_code == 401
    assert "Verification expired" in missing.text and 'id="user"' in missing.text
    assert SESSION_COOKIE_NAME not in web_client.cookies

    web_client.cookies.set(PENDING_LOGIN_COOKIE_NAME, "made-up", path="/login")
    forged = await post_form(web_client, "/login/totp", {"code": code}, htmx=True)
    assert forged.status_code == 401 and "Verification expired" in forged.text
    web_client.cookies.delete(PENDING_LOGIN_COOKIE_NAME)

    await ask_for_code(web_client)
    used = web_client.cookies[PENDING_LOGIN_COOKIE_NAME]
    await post_form(web_client, "/login/totp", {"code": "000000"}, htmx=True)
    web_client.cookies.delete(PENDING_LOGIN_COOKIE_NAME)
    web_client.cookies.set(PENDING_LOGIN_COOKIE_NAME, used, path="/login")
    again = await post_form(web_client, "/login/totp", {"code": code}, htmx=True)
    assert again.status_code == 401 and "Verification expired" in again.text


async def test_second_factor_code_cannot_be_replayed(web_client: httpx.AsyncClient) -> None:
    """A code that signed in once is refused on the next sign in, with the generic message."""
    code = pyotp.TOTP(DEV_TOTP_SECRET).now()
    await ask_for_code(web_client)
    done = await post_form(web_client, "/login/totp", {"code": code}, htmx=True)
    assert done.status_code == 200
    await post_form(web_client, "/logout", {})
    await ask_for_code(web_client)
    replay = await post_form(web_client, "/login/totp", {"code": code}, htmx=True)
    assert replay.status_code == 401
    assert "The code is incorrect or expired." in replay.text


async def test_second_factor_failures_count_toward_lockout(web_client: httpx.AsyncClient) -> None:
    """Five wrong codes lock the name; the locked card replaces the code card."""
    await ask_for_code(web_client)
    for _ in range(5):
        response = await post_form(web_client, "/login/totp", {"code": "000000"}, htmx=True)
    assert response.status_code == 429
    assert "Account temporarily locked" in response.text
    assert PENDING_LOGIN_COOKIE_NAME not in web_client.cookies


async def test_page_request_without_a_session_redirects_to_login(
    web_client: httpx.AsyncClient,
) -> None:
    response = await web_client.get("/projects")
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


async def test_page_request_with_a_stale_session_shows_the_expired_state(
    web_client: httpx.AsyncClient,
) -> None:
    web_client.cookies.set(SESSION_COOKIE_NAME, "not-a-live-session")
    response = await web_client.get("/projects")
    assert (response.status_code, response.headers["location"]) == (303, "/login?expired=1")
    assert f'{SESSION_COOKIE_NAME}=""' in response.headers["set-cookie"]


async def test_htmx_request_without_a_session_gets_hx_redirect(
    web_client: httpx.AsyncClient,
) -> None:
    anonymous = await web_client.get("/projects", headers=HTMX)
    assert (anonymous.status_code, anonymous.headers["hx-redirect"]) == (401, "/login")
    assert_never_cached(anonymous)
    assert SESSION_COOKIE_NAME not in anonymous.headers.get("set-cookie", "")


async def test_htmx_request_with_a_stale_session_goes_to_the_expired_state(
    web_client: httpx.AsyncClient,
) -> None:
    """An open screen whose session ended is sent where a page request is: sign-in, expired."""
    web_client.cookies.set(SESSION_COOKIE_NAME, "not-a-live-session")
    stale = await web_client.get("/projects", headers=HTMX)
    assert (stale.status_code, stale.headers["hx-redirect"]) == (401, "/login?expired=1")
    assert_never_cached(stale)
    assert f'{SESSION_COOKIE_NAME}=""' in stale.headers["set-cookie"]


async def test_non_browser_clients_keep_the_json_401(web_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=web_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/projects")
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication required"}


async def test_there_is_no_standalone_session_expired_page(web_client: httpx.AsyncClient) -> None:
    assert (await web_client.get("/session-expired")).status_code == 404


@pytest.fixture
def trusted_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust the address the test client connects from; runs before the application starts."""
    monkeypatch.setenv("ENVMANAGER_TRUSTED_PROXIES", "127.0.0.1")


async def test_failed_sign_ins_count_under_the_forwarded_address_whatever_a_client_prepends(
    trusted_loopback: None, web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Behind a trusted proxy the counter and the audit entry use the address the proxy added,
    also when the client put junk in front of it.
    """
    await web_client.get("/healthz")
    response = await web_client.post(
        "/login",
        data={
            "identifier": "viewer@example.test",
            "password": "wrong password",
            "csrf_token": web_client.cookies[CSRF_COOKIE_NAME],
        },
        headers={"X-Forwarded-For": "unknown, _hidden, 203.0.113.9"},
    )
    assert response.status_code == 401
    async with web_app.state.session_factory() as db:
        counters = (await db.execute(select(LoginCounter))).scalars().all()
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "login.failure"))
        ).scalar_one()
    by_kind = {(row.kind, row.subject): row.failures for row in counters}
    assert by_kind[(LoginCounterKind.IP, "203.0.113.9")] == 1
    assert (LoginCounterKind.IP, "127.0.0.1") not in by_kind
    assert entry.ip_address == "203.0.113.9"
