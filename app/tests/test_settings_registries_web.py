"""The Registries tab: the list, the dialogs, the login test, the audit, the token kept out.

The tests run the real application on the seeded temporary database and read rows, audit entries
and every response back, so a token that reached any of them in plain text is found.
"""

import base64
import html
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.crypto import CryptoService
from app.models import AuditEntry, AuditResult, DockerRegistry
from app.services.registries import registry_context
from app.services.registry_check import (
    ACTION_TEST,
    OK_MESSAGE,
    REFUSED_MESSAGE,
    UNAVAILABLE_SUFFIX,
)
from app.tests.conftest import sign_in
from app.tests.fake_docker import FakeDocker
from app.tests.settings_helpers import NON_SUPERADMINS, browser_as, error_of, value_of
from app.tests.web_helpers import HTMX, assert_never_cached, events_of, main_of, post

TOKEN = "dckr_pat_Zq81-secret.token_value"
OTHER_TOKEN = "dckr_pat_replacement-token-9f3"
CHANGED_AND_CLOSED = {"registries-changed": True, "close-dialogs": True}
ROW = re.compile(r'<tr data-fragment="registry-row" id="registry-(\d+)">(.*?)</tr>', re.DOTALL)
HOST_CELL = re.compile(r'<span class="wrap-anywhere font-mono text-\[13px\]">([^<]+)</span>')
ACTIONS = re.compile(r'data-row-action="(\w+)"')
REGISTRY_ACTIONS = ("registry.add", "registry.update", "registry.remove", ACTION_TEST)


def encoded(token: str, username: str = "bot") -> str:
    """The base64 of ``username:token``, the form a Docker config file keeps."""
    return base64.b64encode(f"{username}:{token}".encode()).decode("ascii")


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker whose commands succeed and print one line."""
    return FakeDocker(tmp_path / "fake")


@pytest.fixture
def docker(web_app: FastAPI, fake: FakeDocker) -> FakeDocker:
    """The application on a usable fake Docker."""
    web_app.state.docker_cli = fake.cli()
    return fake


async def add_login(
    http: httpx.AsyncClient,
    host: str = "registry.example.com",
    username: str = "bot",
    token: str = TOKEN,
) -> None:
    """Add a registry through the dialog's post."""
    form = {"host": host, "username": username, "token": token}
    response = await post(http, "/settings/registries", form)
    assert response.status_code == 204, response.text


async def stored(web_app: FastAPI) -> list[DockerRegistry]:
    """The registry rows in the database, in id order."""
    async with web_app.state.session_factory() as db:
        query = select(DockerRegistry).order_by(DockerRegistry.id)
        return list((await db.execute(query)).scalars())


async def registry_id(web_app: FastAPI, host: str) -> int:
    """The id of the stored registry with this host."""
    (found,) = [registry.id for registry in await stored(web_app) if registry.host == host]
    return found


async def entries(web_app: FastAPI, *actions: str) -> list[AuditEntry]:
    """The audit entries of the registry actions (all of them by default), oldest first."""
    async with web_app.state.session_factory() as db:
        query = select(AuditEntry).where(AuditEntry.action.in_(actions or REGISTRY_ACTIONS))
        return list((await db.execute(query.order_by(AuditEntry.id))).scalars())


async def opened_token(web_app: FastAPI, host: str) -> str:
    """The token stored for a host, opened with the key of the application."""
    (registry,) = [row for row in await stored(web_app) if row.host == host]
    crypto: CryptoService = web_app.state.crypto
    return crypto.decrypt_text(registry.token_encrypted, registry_context(registry.id))


def rows_of(page: str) -> dict[str, tuple[int, str]]:
    """Registry id and row markup by host, in the table of a page or fragment."""
    found: dict[str, tuple[int, str]] = {}
    for row_id, markup in ROW.findall(page):
        host = HOST_CELL.search(markup)
        assert host is not None
        found[host.group(1)] = (int(row_id), markup)
    return found


async def fragment(http: httpx.AsyncClient, url: str, status_code: int = 200) -> str:
    """The text of a fragment requested the way htmx does."""
    response = await http.get(url, headers=HTMX)
    assert response.status_code == status_code, response.text
    assert_never_cached(response)
    return response.text


async def test_only_the_superadmin_opens_or_changes_the_registries(web_app: FastAPI) -> None:
    """Every other group gets 403 on every route, and nothing is stored or audited."""
    async with browser_as(web_app, "root") as root:
        await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    before = await stored(web_app)
    form = {"host": "other.example.com", "username": "mallory", "token": OTHER_TOKEN}
    reads = (
        "/settings/registries",
        "/settings/registries/table",
        "/settings/registries/new",
        f"/settings/registries/{number}/edit",
        f"/settings/registries/{number}/remove",
    )
    writes = (
        "/settings/registries",
        f"/settings/registries/{number}/edit",
        f"/settings/registries/{number}/remove",
        f"/settings/registries/{number}/test",
    )
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            for path in reads:
                assert (await http.get(path)).status_code == 403, (user, path)
                assert (await http.get(path, headers=HTMX)).status_code == 403, (user, path)
            for path in writes:
                assert (await post(http, path, form)).status_code == 403, (user, path)
    after = await stored(web_app)
    assert [(row.id, row.host, row.username, row.token_encrypted) for row in after] == [
        (row.id, row.host, row.username, row.token_encrypted) for row in before
    ]
    assert [entry.action for entry in await entries(web_app)] == ["registry.add"]


async def test_an_anonymous_visitor_is_sent_to_sign_in(web_client: httpx.AsyncClient) -> None:
    """Signed out, the tab answers like every other settings screen."""
    response = await web_client.get("/settings/registries")
    assert (response.status_code, response.headers["location"]) == (303, "/login")


async def test_the_tab_explains_the_logins_and_starts_with_an_empty_state(
    root: httpx.AsyncClient,
) -> None:
    """With no login the panel says what they are for and offers to add one, twice."""
    response = await root.get("/settings/registries")
    assert response.status_code == 200
    assert_never_cached(response)
    page = main_of(response.text)
    assert 'data-fragment="registries-table"' in page
    assert 'hx-trigger="registries-changed from:body"' in page
    assert 'hx-get="/settings/registries/table"' in page
    assert "Post-deploy commands sign in to these registries to pull private images." in page
    assert "one login per registry host" in page.lower()
    assert "pull-only bot account or a read-only token" in page
    assert "No registry logins" in page and "<table" not in page
    assert page.count('data-target="registry-add"') == 2
    for dialog in ("registry-add", "registry-edit", "registry-remove", "registry-test"):
        assert f'<dialog id="{dialog}"' in page
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', response.text, re.DOTALL)
    assert nav is not None and 'href="/settings/registries" aria-current="page"' in nav.group(0)


async def test_the_table_lists_each_login_with_its_username_and_a_stored_token(
    root: httpx.AsyncClient, web_app: FastAPI, docker: FakeDocker
) -> None:
    """One row per host, in host order, with the token only ever shown as Stored."""
    await add_login(root, host="registry.example.com", username="bot")
    await add_login(root, host="ghcr.io", username="robot", token=OTHER_TOKEN)
    page = main_of((await root.get("/settings/registries")).text)
    for column in ("Host", "Username", "Token", "Updated", "Actions"):
        assert f">{column}</th>" in page
    rows = rows_of(page)
    assert list(rows) == ["ghcr.io", "registry.example.com"]
    markup = rows["registry.example.com"][1]
    assert ">bot</span>" in markup and ">Stored</span>" in markup
    assert "<time datetime=" in markup
    assert "registries-docker-note" not in page
    for token in (TOKEN, OTHER_TOKEN):
        assert token not in page and encoded(token) not in page
    assert ACTIONS.findall(markup) == ["test", "edit", "remove"]
    number = rows["registry.example.com"][0]
    assert f'hx-post="/settings/registries/{number}/test"' in markup
    assert f'hx-get="/settings/registries/{number}/edit"' in markup
    assert f'hx-get="/settings/registries/{number}/remove"' in markup
    assert 'hx-sync="this:drop"' in markup
    refreshed = await fragment(root, "/settings/registries/table")
    assert rows_of(refreshed) == rows


async def test_without_a_working_docker_the_test_action_is_absent_and_the_reason_is_shown(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The dialog tests need Docker: the rows keep Edit and Remove and the panel says why."""
    await add_login(root)
    for markup in (
        main_of((await root.get("/settings/registries")).text),
        await fragment(root, "/settings/registries/table"),
    ):
        row = next(iter(rows_of(markup).values()))[1]
        assert ACTIONS.findall(row) == ["edit", "remove"]
        assert 'id="registries-docker-note"' in markup
        assert "The Docker socket is not mounted. Test login is unavailable." in markup


async def test_the_docker_reason_follows_a_daemon_that_does_not_answer(
    root: httpx.AsyncClient, web_app: FastAPI, fake: FakeDocker
) -> None:
    """A mounted socket with a daemon that fails shows the daemon reason, not the socket one."""
    fake.switch("no-daemon")
    web_app.state.docker_cli = fake.cli()
    await add_login(root)
    page = (await root.get("/settings/registries")).text
    reason = "The Docker daemon does not answer through the socket."
    assert f"{reason} Test login is unavailable." in page
    assert 'data-row-action="test"' not in page


async def test_adding_stores_the_login_audits_it_and_refreshes_the_table(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The answer is 204 with the events; the host is stored normalized and the token sealed."""
    response = await post(
        root,
        "/settings/registries",
        {"host": " HTTPS://Registry.Example.com:5000/ ", "username": " bot ", "token": TOKEN},
    )
    assert response.status_code == 204 and response.content == b""
    assert events_of(response) == CHANGED_AND_CLOSED
    assert_never_cached(response)
    (row,) = await stored(web_app)
    assert (row.host, row.username) == ("registry.example.com:5000", "bot")
    assert row.token_encrypted != TOKEN
    assert await opened_token(web_app, "registry.example.com:5000") == TOKEN
    (entry,) = await entries(web_app)
    assert entry.action == "registry.add" and entry.actor_name == "root"
    assert entry.details == {"host": "registry.example.com:5000", "username": "bot"}
    assert list(rows_of(await fragment(root, "/settings/registries/table"))) == [
        "registry.example.com:5000"
    ]


async def test_the_add_dialog_opens_empty_with_a_password_field(root: httpx.AsyncClient) -> None:
    """Host and username are empty, the token is a password field with the plain hint."""
    page = await fragment(root, "/settings/registries/new")
    assert 'data-fragment="registry-add"' in page and ">Add registry login</h2>" in page
    assert 'hx-post="/settings/registries"' in page and 'hx-target="#registry-add"' in page
    assert [value_of(page, name) for name in ("ra-host", "ra-user", "ra-token")] == ["", "", ""]
    assert re.search(r'<input id="ra-token" name="token" type="password"', page)
    assert 'autocomplete="new-password"' in page
    assert "Stored encrypted and never shown again." in page
    assert ">Add registry</button>" in page and 'placeholder="••••••••"' not in page


@pytest.mark.parametrize(
    ("form", "refused"),
    [
        ({"host": "", "username": "bot", "token": TOKEN}, {"ra-host"}),
        ({"host": "registry.example.com/v2", "username": "bot", "token": TOKEN}, {"ra-host"}),
        ({"host": "registry.example.com", "username": "", "token": TOKEN}, {"ra-user"}),
        ({"host": "registry.example.com", "username": "bot", "token": ""}, {"ra-token"}),
        ({"host": "registry.example.com", "username": "bot", "token": "to\nken"}, {"ra-token"}),
        ({"host": "", "username": "", "token": ""}, {"ra-host", "ra-user", "ra-token"}),
    ],
)
async def test_a_refused_add_keeps_what_was_typed_except_the_token_and_stores_nothing(
    root: httpx.AsyncClient, web_app: FastAPI, form: dict[str, str], refused: set[str]
) -> None:
    """A 422 with the dialog again: a reason under each refused field, host and username kept."""
    response = await post(root, "/settings/registries", form)
    assert response.status_code == 422
    assert "hx-trigger" not in response.headers
    page = response.text
    assert 'data-fragment="registry-add"' in page
    assert value_of(page, "ra-host") == form["host"]
    assert value_of(page, "ra-user") == form["username"]
    assert value_of(page, "ra-token") == ""
    assert {name for name in ("ra-host", "ra-user", "ra-token") if error_of(page, name)} == refused
    assert page.count('aria-invalid="true"') == len(refused)
    if form["token"].strip():
        assert form["token"] not in page
    assert await stored(web_app) == [] and await entries(web_app) == []


async def test_a_host_that_is_in_the_list_is_refused_with_its_own_message(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The same host, the same host in another case and a Docker Hub alias are all refused."""
    await add_login(root, host="registry.example.com")
    await add_login(root, host="docker.io")
    for host, message in (
        ("Registry.Example.com", "The registry registry.example.com is already in the list."),
        (
            "index.docker.io",
            "The registry index.docker.io is the same as docker.io, which is already in the list.",
        ),
    ):
        response = await post(
            root, "/settings/registries", {"host": host, "username": "bot", "token": TOKEN}
        )
        assert response.status_code == 422
        assert error_of(response.text, "ra-host") == message
        assert value_of(response.text, "ra-host") == host
    assert len(await stored(web_app)) == 2 and len(await entries(web_app)) == 2


async def test_the_edit_dialog_shows_the_stored_values_and_an_empty_token_field(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Host and username are filled, the token field is empty with the keep hint."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    page = await fragment(root, f"/settings/registries/{number}/edit")
    assert 'data-fragment="registry-edit"' in page and ">Edit registry login</h2>" in page
    assert "registry.example.com</p>" in page
    assert f'hx-post="/settings/registries/{number}/edit"' in page
    assert value_of(page, "re-host") == "registry.example.com"
    assert value_of(page, "re-user") == "bot"
    assert value_of(page, "re-token") == ""
    assert re.search(r'<input id="re-token" name="token" type="password"', page)
    assert 'placeholder="••••••••"' in page and "Stored. Leave empty to keep it." in page
    assert TOKEN not in page and encoded(TOKEN) not in page
    assert ">Save login</button>" in page


async def test_an_edit_without_a_token_keeps_the_stored_one(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The host and username change, the sealed token stays, and the audit says so."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    before = (await stored(web_app))[0].token_encrypted
    response = await post(
        root,
        f"/settings/registries/{number}/edit",
        {"host": "Registry.Example.org", "username": "robot", "token": ""},
    )
    assert response.status_code == 204 and events_of(response) == CHANGED_AND_CLOSED
    (row,) = await stored(web_app)
    assert (row.id, row.host, row.username) == (number, "registry.example.org", "robot")
    assert row.token_encrypted == before
    assert await opened_token(web_app, "registry.example.org") == TOKEN
    entry = (await entries(web_app, "registry.update"))[0]
    assert entry.details == {
        "host": "registry.example.org",
        "username": "robot",
        "host_changed": True,
        "username_changed": True,
        "token_changed": False,
        "previous_host": "registry.example.com",
        "previous_username": "bot",
    }


async def test_an_edit_with_a_token_replaces_the_stored_one(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A new token is sealed in place of the old one and the audit entry only says it changed."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    response = await post(
        root,
        f"/settings/registries/{number}/edit",
        {"host": "registry.example.com", "username": "bot", "token": OTHER_TOKEN},
    )
    assert response.status_code == 204
    assert await opened_token(web_app, "registry.example.com") == OTHER_TOKEN
    (entry,) = await entries(web_app, "registry.update")
    assert entry.details["token_changed"] is True
    assert entry.details["host_changed"] is False
    assert TOKEN not in json.dumps(entry.details) and OTHER_TOKEN not in json.dumps(entry.details)


async def test_an_edit_that_changes_nothing_stores_and_audits_nothing(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Saving the dialog as it was opened is a success without a change."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    response = await post(
        root,
        f"/settings/registries/{number}/edit",
        {"host": "registry.example.com", "username": "bot", "token": ""},
    )
    assert response.status_code == 204
    assert await entries(web_app, "registry.update") == []


async def test_a_refused_edit_keeps_the_typed_host_and_username_and_never_the_token(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The dialog comes back with a reason, the stored host in its subtitle and an empty token."""
    await add_login(root)
    await add_login(root, host="ghcr.io", username="robot", token=OTHER_TOKEN)
    number = await registry_id(web_app, "registry.example.com")
    response = await post(
        root,
        f"/settings/registries/{number}/edit",
        {"host": "Registry.Example.NET", "username": "", "token": "to\nken"},
    )
    assert response.status_code == 422
    page = response.text
    assert 'data-fragment="registry-edit"' in page
    assert value_of(page, "re-host") == "Registry.Example.NET" and value_of(page, "re-user") == ""
    assert value_of(page, "re-token") == "" and "to\nken" not in page
    assert error_of(page, "re-host") is None
    assert error_of(page, "re-user") == "Enter the username."
    assert error_of(page, "re-token") is not None
    assert "registry.example.com</p>" in page
    assert 'placeholder="••••••••"' in page
    taken = await post(
        root,
        f"/settings/registries/{number}/edit",
        {"host": "GHCR.io", "username": "bot", "token": OTHER_TOKEN},
    )
    assert taken.status_code == 422
    assert error_of(taken.text, "re-host") == "The registry ghcr.io is already in the list."
    assert value_of(taken.text, "re-host") == "GHCR.io" and OTHER_TOKEN not in taken.text
    assert await opened_token(web_app, "registry.example.com") == TOKEN
    assert await entries(web_app, "registry.update") == []


async def test_the_remove_dialog_confirms_and_the_post_removes_the_login(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The dialog states the effect; the post deletes the row and audits it."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    page = await fragment(root, f"/settings/registries/{number}/remove")
    assert 'data-fragment="registry-remove"' in page and "Remove registry.example.com" in page
    assert "is refused by the registry until a login is added again" in page
    assert f'hx-post="/settings/registries/{number}/remove"' in page
    assert ">Remove login</button>" in page
    response = await post(root, f"/settings/registries/{number}/remove")
    assert response.status_code == 204 and events_of(response) == CHANGED_AND_CLOSED
    assert await stored(web_app) == []
    (_, removed) = await entries(web_app)
    assert removed.action == "registry.remove"
    assert removed.details == {"host": "registry.example.com", "username": "bot"}
    assert "No registry logins" in await fragment(root, "/settings/registries/table")


@pytest.mark.parametrize(
    ("method", "path", "dialog"),
    [
        ("get", "/settings/registries/{id}/edit", "registry-edit"),
        ("post", "/settings/registries/{id}/edit", "registry-edit"),
        ("get", "/settings/registries/{id}/remove", "registry-remove"),
        ("post", "/settings/registries/{id}/remove", "registry-remove"),
        ("post", "/settings/registries/{id}/test", "registry-test"),
    ],
)
async def test_a_login_that_was_removed_meanwhile_answers_404_with_a_dialog_and_a_refresh(
    root: httpx.AsyncClient,
    web_app: FastAPI,
    docker: FakeDocker,
    method: str,
    path: str,
    dialog: str,
) -> None:
    """The dialog says the registry is gone and the table refreshes to show the list as it is."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    assert (await post(root, f"/settings/registries/{number}/remove")).status_code == 204
    url = path.format(id=number)
    data = {"host": "registry.example.com", "username": "bot", "token": TOKEN}
    response = await root.get(url, headers=HTMX) if method == "get" else await post(root, url, data)
    assert response.status_code == 404
    assert_never_cached(response)
    assert events_of(response) == {"registries-changed": True}
    assert f'data-fragment="{dialog}"' in response.text
    assert "This registry is no longer in the list." in response.text
    assert docker.calls() == []
    assert [entry.action for entry in await entries(web_app)] == ["registry.add", "registry.remove"]


async def test_test_login_signs_in_with_only_that_login_and_leaves_no_folder_behind(
    root: httpx.AsyncClient, web_app: FastAPI, docker: FakeDocker
) -> None:
    """Docker runs ``login <host>`` with a config of that one entry; the answer is OK."""
    await add_login(root)
    await add_login(root, host="ghcr.io", username="robot", token=OTHER_TOKEN)
    number = await registry_id(web_app, "registry.example.com")
    docker.capture_config()
    response = await post(root, f"/settings/registries/{number}/test")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert 'data-fragment="registry-test"' in page and 'data-result="ok"' in page
    assert OK_MESSAGE in page and "<pre" not in page
    assert "registry.example.com as bot" in html.unescape(page)
    (call,) = docker.calls()
    assert call.arguments == ("login", "registry.example.com")
    folder = Path(call.environment["DOCKER_CONFIG"])
    assert folder.name.startswith("run-") and folder.parent == docker.folder / "scratch"
    assert Path(call.working_directory).name == folder.name
    assert not folder.exists()
    assert list((docker.folder / "scratch").glob("run-*")) == []
    captured = docker.captured_config()
    assert captured is not None
    assert captured.content == {"auths": {"registry.example.com": {"auth": encoded(TOKEN)}}}
    assert (captured.file_mode, captured.folder_mode) == (0o600, 0o700)
    assert TOKEN not in " ".join(call.arguments)
    (entry,) = await entries(web_app, ACTION_TEST)
    assert entry.result == AuditResult.SUCCESS and entry.actor_name == "root"
    assert (entry.target_type, entry.target_id, entry.target_name) == (
        "registry",
        number,
        "registry.example.com",
    )
    assert entry.details == {
        "host": "registry.example.com",
        "username": "bot",
        "ok": True,
        "outcome": "ok",
    }


async def test_a_refused_login_shows_the_output_without_the_token(
    root: httpx.AsyncClient, web_app: FastAPI, docker: FakeDocker
) -> None:
    """A non-zero exit is a 200 refusal: the output is scrubbed of the token and its base64."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    docker.behavior(
        f'echo "Error response from daemon: unauthorized for {TOKEN} ({encoded(TOKEN)})"\nexit 1'
    )
    response = await post(root, f"/settings/registries/{number}/test")
    assert response.status_code == 200
    page = response.text
    assert 'data-result="failed"' in page and REFUSED_MESSAGE in page
    assert "<pre" in page and "Error response from daemon: unauthorized for **** (****)" in page
    assert "whitespace-pre-wrap" in page, "the reason is read whole, not scrolled sideways"
    assert TOKEN not in page and encoded(TOKEN) not in page
    assert list((docker.folder / "scratch").glob("run-*")) == []
    (entry,) = await entries(web_app, ACTION_TEST)
    assert entry.result == AuditResult.FAILURE
    assert entry.details["ok"] is False and entry.details["outcome"] == "refused"
    assert "Error response" not in json.dumps(entry.details)
    assert TOKEN not in json.dumps(entry.details)


async def test_the_login_test_says_why_when_docker_cannot_be_used(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A post that arrives without a working Docker runs nothing and gives the reason."""
    await add_login(root)
    number = await registry_id(web_app, "registry.example.com")
    response = await post(root, f"/settings/registries/{number}/test")
    assert response.status_code == 200
    page = response.text
    assert 'data-result="failed"' in page
    assert f"The Docker socket is not mounted. {UNAVAILABLE_SUFFIX}" in page
    (entry,) = await entries(web_app, ACTION_TEST)
    assert entry.result == AuditResult.FAILURE and entry.details["outcome"] == "unavailable"


async def test_the_token_is_in_no_response_header_attribute_or_audit_entry(
    root: httpx.AsyncClient, web_app: FastAPI, docker: FakeDocker
) -> None:
    """Every answer of the tab, success or refusal, is searched for the token in both forms."""
    await add_login(root, token=TOKEN)
    number = await registry_id(web_app, "registry.example.com")
    docker.behavior(f'echo "{TOKEN}"\nexit 1')
    seen = [
        await root.get("/settings/registries"),
        await root.get("/settings/registries/table", headers=HTMX),
        await root.get("/settings/registries/new", headers=HTMX),
        await root.get(f"/settings/registries/{number}/edit", headers=HTMX),
        await root.get(f"/settings/registries/{number}/remove", headers=HTMX),
        await post(
            root,
            "/settings/registries",
            {"host": "bad host", "username": "bot", "token": OTHER_TOKEN},
        ),
        await post(
            root,
            f"/settings/registries/{number}/edit",
            {"host": "bad host", "username": "bot", "token": OTHER_TOKEN},
        ),
        await post(root, f"/settings/registries/{number}/test"),
        await post(
            root,
            f"/settings/registries/{number}/edit",
            {"host": "registry.example.com", "username": "robot", "token": OTHER_TOKEN},
        ),
        await post(root, f"/settings/registries/{number}/remove"),
    ]
    assert len(seen) == 10
    secrets = [TOKEN, encoded(TOKEN), encoded(TOKEN, "robot")]
    secrets += [OTHER_TOKEN, encoded(OTHER_TOKEN), encoded(OTHER_TOKEN, "robot")]
    for response in seen:
        shown = response.text + str(dict(response.headers))
        assert not [secret for secret in secrets if secret in shown], response.url
    async with web_app.state.session_factory() as db:
        texts = [
            json.dumps(entry.details) + str(entry.target_name)
            for entry in (await db.execute(select(AuditEntry))).scalars()
        ]
    assert not [secret for text in texts for secret in secrets if secret in text]
    assert await entries(web_app, ACTION_TEST)
