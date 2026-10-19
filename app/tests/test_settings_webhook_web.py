"""Tests for the webhook part of the Notifications tab: the fields, the secret that is never shown
back, the refusals, the audit trail, the banner and the test event."""

import json
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, AuditResult
from app.models.types import utc_now
from app.services.app_settings import WEBHOOK_SECRET_KEY, load_stored_settings, read_secret
from app.services.notify_status import EMAIL_CHANNEL, WEBHOOK_CHANNEL, load_failures, record_failure
from app.tests.conftest import sign_in
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    browser_as,
    changes_of,
    error_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
    value_of,
)
from app.tests.test_settings_notifications_web import DEFAULTS_ON, form_of
from app.tests.web_helpers import assert_never_cached, post
from app.tests.webhook_helpers import (
    WEBHOOK_SECRET,
    WEBHOOK_URL,
    RecordingWebhook,
    configure_webhook,
    permanent_failure,
    verifies,
)

TAB = "/settings/notifications"
TEST = "/settings/notifications/webhook/test"
OTHER_SECRET = "another-secret-sentinel-98765"


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


def hook_form(
    *,
    url: str = WEBHOOK_URL,
    secret: str = WEBHOOK_SECRET,
    enabled: bool = True,
    remove: bool = False,
) -> dict[str, str]:
    """The default matrix with the webhook fields."""
    data = form_of(DEFAULTS_ON)
    data["webhook_url"] = url
    data["webhook_secret"] = secret
    if enabled:
        data["webhook_enabled"] = "on"
    if remove:
        data["remove_webhook_secret"] = "on"
    return data


def banner_area(page: str) -> str:
    """The markup of the banner area in a response: up to the page body, or to the end of an
    answer that is only a fragment."""
    start = page.index('<div id="banner-area"')
    end = page.find("<main", start)
    return page[start : end if end != -1 else None]


def secret_input(page: str) -> str:
    """The markup of the secret input."""
    found = re.search(r'<input id="ws"[^>]*>', page)
    assert found is not None
    return found.group(0)


async def stored_secret(web_app: FastAPI) -> str | None:
    """The decrypted webhook secret, None when none is stored."""
    async with web_app.state.session_factory() as db:
        return await read_secret(db, web_app.state.crypto, WEBHOOK_SECRET_KEY)


async def audit_text(web_app: FastAPI) -> str:
    """Everything every audit entry holds, as one text."""
    async with web_app.state.session_factory() as db:
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    return json.dumps([[entry.details, entry.target_name, entry.actor_name] for entry in entries])


async def test_the_tab_shows_the_webhook_fields_empty_and_off_by_default(
    root: httpx.AsyncClient,
) -> None:
    """The address, the secret, the switch and the test button, with nothing filled in."""
    response = await root.get(TAB)
    page = response.text
    assert_never_cached(response)
    assert "Outbound webhook URL" in page and "Webhook shared secret" in page
    assert value_of(page, "wu") == ""
    assert 'type="password"' in secret_input(page) and 'value=""' in secret_input(page)
    assert 'placeholder=""' in secret_input(page)
    assert re.search(r'<input id="we" type="checkbox" name="webhook_enabled" class', page)
    assert 'hx-post="/settings/notifications/webhook/test"' in page
    assert "Send test event" in page and "Remove the stored shared secret" not in page


async def test_saving_stores_the_address_and_an_encrypted_secret_and_never_shows_it_back(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The secret is sealed in the settings store, read back only by the code that signs."""
    response = await post(root, TAB, hook_form())
    assert response.status_code == 200
    assert toast_of(response.text) == (
        "Notification settings saved",
        "They apply to the next event.",
    )
    settings = (await stored(web_app)).notifications
    assert settings.webhook_enabled is True and settings.webhook_url == WEBHOOK_URL
    assert await stored_secret(web_app) == WEBHOOK_SECRET
    rows = await stored_rows(web_app)
    assert WEBHOOK_SECRET not in json.dumps(rows) and WEBHOOK_SECRET_KEY in rows
    page = response.text
    assert WEBHOOK_SECRET not in page and 'value="' + WEBHOOK_SECRET not in page
    assert value_of(page, "wu") == WEBHOOK_URL
    assert 'value=""' in secret_input(page) and 'placeholder="••••••••"' in secret_input(page)
    assert "Remove the stored shared secret" in page and "Leave empty to keep it." in page
    assert 'hx-swap-oob="true"' in banner_area(page)
    again = (await root.get(TAB)).text
    assert WEBHOOK_SECRET not in again and 'value=""' in secret_input(again)
    assert 'placeholder="••••••••"' in secret_input(again)


async def test_the_audit_records_the_host_and_that_a_secret_changed_never_the_path_or_secret(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The address is kept as scheme and host, the secret only by its name."""
    await post(root, TAB, hook_form())
    entries = await settings_entries(web_app)
    changes = [change for entry in entries for change in changes_of(entry)]
    by_name = {str(change["setting"]): change for change in changes}
    assert by_name["notifications.webhook_url"] == {
        "setting": "notifications.webhook_url",
        "kind": "url",
        "old": "",
        "new": "https://hooks.example.com",
    }
    assert by_name["notifications.webhook_enabled"]["new"] is True
    assert by_name[WEBHOOK_SECRET_KEY] == {"setting": WEBHOOK_SECRET_KEY, "kind": "secret"}
    text = await audit_text(web_app)
    assert WEBHOOK_SECRET not in text and "path-token" not in text and "T0001" not in text


async def test_an_empty_secret_field_keeps_the_stored_secret_and_a_typed_one_replaces_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Keep, then replace."""
    await post(root, TAB, hook_form())
    moved = hook_form(url="https://hooks.example.com/other", secret="")
    assert toast_of((await post(root, TAB, moved)).text) is not None
    assert await stored_secret(web_app) == WEBHOOK_SECRET
    assert (await stored(web_app)).notifications.webhook_url.endswith("/other")
    await post(root, TAB, hook_form(secret=OTHER_SECRET))
    assert await stored_secret(web_app) == OTHER_SECRET
    assert WEBHOOK_SECRET not in json.dumps(await stored_rows(web_app))
    assert OTHER_SECRET not in await audit_text(web_app)


async def test_the_secret_can_be_removed_and_then_the_webhook_cannot_be_on(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Removing needs the switch off; it is audited by name only."""
    await post(root, TAB, hook_form())
    refused = await post(root, TAB, hook_form(secret="", remove=True))
    assert refused.status_code == 422
    assert error_of(refused.text, "ws") == "Enter a shared secret to switch the webhook on."
    assert await stored_secret(web_app) == WEBHOOK_SECRET
    removed = await post(root, TAB, hook_form(secret="", remove=True, enabled=False))
    assert removed.status_code == 200 and await stored_secret(web_app) is None
    assert "Remove the stored shared secret" not in removed.text
    changes = [change for entry in await settings_entries(web_app) for change in changes_of(entry)]
    assert {"setting": WEBHOOK_SECRET_KEY, "kind": "secret", "removed": True} in changes
    nothing = await post(root, TAB, hook_form(secret="", enabled=True))
    assert nothing.status_code == 422
    assert (await stored(web_app)).notifications.webhook_enabled is False


@pytest.mark.parametrize(
    ("fields", "field_id", "message"),
    [
        ({"url": "ftp://hooks.example.com/x"}, "wu", "Only http and https addresses are accepted."),
        ({"url": "file:///etc/passwd"}, "wu", "Only http and https addresses are accepted."),
        (
            {"url": "https://user:pass@hooks.example.com/x"},
            "wu",
            "Do not put a user name or password in the address. Use the shared secret.",
        ),
        ({"url": "https://"}, "wu", "Enter an address that names a host."),
        ({"url": ""}, "wu", "Enter the address to switch the webhook on."),
        ({"secret": "short"}, "ws", "Use at least 16 characters."),
    ],
)
async def test_a_refused_field_answers_in_place_and_stores_nothing(
    root: httpx.AsyncClient,
    web_app: FastAPI,
    fields: dict[str, str],
    field_id: str,
    message: str,
) -> None:
    """The reason sits under the field (422), the typed secret is not put back."""
    url = fields.get("url", WEBHOOK_URL)
    data = hook_form(url=url, secret=fields.get("secret", WEBHOOK_SECRET))
    response = await post(root, TAB, data)
    assert response.status_code == 422
    assert error_of(response.text, field_id) == message
    assert data["webhook_secret"] not in response.text
    assert await stored_rows(web_app) == {}
    assert await settings_entries(web_app) == []


async def test_a_secret_and_a_removal_together_are_refused(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """One or the other."""
    await post(root, TAB, hook_form())
    response = await post(root, TAB, hook_form(secret=OTHER_SECRET, remove=True))
    assert response.status_code == 422
    assert error_of(response.text, "ws") == "Type a new secret or remove the stored one, not both."
    assert OTHER_SECRET not in response.text
    assert await stored_secret(web_app) == WEBHOOK_SECRET


async def test_an_address_may_be_kept_while_the_webhook_is_off(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The switch is separate from the address and the secret."""
    response = await post(root, TAB, hook_form(enabled=False))
    assert response.status_code == 200
    settings = (await stored(web_app)).notifications
    assert settings.webhook_enabled is False and settings.webhook_url == WEBHOOK_URL
    assert await stored_secret(web_app) == WEBHOOK_SECRET


async def test_only_the_superadmin_reaches_the_webhook_routes(web_app: FastAPI) -> None:
    """Every other group gets 403 on the save and the test, and nothing is stored or posted."""
    web_app.state.webhook_transport = recorder = RecordingWebhook()
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            assert (await post(http, TAB, hook_form())).status_code == 403, user
            assert (await post(http, TEST)).status_code == 403, user
    assert await stored_rows(web_app) == {} and recorder.sent == []


async def test_a_save_that_changes_the_webhook_starts_the_failure_count_again(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A failure under the old address says nothing about the new one; mail is left alone."""
    async with web_app.state.session_factory() as db:
        await record_failure(db, WEBHOOK_CHANNEL, "old", utc_now(), 3)
        await record_failure(db, EMAIL_CHANNEL, "mail", utc_now(), 2)
        await db.commit()
    await post(root, TAB, form_of(DEFAULTS_ON | {"file_changed"}))
    async with web_app.state.session_factory() as db:
        assert {row.channel for row in await load_failures(db)} == {WEBHOOK_CHANNEL, EMAIL_CHANNEL}
    response = await post(root, TAB, hook_form())
    assert "deploy webhook is failing" not in banner_area(response.text)
    async with web_app.state.session_factory() as db:
        assert [row.channel for row in await load_failures(db)] == [EMAIL_CHANNEL]


async def test_a_failing_webhook_shows_a_banner_that_links_to_the_tab(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Every superadmin page carries it, and other groups never see it."""
    async with web_app.state.session_factory() as db:
        reason = "The webhook did not answer in time."
        await record_failure(db, WEBHOOK_CHANNEL, reason, utc_now(), 2)
        await db.commit()
    page = (await root.get("/projects")).text
    assert "The deploy webhook is failing: 2 events could not be delivered" in page
    assert "The webhook did not answer in time." in page
    assert 'href="/settings/notifications"' in banner_area(page)
    async with browser_as(web_app, "admin") as admin:
        assert "deploy webhook" not in (await admin.get("/projects")).text


async def test_the_test_event_is_posted_through_the_saved_settings_and_the_result_shown(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A delivered event shows the success badge with the status, is signed and is audited."""
    web_app.state.webhook_transport = recorder = RecordingWebhook()
    async with web_app.state.session_factory() as db:
        await record_failure(db, WEBHOOK_CHANNEL, "old", utc_now(), 2)
        await db.commit()
    await configure_webhook(web_app.state.engine, web_app.state.crypto, enabled=False)
    response = await post(root, TEST)
    assert response.status_code == 200
    assert "Test event delivered" in response.text and "(HTTP 200)" in response.text
    assert 'hx-swap-oob="true"' in banner_area(response.text)
    assert "deploy webhook is failing" not in response.text
    (request,) = recorder.sent
    assert verifies(WEBHOOK_SECRET, request) and json.loads(request.body)["event"] == "webhook.test"
    async with web_app.state.session_factory() as db:
        (entry,) = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "webhook.test"))
        ).scalars()
        assert await load_failures(db) == []
    assert entry.result is AuditResult.SUCCESS and entry.actor_name == "root"
    assert WEBHOOK_SECRET not in await audit_text(web_app)


async def test_a_failed_test_event_shows_the_badge_and_the_reason(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Fixed words, no address, and nothing counted for the banner."""
    web_app.state.webhook_transport = RecordingWebhook(outcomes=[permanent_failure()])
    await configure_webhook(web_app.state.engine, web_app.state.crypto)
    response = await post(root, TEST)
    assert response.status_code == 200
    assert "Test event failed" in response.text
    assert "The webhook refused the request (HTTP 404)." in response.text
    assert "hooks.example.com" not in response.text and "path-token" not in response.text
    async with web_app.state.session_factory() as db:
        assert await load_failures(db) == []


async def test_the_test_event_before_the_webhook_is_set_up_says_to_save_first(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Nothing is posted without an address and a secret."""
    web_app.state.webhook_transport = recorder = RecordingWebhook()
    response = await post(root, TEST)
    assert "Save a webhook address and a shared secret first." in response.text
    assert recorder.sent == []
    async with web_app.state.session_factory() as db:
        assert not (await load_stored_settings(db)).notifications.webhook_url
