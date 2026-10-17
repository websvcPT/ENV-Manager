"""Sign-in screens, session expiry redirects and the second factor step, on the seeded app."""

import re

import httpx
import pyotp
from fastapi import FastAPI

from app.core.csrf import CSRF_COOKIE_NAME
from app.core.sessions import SESSION_COOKIE_NAME
from app.seeds import DEV_PASSWORD, DEV_TOTP_SECRET
from app.tests.conftest import sign_in

HTMX = {"HX-Request": "true"}


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


async def test_invalid_credentials_keep_the_name_and_drop_the_password(
    web_client: httpx.AsyncClient,
) -> None:
    """A plain post answers the whole page, with one generic message for every failure."""
    wrong = await post_form(web_client, "/login", {"identifier": "root", "password": "nope"})
    unknown = await post_form(web_client, "/login", {"identifier": "nobody", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert "Invalid credentials" in wrong.text and "Invalid credentials" in unknown.text
    assert 'role="alert" data-fragment="login-message"' in wrong.text
    assert 'value="root"' in wrong.text
    assert "nope" not in wrong.text
    assert wrong.text.count("<html") == 1


async def test_htmx_failure_answers_only_the_card(web_client: httpx.AsyncClient) -> None:
    response = await post_form(
        web_client, "/login", {"identifier": "root", "password": "nope"}, htmx=True
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
            web_client, "/login", {"identifier": "viewer", "password": "nope"}, htmx=True
        )
    assert response.status_code == 429
    assert 0 < int(response.headers["retry-after"]) <= 900
    assert "Account temporarily locked" in response.text
    assert "Try again in 15:00." in response.text
    assert len(re.findall(r"\sdisabled[\s>]", response.text)) == 3
    refused = await post_form(
        web_client, "/login", {"identifier": "viewer", "password": DEV_PASSWORD}, htmx=True
    )
    assert refused.status_code == 429


async def test_successful_login_redirects(web_client: httpx.AsyncClient) -> None:
    """Plain posts get a 303, htmx gets HX-Redirect; both set a hardened session cookie."""
    form = {"identifier": "root", "password": DEV_PASSWORD}
    plain = await post_form(web_client, "/login", form)
    assert (plain.status_code, plain.headers["location"]) == (303, "/")
    web_client.cookies.delete(SESSION_COOKIE_NAME)
    fragment = await post_form(web_client, "/login", form, htmx=True)
    assert (fragment.status_code, fragment.headers["hx-redirect"]) == (200, "/")
    assert "httponly" in fragment.headers["set-cookie"].lower()
    assert (await web_client.get("/projects")).status_code == 200


async def test_second_factor_step(web_client: httpx.AsyncClient) -> None:
    """The password asks for a code; a wrong code is a field error; the right code signs in."""
    credentials = {"identifier": "totp_user", "password": DEV_PASSWORD}
    asked = await post_form(web_client, "/login", credentials, htmx=True)
    assert asked.status_code == 200
    assert 'action="/login/totp"' in asked.text
    assert 'name="identifier" value="totp_user"' in asked.text
    assert SESSION_COOKIE_NAME not in web_client.cookies

    wrong = await post_form(web_client, "/login/totp", {**credentials, "code": "000000"}, htmx=True)
    assert wrong.status_code == 401
    assert "The code is incorrect or expired." in wrong.text
    assert 'role="alert"' in wrong.text and 'id="code-e"' in wrong.text

    empty = await post_form(web_client, "/login/totp", {**credentials, "code": ""}, htmx=True)
    assert empty.status_code == 422
    assert "Enter the 6-digit code." in empty.text

    code = pyotp.TOTP(DEV_TOTP_SECRET).now()
    done = await post_form(web_client, "/login/totp", {**credentials, "code": code}, htmx=True)
    assert (done.status_code, done.headers["hx-redirect"]) == (200, "/")


async def test_second_factor_failures_count_toward_lockout(web_client: httpx.AsyncClient) -> None:
    credentials = {"identifier": "totp_user", "password": DEV_PASSWORD}
    for _ in range(5):
        response = await post_form(
            web_client, "/login/totp", {**credentials, "code": "000000"}, htmx=True
        )
    assert response.status_code == 429
    assert "Account temporarily locked" in response.text


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
    web_client.cookies.set(SESSION_COOKIE_NAME, "not-a-live-session")
    stale = await web_client.get("/projects", headers=HTMX)
    assert (stale.status_code, stale.headers["hx-redirect"]) == (401, "/session-expired")


async def test_non_browser_clients_keep_the_json_401(web_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=web_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/projects")
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication required"}


async def test_session_expired_page_is_public(web_client: httpx.AsyncClient) -> None:
    response = await web_client.get("/session-expired")
    assert response.status_code == 200
    assert 'data-fragment="session-expired"' in response.text
    assert 'href="/login"' in response.text
    assert 'data-js="theme-toggle"' not in response.text
