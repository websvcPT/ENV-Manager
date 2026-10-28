"""The account pages and routes on the seeded application, for every group.

A password, a code or a pending secret must exist only where the person typed it or was shown
it: these tests look for them in every other answer, audit entry and log line.
"""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.crypto import CryptoService
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.sessions import SESSION_COOKIE_NAME, TOTP_SETUP_COOKIE_NAME
from app.models import AuditEntry, LoginCounter, User, UserSession
from app.models.types import utc_now
from app.seeds import DEV_PASSWORD, DEV_TOTP_SECRET
from app.services import totp
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, assert_never_cached, main_of, post

FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
NEW_PASSWORD = "a brand new passphrase"
SECRET = re.compile(r'id="totp-secret"[^>]*>([^<]+)<')
QR_SOURCE = re.compile(r'<img id="totp-qr" src="([^"]+)"')


def enable_fields(code: str, password: str = DEV_PASSWORD) -> dict[str, str]:
    """The fields of the form that turns two-factor authentication on."""
    return {"current_password": password, "code": code}


@asynccontextmanager
async def another_browser(web_app: FastAPI, name: str) -> AsyncIterator[httpx.AsyncClient]:
    """A second signed-in browser of the same user: a second session."""
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5)"},
    ) as http:
        await sign_in(http, name)
        yield http


async def stored_user(web_app: FastAPI, name: str) -> User:
    """The user as stored now."""
    async with web_app.state.session_factory() as db:
        user: User = (await db.execute(select(User).where(User.name == name))).scalar_one()
    return user


async def audit(web_app: FastAPI, prefix: str = "account.") -> list[AuditEntry]:
    """Audit entries whose action starts with the prefix, oldest first."""
    async with web_app.state.session_factory() as db:
        entries = (await db.execute(select(AuditEntry).order_by(AuditEntry.id))).scalars()
        return [entry for entry in entries if entry.action.startswith(prefix)]


async def session_ids(web_app: FastAPI, name: str) -> list[int]:
    """The ids of the stored sessions of a user."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(UserSession.id)
            .join(User, User.id == UserSession.user_id)
            .where(User.name == name)
            .order_by(UserSession.id)
        )
        return list(rows.scalars())


async def change(http: httpx.AsyncClient, **overrides: str) -> httpx.Response:
    """Post the password form with valid fields unless overridden."""
    fields = {
        "current_password": DEV_PASSWORD,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
        **overrides,
    }
    return await post(http, "/account/password", fields)


async def start_setup(http: httpx.AsyncClient) -> tuple[httpx.Response, str]:
    """Start a second factor setup and return the answer with the secret it shows."""
    response = await post(http, "/account/totp/setup")
    assert response.status_code == 200
    shown = SECRET.search(response.text)
    assert shown is not None
    return response, shown.group(1).replace(" ", "")


def next_code(secret: str) -> str:
    """The code of the next time step: inside the accepted window and newer than the current."""
    return pyotp.TOTP(secret).at(utc_now() + timedelta(seconds=30))


@pytest.mark.parametrize(
    ("name", "group"),
    [
        ("root", "superadmin"),
        ("admin", "admin"),
        ("viewer", "viewer"),
        ("env_scoped_viewer", "viewer"),
        ("deployment_admin", "admin"),
    ],
)
async def test_every_group_opens_the_account_page(
    web_client: httpx.AsyncClient, name: str, group: str
) -> None:
    await sign_in(web_client, name)
    response = await web_client.get("/account")
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert "<h1" in body and "Account</h1>" in body
    assert f"{name} · {name}@example.test" in body
    assert f'data-status="{group}"' not in body
    for fragment in ("account-password", "account-totp", "account-sessions", "account-theme"):
        assert f'data-fragment="{fragment}"' in body
    for marker in (
        'id="cp"',
        'id="np"',
        'id="cp2"',
        'autocomplete="current-password"',
        'autocomplete="new-password"',
        "At least 12 characters.",
        "Change password",
        "Set up two-factor",
        "Not enabled",
        "This session",
        "Signed in ",
        'name="theme"',
        'value="system"',
    ):
        assert marker in body
    assert 'id="totp-remove"' in response.text and 'id="session-revoke"' in response.text
    assert 'aria-current="page"' not in main_of(response.text)
    assert "Revoke" not in body and "Sign out other sessions" not in body


async def test_the_theme_panel_starts_on_match_system(web_client: httpx.AsyncClient) -> None:
    """With nothing stored in the browser the preference is Match system, as the handoff draws."""
    await sign_in(web_client, "admin")
    page = (await web_client.get("/account")).text
    options = re.findall(r'<input type="radio" name="theme" value="(\w+)"( checked)?', page)
    assert [(value, bool(checked)) for value, checked in options] == [
        ("light", False),
        ("dark", False),
        ("system", True),
    ]


async def test_the_account_link_of_the_menu_now_opens_the_page(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "viewer")
    projects = await web_client.get("/projects")
    assert 'href="/account"' in projects.text
    assert (await web_client.get("/account")).status_code == 200


async def test_anonymous_requests_never_reach_the_account(web_client: httpx.AsyncClient) -> None:
    for path in ("/account", "/account/totp", "/account/totp/qr.svg", "/account/sessions"):
        response = await web_client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    forged = await post(web_client, "/account/password", {"current_password": "x"})
    assert forged.status_code == 401


async def test_account_posts_need_the_csrf_token(web_client: httpx.AsyncClient) -> None:
    await sign_in(web_client, "viewer")
    for path in ("/account/password", "/account/totp/setup", "/account/sessions/revoke-others"):
        assert (await web_client.post(path, data={}, headers=HTMX)).status_code == 403
    assert CSRF_COOKIE_NAME in web_client.cookies


async def test_changing_the_password_signs_the_other_sessions_out_and_echoes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    async with another_browser(web_app, "admin") as other:
        assert (await other.get("/projects")).status_code == 200
        response = await change(web_client)
        assert response.status_code == 200
        assert_never_cached(response)
        assert 'data-fragment="account-password"' in response.text
        assert "Password changed. 1 other session was signed out." in response.text
        assert 'role="status"' in response.text
        assert DEV_PASSWORD not in response.text and NEW_PASSWORD not in response.text
        assert 'value=""' in response.text and 'value="a ' not in response.text
        assert "sessions-changed" in response.headers["hx-trigger"]
        assert (await other.get("/projects")).status_code == 303
    assert (await web_client.get("/projects")).status_code == 200
    assert len(await session_ids(web_app, "admin")) == 1
    entries = await audit(web_app)
    assert [entry.action for entry in entries] == ["account.password_change"]
    stored = repr(entries[0].details)
    assert NEW_PASSWORD not in stored and DEV_PASSWORD not in stored


async def test_the_new_password_signs_in_and_the_old_one_does_not(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "viewer")
    assert (await change(web_client)).status_code == 200
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as fresh:
        await fresh.get("/healthz")
        credentials = {
            "csrf_token": fresh.cookies[CSRF_COOKIE_NAME],
            "identifier": "viewer@example.test",
        }
        old = await fresh.post("/login", data={**credentials, "password": DEV_PASSWORD})
        new = await fresh.post("/login", data={**credentials, "password": NEW_PASSWORD})
    assert (old.status_code, new.status_code) == (401, 303)


@pytest.mark.parametrize(
    ("overrides", "message", "field_id"),
    [
        ({"current_password": "wrong password"}, "The current password is incorrect.", "cp"),
        ({"current_password": ""}, "Enter your current password.", "cp"),
        ({"new_password": "short", "confirm_password": "short"}, "at least 12 characters", "np"),
        (
            {"new_password": DEV_PASSWORD, "confirm_password": DEV_PASSWORD},
            "must differ from the current one",
            "np",
        ),
        ({"confirm_password": "another new passphrase"}, "The passwords do not match.", "cp2"),
    ],
)
async def test_a_refused_password_change_names_the_field_and_changes_nothing(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    overrides: dict[str, str],
    message: str,
    field_id: str,
) -> None:
    await sign_in(web_client, "admin")
    before = (await stored_user(web_app, "admin")).password_hash
    response = await change(web_client, **overrides)
    assert response.status_code == 422
    assert message in response.text
    assert f'aria-describedby="{field_id}-e"' in response.text
    assert 'role="alert"' in response.text
    for value in overrides.values():
        if len(value) > 6:
            assert value not in response.text
    assert (await stored_user(web_app, "admin")).password_hash == before
    assert "sessions-changed" not in response.headers.get("hx-trigger", "")


async def test_wrong_current_passwords_count_toward_the_lockout_and_then_lock_the_form(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    for _ in range(4):
        assert (await change(web_client, current_password="wrong password")).status_code == 422
    locked = await change(web_client, current_password="wrong password")
    assert locked.status_code == 429 and locked.headers["retry-after"] == "900"
    assert "Too many failed attempts. Try again in 15 minutes." in locked.text
    refused = await change(web_client)
    assert refused.status_code == 429
    assert (await stored_user(web_app, "admin")).password_hash
    async with web_app.state.session_factory() as db:
        rows = (await db.execute(select(LoginCounter))).scalars()
        counters = {row.subject: row.failures for row in rows}
    assert counters["admin@example.test"] == 5
    actions = [entry.action for entry in await audit(web_app, "")]
    assert "login.lockout" in actions
    results = [
        entry.result.value
        for entry in await audit(web_app)
        if entry.action == "account.password_change"
    ]
    assert results == ["failure"] * 5 + ["blocked"]


async def test_starting_a_setup_shows_a_qr_code_and_the_key_and_stores_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response, secret = await start_setup(web_client)
    assert_never_cached(response)
    assert len(secret) == 32 and secret.isalnum()
    assert "Scan the QR code with an authenticator app." in response.text
    assert "Enter your current password and the 6-digit code to confirm." in response.text
    assert 'autocomplete="one-time-code"' in response.text
    assert 'inputmode="numeric"' in response.text
    assert 'hx-ignore class="select-all font-mono"' in response.text
    assert "Enable two-factor" in response.text and "Cancel" in response.text
    source = QR_SOURCE.search(response.text)
    assert source is not None and source.group(1).startswith("/account/totp/qr.svg?v=")
    assert secret not in source.group(1)
    cookie = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(TOTP_SETUP_COOKIE_NAME)
    ).lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/account/totp" in cookie
    assert secret.lower() not in cookie
    stored = await stored_user(web_app, "admin")
    assert stored.totp_secret is None and not stored.totp_enabled
    assert secret not in (await web_client.get("/account")).text


async def test_the_qr_code_is_a_never_cached_svg_of_the_pending_secret(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response, secret = await start_setup(web_client)
    source = QR_SOURCE.search(response.text)
    assert source is not None
    image = await web_client.get(source.group(1))
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/svg+xml"
    assert image.headers["cache-control"] == "no-store"
    user = await stored_user(web_app, "admin")
    assert image.content == totp.qr_svg(totp.provisioning_uri(user, secret))
    assert b"<svg" in image.content and b"style=" not in image.content
    assert (await web_client.get("/account/totp/qr.svg?v=other")).status_code == 404
    assert (await web_client.get("/account/totp/qr.svg")).status_code == 404


async def test_the_qr_code_needs_this_sessions_own_setup(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response, _ = await start_setup(web_client)
    source = QR_SOURCE.search(response.text)
    assert source is not None
    async with another_browser(web_app, "admin") as other:
        assert (await other.get(source.group(1))).status_code == 404
        borrowed = (
            f"{SESSION_COOKIE_NAME}={other.cookies[SESSION_COOKIE_NAME]}; "
            f"{TOTP_SETUP_COOKIE_NAME}={web_client.cookies[TOTP_SETUP_COOKIE_NAME]}"
        )
        assert (await other.get(source.group(1), headers={"Cookie": borrowed})).status_code == 404
        refused = await post(other, "/account/totp/enable", enable_fields("123456"))
        assert refused.status_code == 409


async def test_a_wrong_code_keeps_the_setup_and_a_right_one_turns_it_on(
    web_client: httpx.AsyncClient, web_app: FastAPI, crypto: CryptoService
) -> None:
    await sign_in(web_client, "admin")
    _, secret = await start_setup(web_client)

    wrong = await post(web_client, "/account/totp/enable", enable_fields("000000"))
    assert wrong.status_code == 422
    assert "The code is incorrect or expired." in wrong.text
    assert 'aria-describedby="tc-e"' in wrong.text
    assert SECRET.search(wrong.text) is not None
    assert SECRET.search(wrong.text).group(1).replace(" ", "") == secret  # type: ignore[union-attr]
    empty = await post(web_client, "/account/totp/enable", enable_fields(""))
    assert empty.status_code == 422 and "Enter the 6-digit code." in empty.text
    assert (await stored_user(web_app, "admin")).totp_secret is None

    done = await post(web_client, "/account/totp/enable", enable_fields(pyotp.TOTP(secret).now()))
    assert done.status_code == 200
    assert_never_cached(done)
    assert "Enabled" in done.text and "Authenticator app configured on" in done.text
    assert "Remove two-factor" in done.text and secret not in done.text
    assert utc_now().strftime("%Y-%m-%d") in done.text
    assert any(
        value.startswith(f'{TOTP_SETUP_COOKIE_NAME}=""') or "max-age=0" in value.lower()
        for value in done.headers.get_list("set-cookie")
    )
    stored = await stored_user(web_app, "admin")
    assert stored.totp_enabled and stored.totp_enabled_at is not None
    assert stored.totp_secret is not None and secret not in stored.totp_secret
    assert totp.read_secret(stored, crypto) == secret
    assert (await web_client.get("/account/totp/qr.svg?v=any")).status_code == 404
    entries = await audit(web_app)
    assert [entry.action for entry in entries] == ["account.totp_enable"]
    assert secret not in repr(entries[0].details)
    assert secret not in (await web_client.get("/account")).text
    again = await post(web_client, "/account/totp/setup")
    assert again.status_code == 409 and "Enabled" in again.text


async def test_turning_it_on_needs_the_current_password(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A valid code is not enough: a wrong or missing password keeps the same setup running."""
    await sign_in(web_client, "admin")
    _, secret = await start_setup(web_client)
    code = pyotp.TOTP(secret).now()

    wrong = await post(web_client, "/account/totp/enable", enable_fields(code, "wrong password"))
    assert wrong.status_code == 422
    assert "The current password is incorrect." in wrong.text
    assert 'aria-describedby="tp-e"' in wrong.text
    assert "wrong password" not in wrong.text and code not in wrong.text
    assert SECRET.search(wrong.text) is not None
    assert SECRET.search(wrong.text).group(1).replace(" ", "") == secret  # type: ignore[union-attr]
    empty = await post(web_client, "/account/totp/enable", enable_fields(code, ""))
    assert empty.status_code == 422 and "Enter your current password." in empty.text
    assert 'aria-describedby="tp-e"' in empty.text
    stored = await stored_user(web_app, "admin")
    assert stored.totp_secret is None and not stored.totp_enabled and stored.totp_last_step is None

    done = await post(web_client, "/account/totp/enable", enable_fields(code))
    assert done.status_code == 200 and "Enabled" in done.text
    entries = await audit(web_app)
    assert [(entry.action, entry.result.value) for entry in entries] == [
        ("account.totp_enable", "failure"),
        ("account.totp_enable", "success"),
    ]
    assert entries[0].details == {"reason": "invalid_password"}
    stored_text = repr([entry.details for entry in entries])
    assert "wrong password" not in stored_text and code not in stored_text


async def test_the_setup_form_asks_for_the_password_and_the_code(
    web_client: httpx.AsyncClient,
) -> None:
    """The form has a password field before the code field and never carries values."""
    await sign_in(web_client, "admin")
    response, _ = await start_setup(web_client)
    page = response.text
    assert page.index('id="tp"') < page.index('id="tc"')
    assert 'name="current_password" type="password" value=""' in page
    assert 'autocomplete="current-password"' in page
    assert "Enter your current password and the 6-digit code to confirm." in page


async def test_wrong_passwords_at_the_setup_lock_it_and_keep_the_pending_secret(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Failures count like sign in; once locked even the right password is refused with 429."""
    await sign_in(web_client, "admin")
    _, secret = await start_setup(web_client)
    code = pyotp.TOTP(secret).now()
    answers = [
        await post(web_client, "/account/totp/enable", enable_fields(code, "wrong password"))
        for _ in range(5)
    ]
    assert [answer.status_code for answer in answers] == [422, 422, 422, 422, 429]
    locked = await post(web_client, "/account/totp/enable", enable_fields(code))
    assert locked.status_code == 429
    assert 0 < int(locked.headers["retry-after"]) <= 900
    assert 'role="alert"' in locked.text and "Too many failed attempts. Try again in" in locked.text
    assert SECRET.search(locked.text) is not None
    assert SECRET.search(locked.text).group(1).replace(" ", "") == secret  # type: ignore[union-attr]
    assert "wrong password" not in locked.text
    stored = await stored_user(web_app, "admin")
    assert stored.totp_secret is None and not stored.totp_enabled
    blocked = (await audit(web_app))[-1]
    assert blocked.result.value == "blocked" and blocked.details == {"reason": "locked"}


async def test_the_secret_never_reaches_the_log(
    web_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await sign_in(web_client, "admin")
    with caplog.at_level(logging.DEBUG):
        _, secret = await start_setup(web_client)
        await post(web_client, "/account/totp/enable", enable_fields(pyotp.TOTP(secret).now()))
        await web_client.get("/account")
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in logged and secret.lower() not in logged.lower()


async def test_a_setup_that_ran_out_is_dropped_with_a_note(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    await start_setup(web_client)
    web_client.cookies.delete(TOTP_SETUP_COOKIE_NAME, path="/account/totp")
    response = await post(web_client, "/account/totp/enable", enable_fields("123456"))
    assert response.status_code == 409
    assert "The setup took too long and was dropped. Start again." in response.text
    assert "Set up two-factor" in response.text
    assert (await stored_user(web_app, "admin")).totp_secret is None


async def test_cancelling_drops_the_pending_secret(web_client: httpx.AsyncClient) -> None:
    await sign_in(web_client, "admin")
    _, secret = await start_setup(web_client)
    cancelled = await web_client.get("/account/totp", headers=HTMX)
    assert cancelled.status_code == 200 and "Set up two-factor" in cancelled.text
    assert secret not in cancelled.text
    assert TOTP_SETUP_COOKIE_NAME not in web_client.cookies
    replay = await post(web_client, "/account/totp/enable", enable_fields(pyotp.TOTP(secret).now()))
    assert replay.status_code == 409


async def enable_with_code(http: httpx.AsyncClient) -> tuple[str, str]:
    """Turn the second factor on for the signed-in user; return the secret and the code used."""
    _, secret = await start_setup(http)
    code = pyotp.TOTP(secret).now()
    done = await post(http, "/account/totp/enable", enable_fields(code))
    assert done.status_code == 200
    return secret, code


async def enable_for(http: httpx.AsyncClient) -> str:
    """Turn the second factor on for the signed-in user and return the secret."""
    secret, _ = await enable_with_code(http)
    return secret


async def test_removing_needs_the_password_and_a_code_that_was_not_used_to_turn_it_on(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    secret, used = await enable_with_code(web_client)
    dialog = await web_client.get("/account/totp/remove", headers=HTMX)
    assert dialog.status_code == 200
    for marker in ('id="rp"', 'id="rc"', "Remove two-factor", 'hx-post="/account/totp/remove"'):
        assert marker in dialog.text

    bad_password = await post(
        web_client,
        "/account/totp/remove",
        {"current_password": "wrong password", "code": next_code(secret)},
    )
    assert bad_password.status_code == 422
    assert "The current password is incorrect." in bad_password.text
    assert 'aria-describedby="rp-e"' in bad_password.text
    assert "wrong password" not in bad_password.text

    reused = await post(
        web_client,
        "/account/totp/remove",
        {"current_password": DEV_PASSWORD, "code": used},
    )
    assert reused.status_code == 422 and 'aria-describedby="rc-e"' in reused.text

    missing = await post(
        web_client, "/account/totp/remove", {"current_password": DEV_PASSWORD, "code": ""}
    )
    assert missing.status_code == 422 and "Enter the 6-digit code." in missing.text
    assert (await stored_user(web_app, "admin")).totp_enabled

    done = await post(
        web_client,
        "/account/totp/remove",
        {"current_password": DEV_PASSWORD, "code": next_code(secret)},
    )
    assert done.status_code == 204
    assert "totp-changed" in done.headers["hx-trigger"]
    assert "close-dialogs" in done.headers["hx-trigger"]
    stored = await stored_user(web_app, "admin")
    assert (stored.totp_enabled, stored.totp_secret, stored.totp_enabled_at) == (False, None, None)
    panel = await web_client.get("/account/totp", headers=HTMX)
    assert "Not enabled" in panel.text and "Set up two-factor" in panel.text
    actions = [(entry.action, entry.result.value) for entry in await audit(web_app)]
    assert actions == [
        ("account.totp_enable", "success"),
        ("account.totp_disable", "failure"),
        ("account.totp_disable", "failure"),
        ("account.totp_disable", "success"),
    ]
    assert DEV_PASSWORD not in repr([entry.details for entry in await audit(web_app)])


async def test_removing_counts_failures_toward_the_lockout_and_locks_the_dialog(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    secret = await enable_for(web_client)
    fields = {"current_password": "wrong password", "code": next_code(secret)}
    for _ in range(4):
        assert (await post(web_client, "/account/totp/remove", fields)).status_code == 422
    locked = await post(web_client, "/account/totp/remove", fields)
    assert locked.status_code == 429 and "Try again in 15 minutes." in locked.text
    right = await post(
        web_client,
        "/account/totp/remove",
        {"current_password": DEV_PASSWORD, "code": next_code(secret)},
    )
    assert right.status_code == 429
    assert (await stored_user(web_app, "admin")).totp_enabled


async def test_removing_when_it_is_not_on_says_so(web_client: httpx.AsyncClient) -> None:
    await sign_in(web_client, "viewer")
    dialog = await web_client.get("/account/totp/remove", headers=HTMX)
    assert "Two-factor authentication is not on." in dialog.text and 'id="rp"' not in dialog.text
    forged = await post(
        web_client, "/account/totp/remove", {"current_password": DEV_PASSWORD, "code": "123456"}
    )
    assert forged.status_code == 409 and "not on" in forged.text


async def test_the_seeded_second_factor_user_sees_the_enabled_state(
    web_client: httpx.AsyncClient,
) -> None:
    await web_client.get("/healthz")
    form = {"csrf_token": web_client.cookies[CSRF_COOKIE_NAME]}
    credentials = {"identifier": "totp_user@example.test", "password": DEV_PASSWORD, **form}
    await web_client.post("/login", data=credentials, headers=HTMX)
    code = {"code": pyotp.TOTP(DEV_TOTP_SECRET).now(), **form}
    assert (await web_client.post("/login/totp", data=code, headers=HTMX)).status_code == 200
    response = await web_client.get("/account")
    assert response.status_code == 200
    assert "Enabled" in response.text and "Remove two-factor" in response.text
    assert "Authenticator app configured" in response.text
    assert 'hx-post="/account/totp/setup"' not in response.text


async def test_the_sessions_panel_marks_this_session_and_offers_revoke_for_the_others(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    web_client.headers["User-Agent"] = FIREFOX_LINUX
    await sign_in(web_client, "viewer")
    async with another_browser(web_app, "viewer"):
        page = main_of((await web_client.get("/account")).text)
        panel = page.split('data-fragment="account-sessions"', 1)[1].split("</section>", 1)[0]
        row = r'<tr data-fragment="session-row" id="session-(\d+)"([^>]*)>(.*?)</tr>'
        rows = re.findall(row, panel, re.DOTALL)
        assert len(rows) == 2
        (current_id, current_attrs, current), (other_id, _, other) = rows
        assert 'data-current="true"' in current_attrs
        assert "Firefox on Linux" in current and "This session" in current
        assert "hx-get" not in current
        assert "iPhone" in other and "Firefox" not in other
        assert f'hx-get="/account/sessions/{other_id}/revoke"' in other and "Revoke" in other
        assert "Signed in " in current and "<time" in current
        assert "Sign out other sessions" in panel
        assert f"/account/sessions/{current_id}/" not in panel
        columns = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', panel)
        assert columns == ["Device", "IP", "Last active", "Actions"]


async def test_revoking_another_session_ends_it_after_a_confirm_dialog_and_is_audited(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "viewer")
    async with another_browser(web_app, "viewer") as other:
        current, theirs = await session_ids(web_app, "viewer")
        dialog = await web_client.get(f"/account/sessions/{theirs}/revoke", headers=HTMX)
        assert dialog.status_code == 200
        assert f'hx-post="/account/sessions/{theirs}/revoke"' in dialog.text
        assert "Sign out iPhone" in dialog.text
        assert "Sign out session" in dialog.text
        done = await post(web_client, f"/account/sessions/{theirs}/revoke")
        assert done.status_code == 204
        assert "sessions-changed" in done.headers["hx-trigger"]
        assert "close-dialogs" in done.headers["hx-trigger"]
        assert (await other.get("/projects")).status_code == 303
        assert await session_ids(web_app, "viewer") == [current]
        assert (await post(web_client, f"/account/sessions/{theirs}/revoke")).status_code == 404
    (entry,) = await audit(web_app)
    assert entry.action == "account.session_revoke" and entry.actor_name == "viewer"
    assert set(entry.details) == {"device", "session_ip"}


async def test_the_current_session_and_other_peoples_sessions_answer_not_found(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "viewer")
    async with another_browser(web_app, "admin"):
        (current,) = await session_ids(web_app, "viewer")
        (foreign,) = await session_ids(web_app, "admin")
        for session_id in (current, foreign, 99999):
            dialog = await web_client.get(f"/account/sessions/{session_id}/revoke", headers=HTMX)
            assert dialog.status_code == 404
            forged = await post(web_client, f"/account/sessions/{session_id}/revoke")
            assert forged.status_code == 404
        assert await session_ids(web_app, "admin") == [foreign]
        assert await session_ids(web_app, "viewer") == [current]
    assert await audit(web_app) == []


async def test_signing_out_the_other_sessions_keeps_this_one(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    async with (
        another_browser(web_app, "admin") as first,
        another_browser(web_app, "admin") as second,
    ):
        dialog = await web_client.get("/account/sessions/revoke-others", headers=HTMX)
        assert "2 other sessions end at once. This session stays signed in." in dialog.text
        done = await post(web_client, "/account/sessions/revoke-others")
        assert done.status_code == 204 and "sessions-changed" in done.headers["hx-trigger"]
        assert (await first.get("/projects")).status_code == 303
        assert (await second.get("/projects")).status_code == 303
    assert (await web_client.get("/projects")).status_code == 200
    assert len(await session_ids(web_app, "admin")) == 1
    (entry,) = await audit(web_app)
    assert entry.action == "account.sessions_revoke_others"
    assert entry.details == {"sessions_ended": 2}
    alone = await web_client.get("/account/sessions/revoke-others", headers=HTMX)
    assert "There are no other sessions." in alone.text
    panel = await web_client.get("/account/sessions", headers=HTMX)
    assert "Sign out other sessions" not in panel.text and "This session" in panel.text


async def test_the_account_fragments_are_served_to_the_signed_in_user_only(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "env_scoped_viewer")
    for path in ("/account/totp", "/account/sessions", "/account/totp/remove"):
        response = await web_client.get(path, headers=HTMX)
        assert response.status_code == 200
        assert_never_cached(response)
        assert "<html" not in response.text


async def test_the_account_entries_show_on_the_audit_screen_for_their_actor_and_the_superadmin(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    async with another_browser(web_app, "admin"):
        (_, theirs) = await session_ids(web_app, "admin")
        await post(web_client, f"/account/sessions/{theirs}/revoke")
    await change(web_client, current_password="wrong password")
    await change(web_client)

    for path in ("/audit", "/audit/table", "/audit/export.csv"):
        own = await web_client.get(path, headers=HTMX if path != "/audit" else None)
        assert own.status_code == 200
        assert "account.password_change" in own.text and "account.session_revoke" in own.text
        assert DEV_PASSWORD not in own.text and NEW_PASSWORD not in own.text
    page = (await web_client.get("/audit")).text
    for text in ("sessions ended", "invalid_current_password", "session ip", "iPhone"):
        assert text in page

    async with another_browser(web_app, "root") as root:
        for path in ("/audit", "/audit/export.csv"):
            seen = await root.get(path)
            assert seen.status_code == 200
            assert "account.password_change" in seen.text
            assert "account.session_revoke" in seen.text
        assert "failure" in (await root.get("/audit/export.csv")).text.lower()

    async with another_browser(web_app, "viewer") as viewer:
        assert (await viewer.get("/audit")).status_code == 403


async def test_no_password_reaches_the_log_when_it_is_changed_or_refused(
    web_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await sign_in(web_client, "admin")
    with caplog.at_level(logging.DEBUG):
        await change(web_client, current_password="a wrong current password")
        await change(web_client, new_password="x", confirm_password="y")
        await change(web_client)
    logged = " ".join(record.getMessage() for record in caplog.records)
    for password in (DEV_PASSWORD, NEW_PASSWORD, "a wrong current password"):
        assert password not in logged
