"""Editor pages and fragments on the seeded application: masking, reveal, saving and access."""

import html
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, EnvironmentVersion
from app.seeds import EDITOR_SECRET_VALUES
from app.services.notify_events import FileChange, FileChanged
from app.tests.conftest import sign_in
from app.tests.notify_helpers import RecordingEvents, file_event_ids
from app.tests.web_helpers import HTMX, EnvironmentIds, assert_never_cached, main_of, post

CERTIFICATE_BODY = "c2VlZA=="
EDITOR_VALUES = (*EDITOR_SECRET_VALUES, "db.internal.example.com", "TLSv1.2", "AQIEAgAAuTAN")
EDITORS = ("root", "admin", "project_admin", "deployment_admin")
VIEWERS = ("viewer",)
EDITOR_ENV_ROLES = (*EDITORS, *VIEWERS)
OUT_OF_SCOPE_FOR_EDITOR = ("env_scoped_admin", "env_scoped_viewer", "ungranted_admin")


def masked_urls(environment: int, keys: tuple[str, ...]) -> list[tuple[str, dict[str, str]]]:
    """Every GET a signed-in caller can make for an environment, with and without htmx."""
    base = f"/environments/{environment}"
    urls = [base, f"{base}?mode=raw", f"{base}/panel", f"{base}/panel?mode=raw"]
    urls += [f"{base}/rows/{key}" for key in keys]
    return [(url, headers) for url in urls for headers in ({}, HTMX)]


@pytest.mark.parametrize("role", EDITOR_ENV_ROLES)
async def test_masked_responses_never_contain_a_value(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, role: str
) -> None:
    await sign_in(web_client, role)
    keys = ("MYSQL_HOST", "MYSQL_PASSWORD", "TLS_CA_CERT", "MAX_CONNECTIONS")
    requests = masked_urls(ids.editor, keys)
    if role in EDITORS:
        base = f"/environments/{ids.editor}"
        requests += [
            (f"{base}/rows/MYSQL_PASSWORD/edit", HTMX),
            (f"{base}/rows/TLS_CA_CERT/edit", HTMX),
            (f"{base}/new-row", HTMX),
        ]
    for url, headers in requests:
        response = await web_client.get(url, headers=headers)
        assert response.status_code == 200, (role, url)
        assert_never_cached(response)
        leaked = [value for value in EDITOR_VALUES if value in response.text]
        assert not leaked, (role, url, leaked)
        assert "BEGIN CERTIFICATE" not in response.text, (role, url)
        assert "rotated by hand" not in response.text, (role, url)


@pytest.mark.parametrize("role", ["root", "admin", "project_admin", "viewer"])
async def test_masked_certificate_never_contains_its_body(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, role: str
) -> None:
    await sign_in(web_client, role)
    for url in (f"/environments/{ids.ca}", f"/environments/{ids.ca}/panel?mode=raw"):
        for headers in ({}, HTMX):
            response = await web_client.get(url, headers=headers)
            assert response.status_code == 200
            assert_never_cached(response)
            assert CERTIFICATE_BODY not in response.text
            assert "BEGIN CERTIFICATE" not in response.text
            assert "3 lines" in response.text


@pytest.mark.parametrize("role", ["env_scoped_admin", "env_scoped_viewer"])
async def test_environment_scoped_users_see_their_file_masked(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, role: str
) -> None:
    await sign_in(web_client, role)
    for url in (f"/environments/{ids.db}", f"/environments/{ids.db}/panel?mode=raw"):
        response = await web_client.get(url)
        assert response.status_code == 200
        assert "3306" not in response.text and "DB_HOST" in response.text
        assert_never_cached(response)


@pytest.mark.parametrize("role", OUT_OF_SCOPE_FOR_EDITOR)
async def test_out_of_scope_environments_answer_404_everywhere(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, role: str
) -> None:
    await sign_in(web_client, role)
    base = f"/environments/{ids.editor}"
    for url in (base, f"{base}/panel", f"{base}/rows/MYSQL_HOST", f"{base}/rows/MYSQL_HOST/edit"):
        assert (await web_client.get(url)).status_code == 404, url
    assert (await web_client.get(f"{base}/new-row")).status_code == 404
    for url in (f"{base}/reveal/MYSQL_PASSWORD", f"{base}/reveal-raw", f"{base}/versions"):
        response = await post(web_client, url, {"mode": "raw", "content": "A=1"})
        assert response.status_code == 404, url
        assert not any(value in response.text for value in EDITOR_VALUES)
    assert (await web_client.get("/environments/999999")).status_code == 404


async def test_viewer_cannot_reveal_edit_or_save(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "viewer")
    base = f"/environments/{ids.editor}"
    for url in (f"{base}/reveal/MYSQL_PASSWORD", f"{base}/reveal-raw"):
        response = await post(web_client, url)
        assert response.status_code == 403
        assert not any(value in response.text for value in EDITOR_VALUES)
    saved = await post(
        web_client, f"{base}/versions", {"mode": "raw", "content": "A=1\n", "base_version": "3"}
    )
    assert saved.status_code == 403
    for url in (f"{base}/rows/MYSQL_PASSWORD/edit", f"{base}/new-row"):
        assert (await web_client.get(url, headers=HTMX)).status_code == 403
    async with web_app.state.session_factory() as db:
        numbers = (
            (
                await db.execute(
                    select(EnvironmentVersion.number).where(
                        EnvironmentVersion.environment_id == ids.editor
                    )
                )
            )
            .scalars()
            .all()
        )
        reveals = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.reveal"))
        ).first()
    assert sorted(numbers) == [1, 2, 3] and reveals is None


async def test_viewer_page_has_no_edit_or_reveal_controls(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "viewer")
    page = main_of((await web_client.get(f"/environments/{ids.editor}")).text)
    assert "Read-only view. Values stay masked and cannot be revealed or edited" in page
    assert "Values are masked." in page
    for absent in (
        "Reveal",
        "Edit<",
        "Save as new version",
        "Add variable",
        "editor-save",
        "/reveal",
    ):
        assert absent not in page
    assert ">View</a>" in page and "History" in page and "Compare to dist" not in page
    assert page.count('data-fragment="kv-row"') == 6
    raw = main_of((await web_client.get(f"/environments/{ids.editor}?mode=raw")).text)
    assert "MYSQL_PASSWORD=••••••••••••" in raw and "Reveal raw text" not in raw


async def test_admin_page_has_the_editor_controls(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    page = main_of((await web_client.get(f"/environments/{ids.editor}")).text)
    assert "Read-only view" not in page
    assert ">Edit</a>" in page and "Compare to dist" in page
    assert "Save as new version" in page and "Add variable" in page
    assert f'hx-post="/environments/{ids.editor}/reveal/MYSQL_PASSWORD?version=3"' in page
    assert "multiline value, 4 lines" in page
    assert "Values masked. Each reveal is audited and hides after 30 s." in page
    assert 'name="base_version" value="3"' in page
    assert "Pending" not in page and "Never deployed" in page
    assert "version 3 saved, never deployed" in page and "last edited by root" in page
    assert "v3 · " in page and "www-data : www-data" in page
    assert "Saved as version" not in page
    toast = main_of((await web_client.get(f"/environments/{ids.editor}?saved=4")).text)
    assert "Saved as version 4. Pending deploy." in toast
    ignored = main_of((await web_client.get(f"/environments/{ids.editor}?saved=<b>")).text)
    assert "Saved as version" not in ignored


async def test_certificate_opens_as_raw_text_only(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    page = main_of((await web_client.get(f"/environments/{ids.ca}")).text)
    assert "Key / value" not in page and "Raw text" not in page
    assert "not in KEY=VALUE format" in page and "Reveal raw text" in page
    assert 'name="mode" value="raw"' in page


async def test_admin_reveal_returns_only_that_value_and_is_audited(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.editor}/reveal/MYSQL_PASSWORD?version=3")
    assert response.status_code == 200
    assert_never_cached(response)
    assert response.text.strip() == "<span hx-ignore data-reveal-value>Ke3rH6vNa5</span>"
    others = [value for value in EDITOR_VALUES if value != "Ke3rH6vNa5"]
    assert not any(value in response.text for value in others)
    old = await post(web_client, f"/environments/{ids.editor}/reveal/MYSQL_PASSWORD?version=1")
    assert "Zq7vK2mPw9" in old.text and "Ke3rH6vNa5" not in old.text
    async with web_app.state.session_factory() as db:
        entries = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.reveal")))
            .scalars()
            .all()
        )
    assert [entry.details for entry in entries] == [
        {"version": 3, "key": "MYSQL_PASSWORD"},
        {"version": 1, "key": "MYSQL_PASSWORD"},
    ]
    assert not any(value in repr(entry.details) for entry in entries for value in EDITOR_VALUES)
    assert all(
        entry.actor_name == "admin" and entry.environment_id == ids.editor for entry in entries
    )


async def test_reveal_escapes_the_value_and_handles_empty_values(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "root")
    base = f"/environments/{ids.editor}"
    saved = await post(
        web_client,
        f"{base}/versions",
        {
            "mode": "raw",
            "base_version": "3",
            "content": "XSS='<img src=x onerror=alert(1)>'\nEMPTY=\n",
        },
    )
    assert saved.headers["hx-redirect"] == f"{base}?saved=4"
    xss = await post(web_client, f"{base}/reveal/XSS")
    assert "&lt;img src=x onerror=alert(1)&gt;" in xss.text and "<img" not in xss.text
    empty = await post(web_client, f"{base}/reveal/EMPTY")
    assert "Empty value" in empty.text
    assert (await post(web_client, f"{base}/reveal/NOPE")).status_code == 404
    assert (await post(web_client, f"{base}/reveal/XSS?version=99")).status_code == 404


async def test_raw_reveal_keeps_a_leading_blank_line_and_escapes(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "root")
    base = f"/environments/{ids.editor}"
    text = '\n# first line is blank </textarea><b>\nA="x"\n'
    await post(
        web_client, f"{base}/versions", {"mode": "raw", "base_version": "3", "content": text}
    )
    response = await post(web_client, f"{base}/reveal-raw")
    assert response.status_code == 200
    assert_never_cached(response)
    assert "hx-ignore" in response.text and 'name="content"' in response.text
    body = response.text.split(">", 2)[2]
    assert "&lt;/textarea&gt;&lt;b&gt;" in body and "</textarea><b>" not in body
    match = re.search(r"<textarea[^>]*>(.*)</textarea>", response.text, re.DOTALL)
    assert match is not None
    # A parser drops one newline after the opening tag; the rest is exactly the stored text.
    assert match.group(1).startswith("\n\n# first line is blank")
    assert html.unescape(match.group(1)[1:]) == text


async def test_edit_row_starts_empty_and_cancel_returns_the_masked_row(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    base = f"/environments/{ids.editor}"
    edit = (
        await web_client.get(f"{base}/rows/MYSQL_PASSWORD/edit?n=2&version=3", headers=HTMX)
    ).text
    assert 'name="edit_key" value="MYSQL_PASSWORD"' in edit
    assert re.search(r'<textarea[^>]*name="edit_value"[^>]*></textarea>', edit)
    assert "Leave blank to keep the current value." in edit and "Edited" in edit
    cancel = (await web_client.get(f"{base}/rows/MYSQL_PASSWORD?n=2&version=3", headers=HTMX)).text
    assert 'data-fragment="kv-row"' in cancel and "Reveal" in cancel
    assert (await web_client.get(f"{base}/rows/NOPE", headers=HTMX)).status_code == 404
    blank = (await web_client.get(f"{base}/new-row", headers=HTMX)).text
    assert 'name="new_key"' in blank and 'name="new_value"' in blank and "row-remove" in blank


async def test_key_value_save_redirects_and_stores_the_patched_text(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    base = f"/environments/{ids.editor}"
    data = {
        "mode": "kv",
        "base_version": "3",
        "comment": "raise limit",
        "edit_key": ["MAX_CONNECTIONS", "MYSQL_PORT"],
        "edit_value": ["500", ""],
        "remove_key": ["TLS_MIN_VERSION"],
        "new_key": ["", "EXTRA"],
        "new_value": ["", "a b"],
    }
    response = await post(web_client, f"{base}/versions", data)
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"{base}?saved=4"
    assert_never_cached(response)
    async with web_app.state.session_factory() as db:
        version = (
            await db.execute(
                select(EnvironmentVersion).where(
                    EnvironmentVersion.environment_id == ids.editor,
                    EnvironmentVersion.number == 4,
                )
            )
        ).scalar_one()
        text = web_app.state.crypto.decrypt_text(version.content_encrypted)
    assert version.comment == "raise limit"
    assert "export MAX_CONNECTIONS=500\n" in text and "TLS_MIN_VERSION" not in text
    assert "MYSQL_PORT=3306\n" in text and text.endswith("EXTRA='a b'\n")
    page = main_of((await web_client.get(f"{base}?saved=4")).text)
    assert "version 4 saved" in page and "Saved as version 4" in page


async def test_raw_save_and_the_refusals_swap_the_save_bar(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    base = f"/environments/{ids.editor}"
    unchanged = await post(
        web_client, f"{base}/versions", {"mode": "kv", "base_version": "3", "comment": ""}
    )
    assert unchanged.status_code == 422
    assert (
        'data-fragment="editor-save"' in unchanged.text and "No changes to save." in unchanged.text
    )
    assert 'role="alert"' in unchanged.text and 'name="base_version" value="3"' in unchanged.text
    no_text = await post(web_client, f"{base}/versions", {"mode": "raw", "base_version": "3"})
    assert no_text.status_code == 422 and "Reveal the raw text to edit it." in no_text.text
    assert 'name="mode" value="raw"' in no_text.text
    saved = await post(
        web_client,
        f"{base}/versions",
        {"mode": "raw", "base_version": "3", "content": "ONLY=1\r\n", "comment": ""},
    )
    assert saved.headers["hx-redirect"] == f"{base}?saved=4"
    stale = await post(
        web_client, f"{base}/versions", {"mode": "raw", "base_version": "3", "content": "X=1\n"}
    )
    assert stale.status_code == 422
    assert "This file changed since you opened it." in stale.text
    for bad in ("", "abc", "-1", "9" * 30):
        response = await post(
            web_client, f"{base}/versions", {"mode": "raw", "base_version": bad, "content": "X=1"}
        )
        assert response.status_code == 422


async def test_save_needs_a_csrf_token_and_a_length(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    refused = await web_client.post(
        f"/environments/{ids.editor}/versions", data={"mode": "raw", "content": "A=1"}
    )
    assert refused.status_code == 403
    too_large = await post(
        web_client,
        f"/environments/{ids.editor}/versions",
        {"mode": "raw", "base_version": "3", "content": "A" * 1_100_000},
    )
    assert too_large.status_code == 422 and "larger than 1024 KiB" in too_large.text


async def test_saving_a_version_announces_the_file_change_to_the_notifier(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    """The save route hands its notifier to the service, which tells it after the commit."""
    recorder = RecordingEvents()
    web_app.state.notifier = recorder
    await sign_in(web_client, "admin")
    response = await post(
        web_client,
        f"/environments/{ids.editor}/versions",
        {"mode": "raw", "base_version": "3", "content": "A=1\n", "comment": "one"},
    )
    assert response.status_code == 200
    project, deployment, service, name = await file_event_ids(web_app, ids.editor)
    assert recorder.events == [
        FileChanged(project, deployment, service, ids.editor, name, 4, FileChange.SAVED, "admin")
    ]
    refused = await post(
        web_client,
        f"/environments/{ids.editor}/versions",
        {"mode": "raw", "base_version": "3", "content": "B=2\n", "comment": ""},
    )
    assert refused.status_code == 422 and len(recorder.events) == 1
