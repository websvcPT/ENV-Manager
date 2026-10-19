"""Tests for the Notifications tab: the matrix, its defaults, the save and the refusals."""

import re

import httpx
import pytest
from fastapi import FastAPI

from app.services.app_settings import NotificationSettings
from app.tests.conftest import sign_in
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    browser_as,
    changes_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
)
from app.tests.web_helpers import assert_never_cached, post

KINDS = (
    "login_new_address",
    "lockout",
    "deploy_succeeded",
    "deploy_problem",
    "file_changed",
    "user_changed",
    "backup_failed",
    "git_push_failed",
    "key_rotated",
)
PROJECT_KINDS = ("deploy_succeeded", "deploy_problem", "file_changed")
DEFAULTS_ON = {
    "login_new_address",
    "lockout",
    "deploy_problem",
    "user_changed",
    "backup_failed",
    "git_push_failed",
    "key_rotated",
}


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


def form_of(on: set[str], **recipients: str) -> dict[str, str]:
    """The posted fields: a checked box sends its name and a recipients select always does."""
    data = dict.fromkeys(on, "on")
    for kind in PROJECT_KINDS:
        data[f"{kind}_to"] = recipients.get(kind, "project_admins")
    return data


def rows_of(page: str) -> dict[str, dict[str, object]]:
    """Each row of the matrix: its label, whether it is checked and the selected recipients."""
    found: dict[str, dict[str, object]] = {}
    for row in re.findall(r'<tr data-kind="([^"]+)">(.*?)</tr>', page, re.DOTALL):
        kind, markup = row
        select = re.search(r'<select name="[^"]+".*?</select>', markup, re.DOTALL)
        selected = (
            re.search(r'<option value="([^"]*)" selected', select.group(0)) if select else None
        )
        found[kind] = {
            "label": re.search(r"<th[^>]*>([^<]+)", markup).group(1),  # type: ignore[union-attr]
            "checked": bool(re.search(r"<input type=\"checkbox\"[^>]* checked", markup)),
            "recipients": selected.group(1) if selected else None,
            "has_select": select is not None,
        }
    return found


async def test_only_the_superadmin_may_read_or_change_the_matrix(web_app: FastAPI) -> None:
    """Every other group gets 403 on the tab and the save, and nothing is stored."""
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            assert (await http.get("/settings/notifications")).status_code == 403, user
            assert (
                await post(http, "/settings/notifications", form_of(set(KINDS)))
            ).status_code == 403
    assert await stored_rows(web_app) == {}


async def test_the_tab_shows_the_decided_defaults(root: httpx.AsyncClient) -> None:
    """Failures, blocks, lockouts, user changes and backups are on; successes and files are off."""
    response = await root.get("/settings/notifications")
    assert response.status_code == 200
    assert_never_cached(response)
    assert "<title>Settings · Notifications · ENV Manager</title>" in response.text
    rows = rows_of(response.text)
    assert list(rows) == list(KINDS)
    assert {kind for kind, row in rows.items() if row["checked"]} == DEFAULTS_ON
    assert {kind for kind, row in rows.items() if row["has_select"]} == set(PROJECT_KINDS)
    assert {kind: rows[kind]["recipients"] for kind in PROJECT_KINDS} == dict.fromkeys(
        PROJECT_KINDS, "project_admins"
    )
    assert rows["login_new_address"]["label"] == "Login"
    assert rows["deploy_problem"]["label"] == "Failed, blocked or halted deploy"
    assert response.text.count("Superadmins</span>") == 6
    assert rows["key_rotated"]["label"] == "Master key rotated"


async def test_the_recipients_offered_for_a_project_event_are_the_three_choices(
    root: httpx.AsyncClient,
) -> None:
    """Superadmins, project admins, or both."""
    page = (await root.get("/settings/notifications")).text
    first = re.search(r'<select name="deploy_succeeded_to".*?</select>', page, re.DOTALL)
    assert first is not None
    assert re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', first.group(0)) == [
        ("superadmins", "Superadmins"),
        ("project_admins", "Project admins"),
        ("both", "Superadmins and project admins"),
    ]


async def test_saving_stores_the_matrix_audits_the_changes_and_shows_it_again(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A switch turned on and a recipients choice changed are stored for the next event."""
    on = DEFAULTS_ON | {"deploy_succeeded"}
    response = await post(root, "/settings/notifications", form_of(on, deploy_succeeded="both"))
    assert response.status_code == 200
    assert toast_of(response.text) == (
        "Notification settings saved",
        "They apply to the next event.",
    )
    settings = (await stored(web_app)).notifications
    assert settings.deploy_succeeded is True and settings.deploy_succeeded_to == "both"
    shown = rows_of(response.text)
    assert (
        shown["deploy_succeeded"]["checked"] and shown["deploy_succeeded"]["recipients"] == "both"
    )
    (entry,) = await settings_entries(web_app)
    assert {change["setting"] for change in changes_of(entry)} == {
        "notifications.deploy_succeeded",
        "notifications.deploy_succeeded_to",
    }


async def test_a_box_that_is_not_sent_is_off(root: httpx.AsyncClient, web_app: FastAPI) -> None:
    """A browser sends nothing for a cleared box, so no box checked means every switch off."""
    response = await post(root, "/settings/notifications", form_of(set()))
    assert response.status_code == 200
    settings = (await stored(web_app)).notifications
    assert not any(getattr(settings, kind) for kind in KINDS)
    assert not any(row["checked"] for row in rows_of(response.text).values())


async def test_saving_the_defaults_again_says_nothing_changed(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The default matrix posted as it is stores nothing and audits nothing."""
    response = await post(root, "/settings/notifications", form_of(DEFAULTS_ON))
    assert toast_of(response.text) == ("Nothing to save", "The settings are unchanged.")
    assert await settings_entries(web_app) == [] and await stored_rows(web_app) == {}


async def test_a_recipients_value_that_is_not_a_choice_is_refused_and_nothing_is_stored(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The reason sits on the row (422), the other rows keep what was posted."""
    data = form_of(DEFAULTS_ON | {"file_changed"}, file_changed="everyone")
    response = await post(root, "/settings/notifications", data)
    assert response.status_code == 422
    assert "Choose one of the listed recipients." in response.text
    assert rows_of(response.text)["file_changed"]["checked"]
    assert await stored_rows(web_app) == {}


async def test_the_stored_matrix_is_what_the_tab_shows_next_time(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A reload reads the saved values."""
    await post(root, "/settings/notifications", form_of({"lockout"}, file_changed="superadmins"))
    page = (await root.get("/settings/notifications")).text
    rows = rows_of(page)
    assert {kind for kind, row in rows.items() if row["checked"]} == {"lockout"}
    assert rows["file_changed"]["recipients"] == "superadmins"
    assert (await stored(web_app)).notifications == NotificationSettings(
        login_new_address=False,
        lockout=True,
        deploy_problem=False,
        user_changed=False,
        backup_failed=False,
        git_push_failed=False,
        key_rotated=False,
        file_changed_to="superadmins",
    )
