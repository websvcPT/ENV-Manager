"""The Allowed paths tab: the table, the add dialog with its live checks, remove and the audit."""

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.models import AllowedPath, AuditEntry, Deployment
from app.services.settings_events import SettingsChanged
from app.tests.conftest import sign_in
from app.tests.settings_helpers import error_of, settings_entries, value_of
from app.tests.web_helpers import HTMX, assert_never_cached, main_of, post

CHANGED_AND_CLOSED = {"paths-changed": True, "close-dialogs": True}
ROW = re.compile(r'<tr data-fragment="path-row" id="path-(\d+)">(.*?)</tr>', re.DOTALL)
PATH = re.compile(r'<span class="font-mono text-\[13px\]">([^<]+)</span>')
BADGE = re.compile(r'data-status="(\w+)"[^>]*>(?:<svg.*?</svg>)?([^<]+)</span>')


class Notifier:
    """Remembers the settings events the application announces."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[SettingsChanged] = []

    async def settings_changed(self, event: SettingsChanged) -> None:
        """Record the event."""
        self.events.append(event)


@pytest.fixture
def host(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real folder outside the data volume that counts as the only host mount."""
    root = tmp_path_factory.mktemp("host").resolve()
    for name in ("alpha", "beta"):
        (root / name).mkdir()
    monkeypatch.setattr("app.services.paths.read_mount_points", lambda *args: [root])
    return root


@pytest.fixture
def notifier(web_app: FastAPI) -> Notifier:
    """A recording notifier on the application."""
    recorder = Notifier()
    web_app.state.settings_notifier = recorder
    return recorder


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


def rows_of(page: str) -> dict[str, tuple[int, list[tuple[str, str]], str]]:
    """Path id, badges and the row markup by path, in the table of a page or fragment."""
    found: dict[str, tuple[int, list[tuple[str, str]], str]] = {}
    for path_id, markup in ROW.findall(page):
        path = PATH.search(markup)
        assert path is not None
        found[path.group(1)] = (int(path_id), BADGE.findall(markup), markup)
    return found


async def entry_id(web_app: FastAPI, path: str) -> int:
    """The id of the stored entry with this path."""
    async with web_app.state.session_factory() as db:
        found = (await db.execute(select(AllowedPath.id).where(AllowedPath.path == path))).scalar()
    assert found is not None
    return int(found)


async def test_the_table_lists_the_seeded_entries_with_status(root: httpx.AsyncClient) -> None:
    """The seeded folders do not exist in the container, so they read as missing, with counts."""
    response = await root.get("/settings/paths")
    assert response.status_code == 200
    assert_never_cached(response)
    rows = rows_of(response.text)
    assert {"/opt/rentals", "/opt/billing"} <= set(rows)
    _id, badges, markup = rows["/opt/rentals"]
    assert [label for _tone, label in badges] == ["Missing", "Not checked"]
    assert "The folder no longer exists." in markup
    assert re.search(r"<td[^>]*>2</td>", markup)
    assert re.search(r"<td[^>]*>1</td>", rows["/opt/billing"][2])
    page = main_of(response.text)
    assert 'data-fragment="paths-table"' in page and 'hx-trigger="paths-changed from:body"' in page
    assert page.count('data-js="dialog-open"') == len(rows) + 1
    for column in ("Path", "Mounted", "Writable", "Deployments", "Actions"):
        assert f">{column}</th>" in page


async def test_a_mounted_writable_entry_and_a_read_only_one_read_as_such(
    root: httpx.AsyncClient, web_app: FastAPI, host: Path
) -> None:
    """The mounted and writable badges follow the folder."""
    async with web_app.state.session_factory() as db:
        db.add_all([AllowedPath(path=str(host / "alpha")), AllowedPath(path=str(host / "beta"))])
        await db.commit()
    (host / "beta").chmod(0o500)
    try:
        rows = rows_of(await _fragment(root, "/settings/paths/table"))
    finally:
        (host / "beta").chmod(0o700)
    assert rows[str(host / "alpha")][1] == [("deployed", "Mounted"), ("deployed", "Writable")]
    assert rows[str(host / "beta")][1] == [("deployed", "Mounted"), ("halted", "Read-only")]


async def _fragment(http: httpx.AsyncClient, url: str) -> str:
    """The text of a fragment requested the way htmx does."""
    response = await http.get(url, headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    return response.text


async def test_the_empty_list_says_what_it_means(root: httpx.AsyncClient, web_app: FastAPI) -> None:
    """With no entries the table gives way to a warning that no deployment can be created."""
    async with web_app.state.session_factory() as db:
        await db.execute(delete(AllowedPath))
        await db.commit()
    page = main_of((await root.get("/settings/paths")).text)
    assert "The list is empty. The app runs but cannot create deployments until you add" in page
    assert "<table" not in page and 'data-js="dialog-open" data-target="path-add"' in page


async def test_the_add_dialog_opens_empty_with_the_hint_and_a_disabled_button(
    root: httpx.AsyncClient,
) -> None:
    """The dialog content has an empty field, the neutral hint and no way to submit yet."""
    page = await _fragment(root, "/settings/paths/new")
    assert 'data-fragment="path-add"' in page and ">Add allowed path</h2>" in page
    assert value_of(page, "ap") == ""
    assert "It is checked as you type." in page
    assert re.search(r'<button type="submit" disabled[^>]*id="path-add-submit"', page)
    assert 'hx-get="/settings/paths/validate"' in page
    assert 'hx-trigger="input changed delay:400ms from:#ap"' in page


async def test_the_live_check_lists_each_check_and_toggles_the_button(
    root: httpx.AsyncClient, host: Path
) -> None:
    """A good path shows two passed lines and an enabled button, a bad one a failed line."""
    good = await _fragment(root, f"/settings/paths/validate?path={host}/alpha")
    assert good.count("text-ok-fg") == 3 and "Exists" in good
    assert "Absolute and normalized" in good
    assert f"Inside the mount {host}, writable" in good
    assert re.search(r'<template hx type="partial" hx-target="#path-add-submit"', good)
    assert not re.search(r'<button type="submit" disabled', good)
    bad = await _fragment(root, "/settings/paths/validate?path=/")
    assert "The filesystem root cannot be an allowed path." in bad
    assert re.search(r'<button type="submit" disabled', bad)
    blank = await _fragment(root, "/settings/paths/validate?path=")
    assert "It is checked as you type." in blank
    assert re.search(r'<button type="submit" disabled', blank)


async def test_adding_a_path_stores_audits_announces_and_refreshes_the_table(
    root: httpx.AsyncClient, web_app: FastAPI, host: Path, notifier: Notifier
) -> None:
    """The answer is 204 with the events; the entry, the audit entry and the event follow."""
    response = await post(root, "/settings/paths", {"path": f"{host}/alpha"})
    assert response.status_code == 204
    assert json.loads(response.headers["hx-trigger"]) == CHANGED_AND_CLOSED
    assert await entry_id(web_app, str(host / "alpha"))
    (entry,) = await settings_entries(web_app)
    assert entry.action == "allowed_path.add" and entry.actor_name == "root"
    assert entry.target_name == str(host / "alpha")
    expected = SettingsChanged("allowed_paths", "added", str(host / "alpha"), "root")
    assert notifier.events == [expected]
    rows = rows_of(await _fragment(root, "/settings/paths/table"))
    assert rows[str(host / "alpha")][1] == [("deployed", "Mounted"), ("deployed", "Writable")]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("/", "The filesystem root cannot be an allowed path."),
        ("{host}/missing", "Path does not exist."),
        ("not/absolute", "Path must be absolute."),
        ("{host}/alpha/../beta", "Path must not contain '..' components."),
        ("", None),
    ],
)
async def test_a_refused_path_swaps_the_dialog_with_the_reason_and_stores_nothing(
    root: httpx.AsyncClient,
    web_app: FastAPI,
    host: Path,
    notifier: Notifier,
    raw: str,
    message: str | None,
) -> None:
    """The refusal is a 422 with the value kept, the reason under the field and Add disabled."""
    typed = raw.format(host=host)
    response = await post(root, "/settings/paths", {"path": typed})
    assert response.status_code == 422
    page = response.text
    assert 'data-fragment="path-add"' in page and value_of(page, "ap") == typed
    assert re.search(r'<button type="submit" disabled[^>]*id="path-add-submit"', page)
    assert error_of(page, "ap") == (message or "Enter an absolute path.")
    assert 'aria-invalid="true"' in page
    assert await settings_entries(web_app) == [] and notifier.events == []


async def test_a_path_in_the_list_cannot_be_added_again(
    root: httpx.AsyncClient, web_app: FastAPI, host: Path
) -> None:
    """The duplicate is refused with its own message and does not add an audit entry."""
    assert (await post(root, "/settings/paths", {"path": f"{host}/alpha"})).status_code == 204
    again = await post(root, "/settings/paths", {"path": f"{host}/alpha/"})
    assert again.status_code == 422
    assert error_of(again.text, "ap") == "This path is already in the list."
    assert len(await settings_entries(web_app)) == 1


async def test_the_remove_dialog_states_what_happens_to_the_deployments(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The text counts the deployments the removal leaves outside the list."""
    fancy = await entry_id(web_app, "/opt/rentals")
    page = await _fragment(root, f"/settings/paths/{fancy}/remove")
    assert 'data-fragment="path-remove"' in page and ">Remove /opt/rentals</h2>" in page
    assert "Nothing is deleted. The 2 deployments under this path are flagged as outside" in page
    assert "or the deployments are moved." in page
    billing = await entry_id(web_app, "/opt/billing")
    one = await _fragment(root, f"/settings/paths/{billing}/remove")
    assert "The 1 deployment under this path is flagged as outside allowed paths and" in one
    assert re.search(rf'hx-post="/settings/paths/{billing}/remove"', one)
    assert ">Remove path</button>" in one and ">Cancel</button>" in one


async def test_the_remove_dialog_of_an_unused_entry_says_nothing_is_left_outside(
    root: httpx.AsyncClient, web_app: FastAPI, host: Path
) -> None:
    """An entry with no deployment under it has nothing to flag."""
    await post(root, "/settings/paths", {"path": f"{host}/beta"})
    beta = await entry_id(web_app, str(host / "beta"))
    page = await _fragment(root, f"/settings/paths/{beta}/remove")
    assert "Nothing is deleted. No deployment is left outside the allowed paths." in page


async def test_removing_a_path_audits_announces_and_flags_its_deployments(
    root: httpx.AsyncClient, web_app: FastAPI, notifier: Notifier
) -> None:
    """The entry goes, the audit entry counts the flagged deployments and the deployment page
    shows the outside state."""
    fancy = await entry_id(web_app, "/opt/rentals")
    response = await post(root, f"/settings/paths/{fancy}/remove")
    assert response.status_code == 204
    assert json.loads(response.headers["hx-trigger"]) == CHANGED_AND_CLOSED
    async with web_app.state.session_factory() as db:
        assert await db.get(AllowedPath, fancy) is None
        removal = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "allowed_path.remove"))
        ).scalar_one()
    assert removal.details == {"path": "/opt/rentals", "deployments_outside": 2}
    assert notifier.events == [SettingsChanged("allowed_paths", "removed", "/opt/rentals", "root")]
    async with web_app.state.session_factory() as db:
        found = await db.execute(select(Deployment.id).where(Deployment.name == "prod"))
        prod_id = found.scalar_one()
    page = (await root.get(f"/deployments/{prod_id}")).text
    assert "This deployment is outside the allowed paths." in page


async def test_removing_an_entry_twice_answers_that_it_is_gone(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The second removal is a 409 with the dialog saying so and the table refreshing."""
    fancy = await entry_id(web_app, "/opt/rentals")
    assert (await post(root, f"/settings/paths/{fancy}/remove")).status_code == 204
    again = await post(root, f"/settings/paths/{fancy}/remove")
    assert again.status_code == 409
    assert "This path is no longer in the list." in again.text
    assert json.loads(again.headers["hx-trigger"]) == {"paths-changed": True}
    assert (await root.get(f"/settings/paths/{fancy}/remove", headers=HTMX)).status_code == 404
    removed = [e for e in await settings_entries(web_app) if e.action == "allowed_path.remove"]
    assert len(removed) == 1
