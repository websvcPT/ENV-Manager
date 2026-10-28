"""Tests for the Single sign-on tab: the form, the save, the secret, the mapping rows, the
confirmation of turning sign in on and the discovery test."""

import html
import json
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AppSetting, AuditEntry
from app.services.app_settings import SSO_CLIENT_SECRET_KEY, SsoSettings, read_secret
from app.services.sso_refusal import ISSUER_MISMATCH, MESSAGES, PROVIDER_UNAVAILABLE
from app.tests.conftest import sign_in
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    browser_as,
    error_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
    value_of,
)
from app.tests.sso_helpers import BASE_URL, CLIENT_ID, CLIENT_SECRET, ISSUER, World
from app.tests.sso_web_helpers import sso_world
from app.tests.web_helpers import assert_never_cached, post

ROWS = {"mapping_role": ["env-admin", "env-viewer"], "mapping_group": ["admin", "viewer"]}
SSO_FORM = {
    "issuer": ISSUER,
    "client_id": CLIENT_ID,
    "create_users": "on",
    "link_by_email": "on",
    **ROWS,
}
COMPLETE = {**SSO_FORM, "client_secret": CLIENT_SECRET}
TYPED_SECRET = "typed-but-not-echoed"


@pytest.fixture(autouse=True)
def base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address the application is reached at, before the application starts."""
    monkeypatch.setenv("ENVMANAGER_BASE_URL", BASE_URL)


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


@pytest.fixture
def world(web_app: FastAPI) -> World:
    """The application's side of a sign in against a fresh fake provider."""
    return sso_world(web_app)


def rows_of(page: str) -> list[tuple[str, str]]:
    """The mapping rows in a response as (role, group) pairs, in order."""
    roles = [
        html.unescape(role) for role in re.findall(r'name="mapping_role" value="([^"]*)"', page)
    ]
    selects = re.findall(r'<select name="mapping_group".*?</select>', page, re.DOTALL)
    groups = []
    for markup in selects:
        found = re.search(r'<option value="([^"]*)" selected', markup)
        assert found is not None
        groups.append(found.group(1))
    assert len(roles) == len(groups)
    return list(zip(roles, groups, strict=True))


def turn_on_offered(page: str) -> bool:
    """Whether the form has the button that opens the confirmation."""
    return 'hx-post="/settings/sso/enable"' in page


async def save_and_turn_on(root: httpx.AsyncClient) -> httpx.Response:
    """Save the complete form with sign in on, with the confirmation."""
    return await post(root, "/settings/sso", {**COMPLETE, "enabled": "on", "confirm_enable": "1"})


async def changes_saved(web_app: FastAPI) -> list[dict[str, object]]:
    """Every change listed by the settings audit entries, oldest first."""
    return [
        change
        for entry in await settings_entries(web_app)
        for change in entry.details["changes"]  # type: ignore[attr-defined]
    ]


async def test_only_the_superadmin_may_read_or_change_the_settings(web_app: FastAPI) -> None:
    """Every other group gets 403 on the tab and on every action, and nothing is stored."""
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            assert (await http.get("/settings/sso")).status_code == 403, user
            for path in (
                "/settings/sso",
                "/settings/sso/mappings",
                "/settings/sso/enable",
                "/settings/sso/test",
            ):
                response = await post(http, path, {**COMPLETE, "enabled": "on"})
                assert response.status_code == 403, (user, path)
    assert await stored_rows(web_app) == {}
    assert await settings_entries(web_app) == []


async def test_an_anonymous_visitor_is_sent_to_sign_in(web_client: httpx.AsyncClient) -> None:
    response = await web_client.get("/settings/sso")
    assert (response.status_code, response.headers["location"]) == (303, "/login")


async def test_the_tab_shows_the_empty_settings_the_redirect_address_and_the_actions(
    root: httpx.AsyncClient,
) -> None:
    response = await root.get("/settings/sso")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert "<title>Settings · Single sign-on · ENV Manager</title>" in page
    assert [value_of(page, name) for name in ("sso-issuer", "sso-client-id")] == ["", ""]
    assert value_of(page, "sso-redirect-uri") == f"{BASE_URL}/login/sso/callback"
    assert 'data-js="copy" data-target="sso-redirect-uri"' in page
    assert 'placeholder="resource_access.&lt;client&gt;.roles"' in page
    assert 'name="remove_client_secret"' not in page
    assert 'name="enabled" checked' not in page
    assert 'name="create_users" checked' in page and 'name="link_by_email" checked' in page
    assert "No rows yet." in page and rows_of(page) == []
    assert turn_on_offered(page)
    assert 'hx-post="/settings/sso/test"' in page and 'hx-include="#sso-issuer"' in page
    assert 'id="sso-enable"' in page


async def test_saving_with_sign_in_off_stores_the_settings_and_audits_the_origin_only(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    response = await post(root, "/settings/sso", {**COMPLETE, "roles_claim": "groups"})
    assert response.status_code == 200
    assert_never_cached(response)
    assert toast_of(response.text) == (
        "Single sign-on settings saved",
        "They apply to the next sign in.",
    )
    rows = (("env-admin", "admin"), ("env-viewer", "viewer"))
    expected = SsoSettings(False, ISSUER, CLIENT_ID, "groups", "", True, True, rows)
    assert (await stored(web_app)).sso == expected
    changes = await changes_saved(web_app)
    issuer = next(change for change in changes if change["setting"] == "sso.issuer")
    assert issuer == {
        "setting": "sso.issuer",
        "kind": "url",
        "old": "",
        "new": "https://idp.example.test",
    }
    assert {"setting": SSO_CLIENT_SECRET_KEY, "kind": "secret"} in changes
    details = json.dumps([entry.details for entry in await settings_entries(web_app)])
    assert CLIENT_SECRET not in details


async def test_the_account_address_is_audited_as_its_origin_too(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    address = "https://sso.example.test/realms/main/account?token=abc"
    await post(root, "/settings/sso", {**SSO_FORM, "account_url": address})
    changes = await changes_saved(web_app)
    found = next(change for change in changes if change["setting"] == "sso.account_url")
    assert found["new"] == "https://sso.example.test" and found["kind"] == "url"
    assert "token=abc" not in json.dumps(changes)


async def test_saving_the_same_settings_says_there_is_nothing_to_save(
    root: httpx.AsyncClient,
) -> None:
    await post(root, "/settings/sso", COMPLETE)
    response = await post(root, "/settings/sso", SSO_FORM)
    assert toast_of(response.text) == ("Nothing to save", "The settings are unchanged.")


async def test_each_refused_field_shows_its_reason_and_nothing_is_stored(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    response = await post(
        root,
        "/settings/sso",
        {
            "issuer": "ftp://idp.example.test",
            "client_id": "x" * 300,
            "roles_claim": "a..b",
            "account_url": "not an address",
            "client_secret": TYPED_SECRET,
            "remove_client_secret": "on",
            "mapping_role": ["env-admin", ""],
            "mapping_group": ["admin", "viewer"],
        },
    )
    assert response.status_code == 422
    page = response.text
    fields = ("sso-issuer", "sso-client-id", "sso-roles-claim", "sso-account-url", "sso-mappings")
    for field in fields:
        assert error_of(page, field), field
    assert (error_of(page, "sso-mappings") or "").startswith("Row 2: enter the role")
    assert toast_of(page) is None
    assert TYPED_SECRET not in page
    assert await stored_rows(web_app) == {}


async def test_the_superadmin_group_is_not_a_choice_and_is_refused(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    added = await post(root, "/settings/sso/mappings", {"row_action": "add"})
    assert re.findall(r'<option value="([^"]*)"', added.text) == ["admin", "viewer"]
    assert "uperadmin" not in added.text
    response = await post(
        root,
        "/settings/sso",
        {**SSO_FORM, "mapping_role": ["env-super"], "mapping_group": ["superadmin"]},
    )
    assert response.status_code == 422
    assert error_of(response.text, "sso-mappings") == "Row 1: choose admin or viewer."
    assert await stored_rows(web_app) == {}


async def test_the_client_secret_is_stored_encrypted_and_never_shown_or_audited(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    response = await post(root, "/settings/sso", COMPLETE)
    page = (await root.get("/settings/sso")).text
    for text in (response.text, page):
        assert CLIENT_SECRET not in text
        assert value_of(text, "sso-secret") == ""
        assert 'placeholder="••••••••"' in text and "Leave empty to keep it." in text
        assert 'name="remove_client_secret"' in text
    async with web_app.state.session_factory() as db:
        rows = (await db.execute(select(AppSetting))).scalars().all()
        assert not any(CLIENT_SECRET in item.value for item in rows)
        entries = (await db.execute(select(AuditEntry))).scalars().all()
        assert not any(CLIENT_SECRET in repr(entry.details) for entry in entries)


async def test_an_empty_secret_field_keeps_the_secret_and_remove_deletes_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await post(root, "/settings/sso", COMPLETE)
    await post(root, "/settings/sso", {**SSO_FORM, "client_id": "other-client"})
    async with web_app.state.session_factory() as db:
        assert await read_secret(db, web_app.state.crypto, SSO_CLIENT_SECRET_KEY) == CLIENT_SECRET
    both = await post(
        root,
        "/settings/sso",
        {**SSO_FORM, "client_secret": "new", "remove_client_secret": "on"},
    )
    assert both.status_code == 422 and error_of(both.text, "sso-secret")
    removed = await post(root, "/settings/sso", {**SSO_FORM, "remove_client_secret": "on"})
    assert removed.status_code == 200
    assert SSO_CLIENT_SECRET_KEY not in (await stored(web_app)).secrets
    assert 'name="remove_client_secret"' not in removed.text
    assert 'placeholder=""' in removed.text


async def test_rows_are_added_and_removed_on_the_server_and_keep_their_order(
    root: httpx.AsyncClient,
) -> None:
    form = {**ROWS, "client_secret": TYPED_SECRET}
    added = await post(root, "/settings/sso/mappings", {**form, "row_action": "add"})
    assert added.status_code == 200
    assert_never_cached(added)
    assert rows_of(added.text) == [("env-admin", "admin"), ("env-viewer", "viewer"), ("", "viewer")]
    assert TYPED_SECRET not in added.text
    assert added.text.lstrip().startswith('<div id="sso-mappings" data-fragment="sso-mappings"')
    three = {"mapping_role": ["a", "b", "c"], "mapping_group": ["viewer", "admin", "viewer"]}
    removed = await post(root, "/settings/sso/mappings", {**three, "row_action": "remove:1"})
    assert rows_of(removed.text) == [("a", "viewer"), ("c", "viewer")]
    outside = await post(root, "/settings/sso/mappings", {**three, "row_action": "remove:9"})
    assert len(rows_of(outside.text)) == 3
    last = {"mapping_role": ["a"], "mapping_group": ["admin"], "row_action": "remove:0"}
    empty = await post(root, "/settings/sso/mappings", last)
    assert rows_of(empty.text) == [] and "No rows yet." in empty.text


async def test_a_row_moves_up_or_down_and_a_move_past_either_end_changes_nothing(
    root: httpx.AsyncClient,
) -> None:
    three = {"mapping_role": ["a", "b", "c"], "mapping_group": ["viewer", "admin", "viewer"]}
    expected = [("a", "viewer"), ("b", "admin"), ("c", "viewer")]
    down = await post(root, "/settings/sso/mappings", {**three, "row_action": "down:0"})
    assert rows_of(down.text) == [expected[1], expected[0], expected[2]]
    up = await post(root, "/settings/sso/mappings", {**three, "row_action": "up:2"})
    assert rows_of(up.text) == [expected[0], expected[2], expected[1]]
    for action in ("up:0", "down:2", "up:9", "down:-1", "sideways:1", ""):
        same = await post(root, "/settings/sso/mappings", {**three, "row_action": action})
        assert rows_of(same.text) == expected, action
    for position in ("up", "down", "remove"):
        assert f'data-row-action="{position}"' in up.text


async def test_the_row_limit_hides_the_add_button(root: httpx.AsyncClient) -> None:
    many = {"mapping_role": [f"role-{n}" for n in range(50)], "mapping_group": ["viewer"] * 50}
    full = await post(root, "/settings/sso/mappings", {**many, "row_action": "add"})
    assert len(rows_of(full.text)) == 50
    assert "Add mapping" not in full.text


async def test_saved_rows_come_back_in_the_order_they_were_saved(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    order = {
        "mapping_role": ["z-role", "a-role", "m-role"],
        "mapping_group": ["viewer", "admin", "viewer"],
    }
    response = await post(root, "/settings/sso", {**SSO_FORM, **order})
    expected = [("z-role", "viewer"), ("a-role", "admin"), ("m-role", "viewer")]
    assert rows_of(response.text) == expected
    assert (await stored(web_app)).sso.mappings == tuple(expected)
    assert rows_of((await root.get("/settings/sso")).text) == expected


async def test_turning_on_needs_the_confirmation_and_stores_nothing_without_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    response = await post(root, "/settings/sso", {**COMPLETE, "enabled": "on"})
    assert response.status_code == 422
    assert (
        error_of(response.text, "sso-enabled") == "Turning single sign-on on needs a confirmation."
    )
    assert await stored_rows(web_app) == {}


async def test_the_confirmation_says_what_changes_and_saves_with_the_form(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    dialog = await post(root, "/settings/sso/enable", {**COMPLETE, "enabled": "on"})
    assert dialog.status_code == 200
    assert_never_cached(dialog)
    text = html.unescape(dialog.text)
    assert "Admins and viewers can then sign in only with SSO" in text
    assert "Superadmins keep password sign in" in text
    assert 'hx-post="/settings/sso"' in dialog.text and 'hx-include="#sso"' in dialog.text
    assert '{"confirm_enable": "1"}' in text
    assert await stored_rows(web_app) == {}
    saved = await save_and_turn_on(root)
    assert saved.status_code == 200
    assert json.loads(saved.headers["hx-trigger"]) == {"close-dialogs": True}
    assert toast_of(saved.text) is not None
    assert (await stored(web_app)).sso_on
    assert not turn_on_offered(saved.text)
    assert re.search(r'name="enabled" checked', saved.text)


async def test_while_on_a_save_needs_no_confirmation_and_turning_off_never_does(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await save_and_turn_on(root)
    kept = await post(root, "/settings/sso", {**SSO_FORM, "enabled": "on", "roles_claim": "groups"})
    assert kept.status_code == 200
    off = await post(root, "/settings/sso", SSO_FORM)
    assert off.status_code == 200
    assert not (await stored(web_app)).sso_on
    assert turn_on_offered(off.text)


async def test_turning_on_an_incomplete_setup_is_refused_with_each_missing_thing(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    bare = {"enabled": "on", "create_users": "on", "link_by_email": "on"}
    response = await post(root, "/settings/sso", {**bare, "confirm_enable": "1"})
    assert response.status_code == 422
    page = response.text
    for field in ("sso-issuer", "sso-client-id", "sso-secret", "sso-mappings", "sso-enabled"):
        assert error_of(page, field), field
    assert error_of(page, "sso-secret") == "Enter the client secret."
    assert error_of(page, "sso-mappings") == "Add at least one mapping row."
    assert await stored_rows(web_app) == {}
    dialog = await post(root, "/settings/sso/enable", bare)
    assert dialog.status_code == 422
    assert dialog.headers["hx-retarget"] == "#sso" and dialog.headers["hx-reswap"] == "outerHTML"
    assert json.loads(dialog.headers["hx-trigger"]) == {"close-dialogs": True}
    assert error_of(dialog.text, "sso-secret") == "Enter the client secret."
    no_rows = {**COMPLETE, "enabled": "on", "confirm_enable": "1", "mapping_role": []}
    refused = await post(root, "/settings/sso", {**no_rows, "mapping_group": []})
    assert error_of(refused.text, "sso-mappings") == "Add at least one mapping row."


async def test_removing_the_secret_while_on_is_refused(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await save_and_turn_on(root)
    form = {**SSO_FORM, "enabled": "on", "remove_client_secret": "on"}
    response = await post(root, "/settings/sso", form)
    assert response.status_code == 422 and error_of(response.text, "sso-secret")
    assert (await stored(web_app)).sso_on


async def test_the_audit_entry_of_turning_on_shows_the_flag_and_never_the_secret(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await save_and_turn_on(root)
    assert {"setting": "sso.enabled", "old": False, "new": True} in await changes_saved(web_app)
    details = json.dumps([entry.details for entry in await settings_entries(web_app)])
    assert CLIENT_SECRET not in details


async def test_the_discovery_test_reports_the_signing_keys_and_saves_nothing(
    root: httpx.AsyncClient, web_app: FastAPI, world: World
) -> None:
    response = await post(root, "/settings/sso/test", {"issuer": ISSUER})
    assert response.status_code == 200
    assert_never_cached(response)
    assert 'data-status="deployed"' in response.text
    assert "Discovery worked: 1 signing key." in response.text and ISSUER in response.text
    assert world.idp.seen[0] == ("GET", f"{ISSUER}/.well-known/openid-configuration")
    assert await stored_rows(web_app) == {}
    assert await settings_entries(web_app) == []


async def test_the_discovery_test_uses_the_typed_issuer_else_the_stored_one(
    root: httpx.AsyncClient, world: World
) -> None:
    await post(root, "/settings/sso", COMPLETE)
    world.idp.seen.clear()
    stored_issuer = await post(root, "/settings/sso/test", {"issuer": ""})
    assert 'data-status="deployed"' in stored_issuer.text
    assert world.idp.seen[0][1].startswith(ISSUER)
    other = {"issuer": "https://elsewhere.example.test/realms/x"}
    typed = await post(root, "/settings/sso/test", other)
    assert 'data-status="failed"' in typed.text
    assert MESSAGES[PROVIDER_UNAVAILABLE] in typed.text


async def test_the_discovery_test_says_why_it_failed(root: httpx.AsyncClient, world: World) -> None:
    empty = await post(root, "/settings/sso/test", {"issuer": ""})
    assert "Enter the issuer address first." in empty.text
    bad = await post(root, "/settings/sso/test", {"issuer": "ftp://idp.example.test"})
    assert "Enter an http or https address that names a host." in bad.text
    assert world.idp.seen == []
    world.idp.down = True
    down = await post(root, "/settings/sso/test", {"issuer": ISSUER})
    assert 'data-status="failed"' in down.text and "Discovery failed" in down.text
    assert MESSAGES[PROVIDER_UNAVAILABLE] in html.unescape(down.text)
    world.idp.down = False
    world.idp.discovery_issuer = "https://other.example.test"
    mismatch = await post(root, "/settings/sso/test", {"issuer": ISSUER})
    assert MESSAGES[ISSUER_MISMATCH] in html.unescape(mismatch.text)
    world.idp.discovery_issuer = None
    world.idp.keys = []
    no_keys = await post(root, "/settings/sso/test", {"issuer": ISSUER})
    assert 'data-status="failed"' in no_keys.text
