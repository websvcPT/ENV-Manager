"""Tests for the Git tab: the form, the save, the credentials never shown, the test and the pin."""

import html
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AppSetting, AuditEntry
from app.services.app_settings import (
    GIT_DEPLOY_KEY_KEY,
    GIT_TOKEN_KEY,
    GitSettings,
    read_secret,
)
from app.services.audit import AuditActor
from app.services.git_errors import PROBLEMS, GitProblem
from app.services.git_hostkey import parse_entry
from app.services.git_process import GitOutput
from app.services.git_push import push_if_due
from app.services.git_state import load_state, pin_host_key, record_failure
from app.services.notify_events import GitPushFailed
from app.tests.conftest import sign_in
from app.tests.git_helpers import (
    RecordingEvents,
    RecordingRunner,
    generate_key,
    keygen_fingerprint,
    no_matching_refs,
    ok,
)
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    browser_as,
    changes_of,
    error_of,
    selected_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
    value_of,
)
from app.tests.web_helpers import assert_never_cached, hx_vals, post

SSH_URL = "ssh://git@git.example.com:2222/ops/env.git"
HTTPS_URL = "https://ops@git.example.com/ops/env.git"
TOKEN = "tok-9f2c1d7a5b3e-SECRET"
FORM = {
    "url": HTTPS_URL,
    "branch": "main",
    "author_name": "ENV Manager",
    "export_format": "per_version",
    "push_enabled": "on",
    "token": TOKEN,
}
NOTHING_PUSHED = "Nothing has been pushed yet."


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


@pytest.fixture
def runner(web_app: FastAPI, tmp_path: Path) -> RecordingRunner:
    """A recording runner in place of the application's, with the network commands scripted."""
    recording = RecordingRunner(scratch=tmp_path / "git")
    web_app.state.git_services = replace(web_app.state.git_services, runner=recording)
    return recording


def scan_lines(*lines: str) -> GitOutput:
    """What ssh-keyscan prints for a server: its key lines and a comment."""
    return GitOutput(0, False, "\n".join([*lines, "# git.example.com:2222 SSH-2.0-OpenSSH_10"]))


def banner_area(page: str) -> str:
    """The markup of the banner area in a response."""
    rest = page.split('<div id="banner-area"', 1)[1]
    return rest.split("<main", 1)[0]


def textarea_of(page: str, field_id: str) -> str:
    """The text inside the textarea with this id."""
    found = re.search(rf'<textarea id="{field_id}"[^>]*>([^<]*)</textarea>', page)
    assert found is not None, field_id
    return html.unescape(found.group(1))


async def test_only_the_superadmin_may_read_or_change_the_git_settings(web_app: FastAPI) -> None:
    """Every other group gets 403 on the tab, the save, the test and the pin; nothing is stored."""
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            assert (await http.get("/settings/git")).status_code == 403, user
            assert (await post(http, "/settings/git", FORM)).status_code == 403, user
            assert (await post(http, "/settings/git/test")).status_code == 403, user
            pin = await post(http, "/settings/git/pin", {"key_type": "x", "fingerprint": "y"})
            assert pin.status_code == 403, user
    assert await stored_rows(web_app) == {}


async def test_the_tab_shows_the_defaults_and_no_credential_or_sync_state(
    root: httpx.AsyncClient,
) -> None:
    """A new installation: no address, branch main, one file per version, push switched on."""
    response = await root.get("/settings/git")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert "<title>Settings · Git · ENV Manager</title>" in page
    assert (value_of(page, "gu"), value_of(page, "gb"), value_of(page, "ga")) == (
        "",
        "main",
        "ENV Manager",
    )
    assert selected_of(page, "gf") == "per_version"
    assert re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', page) == [
        ("per_version", "One file per version"),
        ("snapshot", "Encrypted database snapshot"),
    ]
    assert textarea_of(page, "gk") == "" and value_of(page, "gt") == ""
    assert '<input type="checkbox" name="push_enabled" checked id="gp"' in page
    assert 'name="remove_deploy_key"' not in page and 'name="remove_token"' not in page
    assert 'hx-post="/settings/git/test"' in page and 'hx-target="#git-test"' in page
    assert "Pushes an encrypted export only, never plaintext." in page
    assert NOTHING_PUSHED not in page and "Host key pinned" not in page
    assert "Git push failing" not in banner_area(page)


async def test_saving_stores_the_settings_with_the_credential_and_audits_by_host(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The answer is the form with a toast and the banner area; the audit entry keeps the origin
    of the address, and the token is sealed in its own row."""
    response = await post(root, "/settings/git", FORM)
    assert response.status_code == 200
    assert_never_cached(response)
    assert toast_of(response.text) == ("Git settings saved", "The next scheduled push uses them.")
    assert 'hx-swap-oob="true"' in banner_area(response.text)
    assert (await stored(web_app)).git == GitSettings(url=HTTPS_URL)
    async with web_app.state.session_factory() as db:
        assert await read_secret(db, web_app.state.crypto, GIT_TOKEN_KEY) == TOKEN
    changes = [change for entry in await settings_entries(web_app) for change in changes_of(entry)]
    assert {
        "setting": "git.url",
        "kind": "url",
        "old": "",
        "new": "https://git.example.com",
    } in changes
    assert {"setting": "git.token", "kind": "secret"} in changes


async def test_the_credentials_are_sealed_and_never_shown_or_audited(
    root: httpx.AsyncClient, web_app: FastAPI, tmp_path: Path
) -> None:
    """Not in the answer, the page, the rows in the clear or any audit entry; the page only says
    that one is stored and offers to remove it."""
    key_text, _public = generate_key(tmp_path, "deploy")
    response = await post(
        root,
        "/settings/git",
        {**FORM, "url": SSH_URL, "deploy_key": key_text.replace("\n", "\r\n")},
    )
    page = (await root.get("/settings/git")).text
    body = next(line for line in key_text.splitlines() if line and "-----" not in line)
    for text in (response.text, page):
        assert TOKEN not in text and body not in text
        assert textarea_of(text, "gk") == "" and value_of(text, "gt") == ""
        assert text.count('placeholder="••••••••"') == 2 and "Leave empty to keep it." in text
        assert 'name="remove_deploy_key"' in text and 'name="remove_token"' in text
    async with web_app.state.session_factory() as db:
        rows = (await db.execute(select(AppSetting))).scalars().all()
        assert not any(TOKEN in row.value or body in row.value for row in rows)
        assert await read_secret(db, web_app.state.crypto, GIT_DEPLOY_KEY_KEY) == key_text
        entries = (await db.execute(select(AuditEntry))).scalars().all()
        assert not any(TOKEN in repr(e.details) or body in repr(e.details) for e in entries)


async def test_empty_credential_fields_keep_what_is_stored_and_a_choice_removes_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Saving another field leaves the sealed token alone; Remove deletes it and says so."""
    await post(root, "/settings/git", FORM)
    async with web_app.state.session_factory() as db:
        before = (await db.get(AppSetting, GIT_TOKEN_KEY)).value
    await post(root, "/settings/git", {**FORM, "token": "", "branch": "release"})
    async with web_app.state.session_factory() as db:
        assert (await db.get(AppSetting, GIT_TOKEN_KEY)).value == before
    assert (await stored(web_app)).git.branch == "release"
    removed = await post(root, "/settings/git", {**FORM, "token": "", "remove_token": "on"})
    assert toast_of(removed.text) == ("Git settings saved", "The next scheduled push uses them.")
    async with web_app.state.session_factory() as db:
        assert await db.get(AppSetting, GIT_TOKEN_KEY) is None
    assert 'name="remove_token"' not in (await root.get("/settings/git")).text


async def test_a_save_with_nothing_changed_says_so(root: httpx.AsyncClient) -> None:
    """The same values again store nothing and show the notice."""
    await post(root, "/settings/git", FORM)
    again = await post(root, "/settings/git", {**FORM, "token": ""})
    assert toast_of(again.text) == ("Nothing to save", "The settings are unchanged.")


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        (
            {"url": "https://user:hunter2@git.example.com/ops/env.git"},
            "gu",
            "Do not put a password or token in the address. Use the credential field.",
        ),
        (
            {"url": "http://git.example.com/ops/env.git"},
            "gu",
            "Use https. An access token is never sent over http.",
        ),
        (
            {"url": "/srv/git/env.git"},
            "gu",
            "Use an ssh:// or https:// address, or the form git@host:path.",
        ),
        (
            {"url": "--upload-pack=x"},
            "gu",
            "Use an ssh:// or https:// address, or the form git@host:path.",
        ),
        ({"branch": "bad branch"}, "gb", "Enter a branch name such as main."),
        ({"branch": "../x"}, "gb", "Enter a branch name such as main."),
        (
            {"author_name": "Ann <ann@example.com>"},
            "ga",
            "Use printable characters, without < or >.",
        ),
        ({"author_name": ""}, "ga", "Enter a name of 1 to 100 characters."),
        ({"export_format": "zip"}, "gf", "Choose one of the listed formats."),
        (
            {"deploy_key": "ssh-ed25519 AAAAC3Nz test"},
            "gk",
            "That is a public key. Paste the private key.",
        ),
        (
            {"deploy_key": "not a key"},
            "gk",
            "Paste the whole private key, starting with -----BEGIN ... PRIVATE KEY-----.",
        ),
        (
            {
                "deploy_key": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n"
                "-----END ENCRYPTED PRIVATE KEY-----"
            },
            "gk",
            "The key must not have a passphrase.",
        ),
        (
            {
                "deploy_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                "b3BlbnNzaC1rZXktdjEAAAAACmFlczI1Ni1jdHIAAAAGYmNyeXB0\n"
                "-----END OPENSSH PRIVATE KEY-----"
            },
            "gk",
            "The key must not have a passphrase.",
        ),
        ({"token": "has a space"}, "gt", "Use printable characters without spaces, at most 1024."),
        (
            {"token": "new", "remove_token": "on"},
            "gt",
            "Type a new token or remove the stored one, not both.",
        ),
    ],
)
async def test_refused_values_show_their_reason_and_store_nothing(
    root: httpx.AsyncClient,
    web_app: FastAPI,
    changes: dict[str, str],
    field: str,
    message: str,
) -> None:
    """A 422 answer with the reason under the field, the typed values kept (never a credential),
    and no row, secret or audit entry written."""
    response = await post(root, "/settings/git", {**FORM, **changes})
    assert response.status_code == 422
    assert error_of(response.text, field) == message
    assert textarea_of(response.text, "gk") == "" and value_of(response.text, "gt") == ""
    assert "hunter2" not in response.text
    assert await stored_rows(web_app) == {}
    assert await settings_entries(web_app) == []


async def test_a_save_ends_the_failure_state_and_the_banner(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Fixing the settings clears the banner at once; an unchanged save leaves it."""
    await post(root, "/settings/git", FORM)
    reason = PROBLEMS[GitProblem.AUTH].message
    async with web_app.state.session_factory() as db:
        state = await load_state(db)
        record_failure(state, reason, web_app.state.git_services.clock())
        await db.commit()
    page = (await root.get("/projects")).text
    assert "Git push failing" in banner_area(page) and reason in banner_area(page)
    assert 'href="/settings/git"' in banner_area(page)
    tab = (await root.get("/settings/git")).text
    assert "Last push failed" in tab and reason in tab

    same = await post(root, "/settings/git", {**FORM, "token": ""})
    assert "Git push failing" in banner_area(same.text)
    changed = await post(root, "/settings/git", {**FORM, "token": "", "branch": "release"})
    assert "Git push failing" not in banner_area(changed.text)
    async with web_app.state.session_factory() as db:
        state = await load_state(db)
    assert (state.failures, state.last_error, state.retry_at) == (0, None, None)


async def test_the_banner_is_for_the_superadmin_only(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """An admin sees no git banner on any page."""
    await post(root, "/settings/git", FORM)
    async with web_app.state.session_factory() as db:
        record_failure(await load_state(db), "x", web_app.state.git_services.clock())
        await db.commit()
    async with browser_as(web_app, "admin") as http:
        assert "Git push failing" not in (await http.get("/projects")).text


async def test_the_tab_shows_the_last_push_and_the_state_of_the_sync(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Nothing pushed yet, a push that worked, and a failure with its reason."""
    await post(root, "/settings/git", FORM)
    assert NOTHING_PUSHED in (await root.get("/settings/git")).text
    now = web_app.state.git_services.clock()
    async with web_app.state.session_factory() as db:
        state = await load_state(db)
        state.last_success_at = now
        await db.commit()
    page = (await root.get("/settings/git")).text
    assert NOTHING_PUSHED not in page and 'data-status="deployed"' in page
    assert "Last push " in page and "Last push failed" not in page


async def test_an_ssh_address_shows_whether_its_host_key_is_pinned(
    root: httpx.AsyncClient, web_app: FastAPI, tmp_path: Path
) -> None:
    """Without a pin the tab says to test; with one it shows the fingerprint and the rule."""
    await post(root, "/settings/git", {**FORM, "url": SSH_URL, "token": ""})
    assert "No host key is pinned for this address yet." in (await root.get("/settings/git")).text
    _private, public = generate_key(tmp_path, "host")
    key = parse_entry(f"[git.example.com]:2222 {' '.join(public.split()[:2])}")
    assert key is not None
    async with web_app.state.session_factory() as db:
        pin_host_key(await load_state(db), key, web_app.state.git_services.clock())
        await db.commit()
    page = (await root.get("/settings/git")).text
    assert "No host key is pinned" not in page
    assert (
        f"Host key pinned for git.example.com:2222: ssh-ed25519 {key.fingerprint}"
        in html.unescape(re.sub(r"<[^>]+>", "", page))
    )


async def test_the_test_button_shows_a_failure_or_success_without_a_push(
    root: httpx.AsyncClient, web_app: FastAPI, runner: RecordingRunner
) -> None:
    """The test answers 200 with a badge and a sentence; only ls-remote ran."""
    await post(root, "/settings/git", FORM)
    runner.script["ls-remote"] = lambda _call: ok()
    response = await post(root, "/settings/git/test")
    assert response.status_code == 200
    assert 'data-status="deployed"' in response.text and "Connected" in response.text
    assert "The repository answered and the branch exists." in response.text
    assert [call.command for call in runner.calls] == ["ls-remote"]
    runner.script["ls-remote"] = lambda _call: no_matching_refs()
    empty = await post(root, "/settings/git/test")
    assert "The branch does not exist yet; the first push creates it." in empty.text
    runner.script.pop("ls-remote")


async def test_the_test_says_what_is_missing_without_running_anything(
    root: httpx.AsyncClient, runner: RecordingRunner
) -> None:
    """No address saved: a failed badge with the fixed sentence, no process."""
    response = await post(root, "/settings/git/test")
    assert 'data-status="failed"' in response.text and "Connection failed" in response.text
    assert PROBLEMS[GitProblem.NOT_CONFIGURED].message in response.text
    assert runner.calls == []


async def test_a_new_ssh_host_key_is_shown_for_trust_and_pinned_by_the_button(
    root: httpx.AsyncClient, web_app: FastAPI, runner: RecordingRunner, tmp_path: Path
) -> None:
    """The first test shows the fingerprint and a button whose values are exactly that key; the
    button's post pins it, tests again and the pin line appears."""

    key_text, _public = generate_key(tmp_path, "client")
    await post(root, "/settings/git", {**FORM, "url": SSH_URL, "token": "", "deploy_key": key_text})
    _hp, host_public = generate_key(tmp_path, "host")
    key_type, blob = host_public.split()[:2]
    fingerprint = keygen_fingerprint(tmp_path, host_public)
    runner.script["ssh-keyscan"] = lambda _call: scan_lines(
        f"[git.example.com]:2222 {key_type} {blob}"
    )
    runner.script["ls-remote"] = lambda _call: no_matching_refs()

    shown = await post(root, "/settings/git/test")
    assert "Host key not trusted yet" in shown.text and fingerprint in shown.text
    assert hx_vals(shown.text, "/settings/git/pin") == {
        "key_type": key_type,
        "fingerprint": fingerprint,
    }
    assert [call.command for call in runner.calls] == ["ssh-keyscan"]

    assert shown.text.strip().startswith(
        '<div id="git-test" aria-live="polite" class="basis-full">'
    )
    assert 'id="git-pin"' in shown.text and "No host key is pinned" in shown.text

    trusted = await post(root, "/settings/git/pin", hx_vals(shown.text, "/settings/git/pin"))
    assert "Connected" in trusted.text
    assert trusted.text.strip().startswith('<div id="git-test" aria-live="polite">')
    pin_line = re.search(r'<p id="git-pin"[^>]*>.*?</p>', trusted.text, re.DOTALL)
    assert pin_line is not None and 'hx-swap-oob="true"' in pin_line.group(0)
    assert "Host key pinned for git.example.com:2222" in pin_line.group(0)
    assert fingerprint in pin_line.group(0)
    async with web_app.state.session_factory() as db:
        assert (await load_state(db)).host_key_fingerprint == fingerprint
    assert fingerprint in (await root.get("/settings/git")).text


async def test_a_changed_host_key_shows_both_fingerprints_and_how_to_trust_the_new_one(
    root: httpx.AsyncClient, web_app: FastAPI, runner: RecordingRunner, tmp_path: Path
) -> None:
    """A server that no longer presents the pinned key is refused in words, and nothing is sent."""
    key_text, _public = generate_key(tmp_path, "client")
    await post(root, "/settings/git", {**FORM, "url": SSH_URL, "token": "", "deploy_key": key_text})
    _a, first = generate_key(tmp_path, "host-a")
    _b, second = generate_key(tmp_path, "host-b")
    first_type, first_blob = first.split()[:2]
    second_type, second_blob = second.split()[:2]
    pinned = parse_entry(f"[git.example.com]:2222 {first_type} {first_blob}")
    assert pinned is not None
    async with web_app.state.session_factory() as db:
        pin_host_key(await load_state(db), pinned, web_app.state.git_services.clock())
        await db.commit()
    runner.script["ssh-keyscan"] = lambda _call: scan_lines(
        f"[git.example.com]:2222 {second_type} {second_blob}"
    )
    response = await post(root, "/settings/git/test")
    assert "Host key changed" in response.text
    assert pinned.fingerprint in response.text
    assert keygen_fingerprint(tmp_path, second) in response.text
    assert "change the repository address and save" in response.text
    assert "Trust this key" not in response.text
    assert [call.command for call in runner.calls] == ["ssh-keyscan"]


async def test_a_failed_push_reaches_the_notifier_the_application_holds(
    root: httpx.AsyncClient, web_app: FastAPI, runner: RecordingRunner
) -> None:
    """The services built at startup announce a failed push to ``app.state.notifier``, read at the
    moment of the failure, so the notifier can be swapped after startup."""
    await post(root, "/settings/git", FORM)
    runner.script["ls-remote"] = lambda _call: GitOutput(
        128, False, "fatal: Authentication failed for 'https://git.example.com/'"
    )
    events = RecordingEvents()
    web_app.state.notifier = events
    async with web_app.state.session_factory() as db:
        result = await push_if_due(db, web_app.state.git_services, actor=AuditActor.system("test"))
    reason = PROBLEMS[GitProblem.AUTH].message
    assert result.reason == reason
    assert events.events == [GitPushFailed(reason, 1)]


async def test_a_pin_post_for_a_key_that_was_not_shown_is_refused(
    root: httpx.AsyncClient, web_app: FastAPI, runner: RecordingRunner, tmp_path: Path
) -> None:
    """The post names a fingerprint the server does not present: nothing is pinned."""
    key_text, _public = generate_key(tmp_path, "client")
    await post(root, "/settings/git", {**FORM, "url": SSH_URL, "token": "", "deploy_key": key_text})
    _hp, host_public = generate_key(tmp_path, "host")
    key_type, blob = host_public.split()[:2]
    runner.script["ssh-keyscan"] = lambda _call: scan_lines(
        f"[git.example.com]:2222 {key_type} {blob}"
    )
    response = await post(
        root, "/settings/git/pin", {"key_type": key_type, "fingerprint": "SHA256:forged"}
    )
    assert "changed since it was shown" in response.text
    async with web_app.state.session_factory() as db:
        assert (await load_state(db)).host_key is None
