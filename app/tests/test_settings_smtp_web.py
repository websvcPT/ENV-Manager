"""Tests for the SMTP tab: the form, the save, the password never shown and the test mail."""

import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AppSetting, AuditEntry
from app.services.app_settings import SMTP_PASSWORD_KEY, SmtpSettings, read_secret
from app.tests.conftest import sign_in
from app.tests.notify_helpers import (
    SMTP,
    SMTP_PASSWORD,
    RecordingTransport,
    permanent_failure,
)
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    browser_as,
    error_of,
    selected_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
    value_of,
)
from app.tests.web_helpers import assert_never_cached, main_of, post

SMTP_FORM = {
    "host": "smtp.example.com",
    "port": "587",
    "tls_mode": "starttls",
    "username": "mailer",
    "password": SMTP_PASSWORD,
    "from_address": "env-manager@example.com",
}
NOT_SET_UP = "SMTP is not configured: no notification emails are sent."


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


@pytest.fixture
def transport(web_app: FastAPI) -> RecordingTransport:
    """A recording transport in place of the application's, for the test mail."""
    recording = RecordingTransport()
    web_app.state.mail_transport = recording
    return recording


def banner_area(page: str) -> str:
    """The markup of the banner area in a response."""
    found = re.search(r'<div id="banner-area".*?</div>', page, re.DOTALL)
    assert found is not None
    return found.group(0)


async def test_only_the_superadmin_may_read_or_change_the_smtp_settings(web_app: FastAPI) -> None:
    """Every other group gets 403 on the tab, the save and the test mail, and nothing is stored."""
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            assert (await http.get("/settings/smtp")).status_code == 403, user
            assert (await post(http, "/settings/smtp", SMTP_FORM)).status_code == 403, user
            test = await post(http, "/settings/smtp/test", {"test_to": "a@example.com"})
            assert test.status_code == 403, user
    assert await stored_rows(web_app) == {}


async def test_the_tab_shows_empty_settings_and_the_banner_for_a_new_installation(
    root: httpx.AsyncClient,
) -> None:
    """The defaults: no server, port 587, STARTTLS, and the test mail offered to the user."""
    response = await root.get("/settings/smtp")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert "<title>Settings · SMTP · ENV Manager</title>" in page
    assert (value_of(page, "sh"), value_of(page, "sp"), value_of(page, "su")) == ("", "587", "")
    assert (value_of(page, "sf"), value_of(page, "st")) == ("", "root@example.test")
    assert selected_of(page, "tls") == "starttls"
    options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', page)
    assert options == [("none", "None"), ("starttls", "STARTTLS"), ("tls", "SSL/TLS")]
    assert NOT_SET_UP in banner_area(page)
    assert 'name="remove_password"' not in page
    assert 'hx-post="/settings/smtp/test"' in page and 'hx-include="#st"' in page


async def test_saving_stores_the_settings_audits_them_and_clears_the_banner(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The answer is the form with a toast, and the banner area comes again, now empty."""
    response = await post(root, "/settings/smtp", SMTP_FORM)
    assert response.status_code == 200
    assert_never_cached(response)
    assert toast_of(response.text) == ("SMTP settings saved", "They apply to the next mail.")
    assert (await stored(web_app)).smtp == SMTP
    assert 'hx-swap-oob="true"' in banner_area(response.text)
    assert NOT_SET_UP not in response.text
    changes = [entry.details for entry in await settings_entries(web_app)]
    assert any("smtp.host" in repr(details) for details in changes)


async def test_the_password_is_stored_encrypted_and_never_shown_or_audited(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Not in the answer, the page, the stored rows in the clear or any audit entry."""
    response = await post(root, "/settings/smtp", SMTP_FORM)
    page = (await root.get("/settings/smtp")).text
    for text in (response.text, page):
        assert SMTP_PASSWORD not in text
        assert value_of(text, "spw") == ""
        assert 'placeholder="••••••••"' in text and "Leave empty to keep it." in text
        assert 'name="remove_password"' in text
    async with web_app.state.session_factory() as db:
        rows = (await db.execute(select(AppSetting))).scalars().all()
        row = next(item for item in rows if item.key == SMTP_PASSWORD_KEY)
        assert SMTP_PASSWORD not in row.value
        assert not any(SMTP_PASSWORD in item.value for item in rows)
        entries = (await db.execute(select(AuditEntry))).scalars().all()
        assert not any(SMTP_PASSWORD in repr(entry.details) for entry in entries)


async def test_an_empty_password_field_keeps_the_stored_password(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Saving another field with the password left empty changes nothing about the secret."""
    await post(root, "/settings/smtp", SMTP_FORM)
    async with web_app.state.session_factory() as db:
        before = (await db.get(AppSetting, SMTP_PASSWORD_KEY)).value
    await post(root, "/settings/smtp", {**SMTP_FORM, "password": "", "port": "2525"})
    async with web_app.state.session_factory() as db:
        assert (await db.get(AppSetting, SMTP_PASSWORD_KEY)).value == before
    assert (await stored(web_app)).smtp.port == 2525


async def test_a_new_password_replaces_the_stored_one_and_remove_deletes_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Typing one replaces it; the checkbox removes it and the field then has no placeholder."""
    await post(root, "/settings/smtp", SMTP_FORM)
    await post(root, "/settings/smtp", {**SMTP_FORM, "password": "another-password"})
    async with web_app.state.session_factory() as db:
        stored_password = await read_secret(db, web_app.state.crypto, SMTP_PASSWORD_KEY)
        assert stored_password == "another-password"
    removed = await post(
        root, "/settings/smtp", {**SMTP_FORM, "password": "", "remove_password": "on"}
    )
    assert removed.status_code == 200 and 'name="remove_password"' not in removed.text
    assert 'placeholder=""' in removed.text
    async with web_app.state.session_factory() as db:
        assert await db.get(AppSetting, SMTP_PASSWORD_KEY) is None


async def test_a_refused_value_answers_with_the_reason_and_stores_nothing(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The messages sit next to their fields (422) and the typed password is not echoed."""
    form = {**SMTP_FORM, "host": "smtp://x", "port": "99999", "from_address": "nobody"}
    response = await post(root, "/settings/smtp", form)
    assert response.status_code == 422
    page = response.text
    assert error_of(page, "sh") == "Enter a host name or address, without a scheme, port or path."
    assert error_of(page, "sp") == "Enter a port from 1 to 65535."
    assert error_of(page, "sf") == "Enter an address such as env-manager@example.com."
    assert SMTP_PASSWORD not in page and value_of(page, "spw") == ""
    assert await stored_rows(web_app) == {}


async def test_saving_an_empty_server_switches_mail_off_and_the_banner_comes_back(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Emptying the server and the from address is how SMTP is unset."""
    await post(root, "/settings/smtp", SMTP_FORM)
    off = {**SMTP_FORM, "host": "", "from_address": "", "username": "", "password": ""}
    response = await post(root, "/settings/smtp", off)
    assert response.status_code == 200
    assert (await stored(web_app)).smtp == SmtpSettings(port=587)
    assert NOT_SET_UP in banner_area(response.text)


async def test_saving_the_same_values_says_nothing_changed(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A repeat is a notice and no new audit entry."""
    await post(root, "/settings/smtp", SMTP_FORM)
    count = len(await settings_entries(web_app))
    response = await post(root, "/settings/smtp", {**SMTP_FORM, "password": ""})
    assert toast_of(response.text) == ("Nothing to save", "The settings are unchanged.")
    assert len(await settings_entries(web_app)) == count


async def test_the_test_mail_uses_the_saved_settings_and_answers_with_the_time(
    root: httpx.AsyncClient, transport: RecordingTransport
) -> None:
    """Saved settings and the decrypted password go to the transport; the badge reads sent."""
    await post(root, "/settings/smtp", SMTP_FORM)
    response = await post(root, "/settings/smtp/test", {"test_to": "alice@example.com"})
    assert response.status_code == 200
    assert_never_cached(response)
    assert re.search(r'data-status="deployed">.*Test mail sent \d\d:\d\d', response.text, re.DOTALL)
    (sent,) = transport.sent
    assert (sent.smtp, sent.password, sent.mail.to) == (SMTP, SMTP_PASSWORD, "alice@example.com")
    assert SMTP_PASSWORD not in response.text


async def test_the_test_mail_answers_200_with_the_reason_when_it_cannot_be_sent(
    root: httpx.AsyncClient, transport: RecordingTransport
) -> None:
    """Not set up, a bad address and a refusing server each show a fixed reason."""
    response = await post(root, "/settings/smtp/test", {"test_to": "alice@example.com"})
    assert response.status_code == 200 and 'data-status="failed"' in response.text
    assert "SMTP is not configured. Save a server and a from address first." in response.text
    await post(root, "/settings/smtp", SMTP_FORM)
    bad = await post(root, "/settings/smtp/test", {"test_to": "nobody"})
    assert "Enter a valid address to send the test mail to." in bad.text
    transport.failures = [permanent_failure()]
    refused = await post(root, "/settings/smtp/test", {"test_to": "alice@example.com"})
    assert refused.status_code == 200 and "Test mail failed" in refused.text
    assert "The mail server refused the username or password." in refused.text
    assert SMTP_PASSWORD not in refused.text


async def test_every_superadmin_page_shows_the_banner_until_smtp_is_set_up(
    root: httpx.AsyncClient,
) -> None:
    """The banner is on pages other than this tab, with its link, and goes after the save."""
    page = (await root.get("/settings/security")).text
    assert NOT_SET_UP in page and 'href="/settings/smtp"' in banner_area(page)
    await post(root, "/settings/smtp", SMTP_FORM)
    assert NOT_SET_UP not in (await root.get("/settings/security")).text
    assert NOT_SET_UP not in main_of((await root.get("/settings/smtp")).text)
