"""History pages and fragments on the seeded application: masking, diff, reveal, restore, access."""

import re

import httpx
import pytest
from fastapi import FastAPI

from app.models import FileType
from app.seeds import EDITOR_SECRET_VALUES
from app.services.notify_events import FileChange, FileChanged
from app.tests.audit_helpers import web_audit_entries
from app.tests.conftest import sign_in
from app.tests.file_type_helpers import set_web_file_type
from app.tests.notify_helpers import RecordingEvents, file_event_ids
from app.tests.web_helpers import (
    HTMX,
    EnvironmentIds,
    add_version,
    assert_never_cached,
    main_of,
    post,
    version_numbers,
)

EDITOR_VALUES = (*EDITOR_SECRET_VALUES, "db.internal.example.com", "TLSv1.2", "AQIEAgAAuTAN")
HIDDEN_TEXT = ("rotated by hand", "BEGIN CERTIFICATE", "sentinelCertBody")
PAIRS = ((1, 2), (2, 3), (1, 3))
EDITORS = ("root", "admin", "project_admin", "deployment_admin")
VIEWERS = ("viewer",)
HISTORY_ROLES = (*EDITORS, *VIEWERS)
OUT_OF_SCOPE = ("env_scoped_admin", "env_scoped_viewer", "ungranted_admin")


def history_urls(environment: int) -> list[tuple[str, dict[str, str]]]:
    """Every GET a signed-in caller can make for the history, with and without htmx."""
    base = f"/environments/{environment}"
    urls = [f"{base}/history", f"{base}/history?v=abc", f"{base}/diff", f"{base}/diff?v=1"]
    urls += [f"{base}/diff?v=1&v=2&v=3", f"{base}/diff?v=1&v=9", f"{base}/diff?v=2&v=2"]
    for left, right in PAIRS:
        urls += [f"{base}/history?v={left}&v={right}", f"{base}/diff?v={left}&v={right}"]
        urls += [f"{base}/diff?v={right}&v={left}"]
    return [(url, headers) for url in urls for headers in ({}, HTMX)]


def version_rows(page: str) -> list[str]:
    """The markup of each version row of the history table."""
    return re.findall(r'<tr data-fragment="version-row".*?</tr>', page, re.DOTALL)


@pytest.mark.parametrize("role", HISTORY_ROLES)
async def test_masked_history_never_contains_a_value(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, role: str
) -> None:
    await sign_in(web_client, role)
    for url, headers in history_urls(ids.editor):
        response = await web_client.get(url, headers=headers)
        assert response.status_code == 200, (role, url)
        assert_never_cached(response)
        leaked = [value for value in (*EDITOR_VALUES, *HIDDEN_TEXT) if value in response.text]
        assert not leaked, (role, url, leaked)
        assert "hx-ignore" not in response.text and "data-reveal-value" not in response.text
        body = main_of(response.text) if "<main" in response.text else response.text
        assert "<textarea" not in body and 'type="hidden"' not in body


@pytest.mark.parametrize("role", ["root", "admin", "viewer"])
async def test_masked_certificate_history_never_contains_its_body(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI, role: str
) -> None:
    await add_version(
        web_app, ids.ca, "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\n-----END CERTIFICATE-----\n"
    )
    await sign_in(web_client, role)
    for url in (f"/environments/{ids.ca}/history", f"/environments/{ids.ca}/diff?v=1&v=2"):
        for headers in ({}, HTMX):
            response = await web_client.get(url, headers=headers)
            assert response.status_code == 200
            assert_never_cached(response)
            for hidden in ("c2VlZA", "bmV3Ym9keQ", "BEGIN CERTIFICATE"):
                assert hidden not in response.text
    panel = (await web_client.get(f"/environments/{ids.ca}/diff?v=1&v=2")).text
    assert "Line 2" in panel and "Changed" in panel and "File content" not in panel
    assert ">Line</span>" in panel and "unchanged" in panel and "unchanged lines hidden" in panel


@pytest.mark.parametrize("role", ["root", "admin", "viewer"])
async def test_masked_free_text_history_of_an_env_file_compares_the_whole_file(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI, role: str
) -> None:
    await set_web_file_type(web_app, ids.ca, FileType.ENV)
    await add_version(
        web_app, ids.ca, "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\n-----END CERTIFICATE-----\n"
    )
    await sign_in(web_client, role)
    for url in (f"/environments/{ids.ca}/history", f"/environments/{ids.ca}/diff?v=1&v=2"):
        response = await web_client.get(url)
        assert response.status_code == 200
        for hidden in ("c2VlZA", "bmV3Ym9keQ", "BEGIN CERTIFICATE"):
            assert hidden not in response.text
    panel = (await web_client.get(f"/environments/{ids.ca}/diff?v=1&v=2")).text
    assert "File content" in panel and "Changed" in panel


@pytest.mark.parametrize("role", OUT_OF_SCOPE)
async def test_out_of_scope_users_get_404_on_every_history_route(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, role: str
) -> None:
    await sign_in(web_client, role)
    base = f"/environments/{ids.editor}"
    for url in (f"{base}/history", f"{base}/diff?v=1&v=2", f"{base}/history?v=1&v=2"):
        assert (await web_client.get(url)).status_code == 404, url
    for url in (f"{base}/history/reveal?v=2&v=3", f"{base}/restore/1?base=3"):
        response = await post(web_client, url)
        assert response.status_code == 404, url
        assert not any(value in response.text for value in EDITOR_VALUES)
    assert (await web_client.get("/environments/999999/history")).status_code == 404
    missing = await post(web_client, "/environments/999999/history/reveal?v=1&v=2")
    assert missing.status_code == 404


async def test_viewer_page_has_no_reveal_or_restore_and_says_values_are_masked(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "viewer")
    page = main_of((await web_client.get(f"/environments/{ids.editor}/history")).text)
    assert ">View</a>" in page and ">History</a>" in page and "Compare to dist" not in page
    assert "Comparing <span" in page and "v2</span> to" in page
    assert "3 unchanged keys hidden. Values are masked for the Viewer group." in page
    for absent in ("Reveal", "Restore", "restore-dialog", "hx-post", "dialog-open"):
        assert absent not in page, absent
    assert page.count('data-fragment="version-row"') == 3


async def test_viewer_cannot_reveal_or_restore(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "viewer")
    base = f"/environments/{ids.editor}"
    for url in (f"{base}/history/reveal?v=2&v=3", f"{base}/restore/1?base=3"):
        response = await post(web_client, url)
        assert response.status_code == 403, url
        assert not any(value in response.text for value in EDITOR_VALUES)
    assert await version_numbers(web_app, ids.editor) == [1, 2, 3]
    assert await web_audit_entries(web_app, "environment.reveal") == []
    assert await web_audit_entries(web_app, "environment.restore") == []


async def test_admin_page_has_the_reveal_and_restore_controls(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    page = main_of((await web_client.get(f"/environments/{ids.editor}/history")).text)
    assert ">Edit</a>" in page and "Compare to dist" in page
    assert "Reveal values (audited)" in page and "Hide values" in page
    assert f'hx-post="/environments/{ids.editor}/history/reveal?v=2&amp;v=3"' in page
    assert "Restore v2 as new version" in page and 'id="restore-dialog"' in page
    assert f'hx-post="/environments/{ids.editor}/restore/2?base=3"' in page
    assert (
        "Creates version 4 with the content of version 2. Version 3 stays in the history. "
        "The restored version is not deployed until you deploy it." in page
    )
    assert "Values are masked for the Viewer group" not in page
    assert "3 unchanged keys hidden" in page
    assert "Deploy v3" in page and "deploy-dialog" in page


async def test_table_lists_versions_newest_first_with_the_deployed_marker(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    page = main_of((await web_client.get(f"/environments/{ids.editor}/history")).text)
    rows = version_rows(page)
    assert re.findall(r'<th scope="row"[^>]*>v(\d+)</th>', page) == ["3", "2", "1"]
    assert "seed version 3" in rows[0] and "root" in rows[0]
    assert re.search(r'<td class="[^"]*font-mono[^"]*">[0-9a-f]{7}</td>', rows[0])
    assert 'data-status="deployed"' not in page
    boxes = re.findall(r'<input type="checkbox" name="v" value="(\d+)"([^>]*)>', page)
    assert [(number, " checked" in attributes) for number, attributes in boxes] == [
        ("3", True),
        ("2", True),
        ("1", False),
    ]
    assert "Version 3 saved, never deployed" in page
    tuning = main_of((await web_client.get(f"/environments/{ids.tuning}/history")).text)
    assert [('data-status="deployed"' in row) for row in version_rows(tuning)] == [False, True]
    assert "Version 2 saved, version 1 deployed" in tuning


async def test_diff_panel_lists_masked_rows_with_empty_slots(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    panel = (await web_client.get(f"/environments/{ids.editor}/diff?v=3&v=1", headers=HTMX)).text
    assert panel.lstrip().startswith('<section data-fragment="diff-panel" id="diff-panel"')
    assert "Comparing <span" in panel
    assert 'v1</span> to <span class="font-mono">v3</span>' in panel
    key_cell = r'font-mono text-\[13px\] font-semibold">(\w+)<'
    keys = re.findall(key_cell, panel)
    assert keys == ["MYSQL_PASSWORD", "MAX_CONNECTIONS", "TLS_MIN_VERSION", "TLS_CA_CERT"]
    assert panel.index("Changed") < panel.index("Added")
    assert panel.count("••••••••") == 5
    for slot in ("diff-left-0", "diff-right-0", "diff-right-1", "diff-right-2", "diff-right-3"):
        assert f'id="{slot}" data-value class="hidden' in panel
    assert 'id="diff-left-1"' not in panel and "not set" in panel
    assert "2 unchanged keys hidden" in panel
    assert 'data-reveal-row data-revealed="false"' in panel


async def test_diff_panel_prompts_unless_exactly_two_versions_are_chosen(
    web_client: httpx.AsyncClient, ids: EnvironmentIds
) -> None:
    await sign_in(web_client, "admin")
    prompt = "Select two versions in the table to compare them."
    for query in ("", "?v=1", "?v=1&v=2&v=3", "?v=1&v=9", "?v=2&v=2", "?v=x"):
        panel = (await web_client.get(f"/environments/{ids.editor}/diff{query}")).text
        assert prompt in panel and "Compare versions" in panel, query
        assert "Reveal values" not in panel and "Restore" not in panel, query
    single = main_of((await web_client.get(f"/environments/{ids.api}/history")).text)
    assert "This file has one version. Save another version to compare." in single
    assert single.count('data-fragment="version-row"') == 1
    assert "Reveal values" not in single and "Restore" not in single


async def test_diff_panel_says_when_nothing_differs(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    await add_version(web_app, ids.tuning, "WORKERS=4\n")
    same = (await web_client.get(f"/environments/{ids.tuning}/diff?v=2&v=3")).text
    assert "These versions have the same content." in same
    assert "Reveal values" not in same
    await add_version(web_app, ids.tuning, "# only a comment\nWORKERS=4\n")
    comment = (await web_client.get(f"/environments/{ids.tuning}/diff?v=3&v=4")).text
    assert "No variables differ." in comment
    assert "only in comments, blank lines or order." in comment
    assert "Reveal values" not in comment and "1 unchanged key hidden" in comment


async def test_admin_reveal_answers_only_values_and_audits_each_side(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.editor}/history/reveal?v=3&v=2")
    assert response.status_code == 200
    assert_never_cached(response)
    partial = r'<template hx type="partial" hx-target="([^"]+)" hx-swap="innerHTML">'
    targets = re.findall(partial, response.text)
    assert targets == ["#diff-left-0", "#diff-right-0", "#diff-right-1", "#diff-right-2"]
    assert response.text.count("<span hx-ignore data-reveal-value>") == 4
    assert ">150</span>" in response.text and ">200</span>" in response.text
    assert "TLSv1.2" in response.text and "sentinelCertBody" in response.text
    for absent in (
        "Ke3rH6vNa5",
        "Zq7vK2mPw9",
        "Tn4xB8cL1d",
        "db.internal.example.com",
        "rotated by hand",
        "MAX_CONNECTIONS",
        "MYSQL_PASSWORD",
        "<section",
    ):
        assert absent not in response.text, absent
    entries = await web_audit_entries(web_app, "environment.reveal")
    assert [entry.details for entry in entries] == [
        {"version": 2, "scope": "diff", "compared_with": 3, "keys": ["MAX_CONNECTIONS"]},
        {
            "version": 3,
            "scope": "diff",
            "compared_with": 2,
            "keys": ["MAX_CONNECTIONS", "TLS_MIN_VERSION", "TLS_CA_CERT"],
        },
    ]
    assert all(
        entry.actor_name == "admin" and entry.environment_id == ids.editor for entry in entries
    )
    assert not any(value in repr(entry.details) for entry in entries for value in EDITOR_VALUES)


async def test_reveal_escapes_values_and_marks_empty_ones(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    await add_version(
        web_app, ids.tuning, "WORKERS=<img src=x onerror=alert(1)>\nEMPTY=\nOTHER=1\n"
    )
    await add_version(web_app, ids.tuning, "WORKERS=4\nEMPTY=full\nOTHER=1\n")
    response = await post(web_client, f"/environments/{ids.tuning}/history/reveal?v=3&v=4")
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text and "<img" not in response.text
    assert "Empty value" in response.text


async def test_certificate_reveal_returns_the_differing_lines_of_both_versions(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    await add_version(
        web_app, ids.ca, "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\n-----END CERTIFICATE-----\n"
    )
    response = await post(web_client, f"/environments/{ids.ca}/history/reveal?v=1&v=2")
    assert "c2VlZA==" in response.text and "bmV3Ym9keQ==" in response.text
    assert "BEGIN CERTIFICATE" not in response.text
    assert response.text.count("<template") == 2
    entries = await web_audit_entries(web_app, "environment.reveal")
    assert [entry.details["scope"] for entry in entries] == ["diff", "diff"]
    assert [entry.details["keys"] for entry in entries] == [["Line 2"], ["Line 2"]]


async def test_whole_file_reveal_returns_both_texts(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await set_web_file_type(web_app, ids.ca, FileType.ENV)
    await sign_in(web_client, "root")
    await add_version(
        web_app, ids.ca, "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\n-----END CERTIFICATE-----\n"
    )
    response = await post(web_client, f"/environments/{ids.ca}/history/reveal?v=1&v=2")
    assert "c2VlZA==" in response.text and "bmV3Ym9keQ==" in response.text
    assert response.text.count("<template") == 2
    entries = await web_audit_entries(web_app, "environment.reveal")
    assert [entry.details["scope"] for entry in entries] == ["file", "file"]


async def test_reveal_refuses_a_bad_selection_without_auditing(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    base = f"/environments/{ids.editor}/history/reveal"
    for query in ("", "?v=1", "?v=2&v=2", "?v=1&v=9", "?v=1&v=2&v=3", "?v=a&v=b"):
        response = await post(web_client, f"{base}{query}")
        assert response.status_code == 404, query
        assert not any(value in response.text for value in EDITOR_VALUES)
    assert await web_audit_entries(web_app, "environment.reveal") == []


async def test_environment_scoped_admin_reveals_and_restores_its_own_file(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "env_scoped_admin")
    base = f"/environments/{ids.db}"
    page = main_of((await web_client.get(f"{base}/history")).text)
    assert "3306" not in page and "hx-ignore" not in page
    assert "Reveal values (audited)" in page and "Restore v1 as new version" in page
    revealed = await post(web_client, f"{base}/history/reveal?v=1&v=2")
    assert revealed.status_code == 200 and "3306" in revealed.text
    assert (await post(web_client, f"{base}/restore/1?base=2")).status_code == 200
    assert await version_numbers(web_app, ids.db) == [1, 2, 3]


async def test_environment_scoped_viewer_sees_the_history_of_its_file_masked(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "env_scoped_viewer")
    base = f"/environments/{ids.db}"
    response = await web_client.get(f"{base}/history")
    assert_never_cached(response)
    page = main_of(response.text)
    assert "Comparing <span" in page and "Values are masked for the Viewer group." in page
    for absent in ("Reveal values", "Restore", "hx-post", "3306"):
        assert absent not in page, absent
    assert (await post(web_client, f"{base}/history/reveal?v=1&v=2")).status_code == 403
    assert (await post(web_client, f"{base}/restore/1?base=2")).status_code == 403
    assert await version_numbers(web_app, ids.db) == [1, 2]


async def test_admin_restore_saves_a_new_version_and_redirects_with_a_toast(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.editor}/restore/1?base=3")
    assert response.status_code == 200
    assert_never_cached(response)
    assert response.headers["hx-redirect"] == (
        f"/environments/{ids.editor}/history?restored=4&source=1"
    )
    assert await version_numbers(web_app, ids.editor) == [1, 2, 3, 4]
    page = main_of((await web_client.get(response.headers["hx-redirect"])).text)
    assert "Restored version 1 as version 4. Pending deploy." in page
    assert "Restored from version 1" in page and "Version 4 saved" in page
    assert page.count('data-fragment="version-row"') == 4
    entries = await web_audit_entries(web_app, "environment.restore")
    assert [entry.details for entry in entries] == [{"number": 4, "restored": 1}]
    assert entries[0].actor_name == "admin" and len(entries[0].version_ids) == 2
    assert not any(value in repr(entries[0].details) for value in EDITOR_VALUES)
    ignored = main_of(
        (await web_client.get(f"/environments/{ids.editor}/history?restored=<b>&source=1")).text
    )
    assert "Restored version" not in ignored


async def test_restore_refusals_swap_the_dialog_form_and_change_nothing(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    base = f"/environments/{ids.editor}/restore"
    stale = await post(web_client, f"{base}/1?base=2")
    assert stale.status_code == 422
    assert 'id="restore-form"' in stale.text and 'role="alert"' in stale.text
    assert "This file changed since you opened it. Reload the page to continue." in stale.text
    assert not any(value in stale.text for value in EDITOR_VALUES)
    for query in ("", "?base=x", "?base=0"):
        assert (await post(web_client, f"{base}/1{query}")).status_code == 422, query
    assert (await post(web_client, f"{base}/9?base=3")).status_code == 404
    assert await version_numbers(web_app, ids.editor) == [1, 2, 3]
    assert (await post(web_client, f"{base}/1?base=3")).status_code == 200
    same = await post(web_client, f"{base}/1?base=4")
    assert same.status_code == 422
    assert "Version 1 has the same content as the current version. Nothing to restore." in same.text
    assert await version_numbers(web_app, ids.editor) == [1, 2, 3, 4]


async def test_restore_needs_the_csrf_token(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response = await web_client.post(f"/environments/{ids.editor}/restore/1?base=3", headers=HTMX)
    assert response.status_code == 403
    assert await version_numbers(web_app, ids.editor) == [1, 2, 3]


async def test_restoring_a_version_announces_the_file_change_to_the_notifier(
    web_client: httpx.AsyncClient, ids: EnvironmentIds, web_app: FastAPI
) -> None:
    """The restore route hands its notifier to the service."""
    recorder = RecordingEvents()
    web_app.state.notifier = recorder
    await sign_in(web_client, "admin")
    response = await post(web_client, f"/environments/{ids.db}/restore/1?base=2")
    assert response.status_code == 200
    project, deployment, service, name = await file_event_ids(web_app, ids.db)
    assert recorder.events == [
        FileChanged(project, deployment, service, ids.db, name, 3, FileChange.RESTORED, "admin")
    ]
    stale = await post(web_client, f"/environments/{ids.db}/restore/1?base=2")
    assert stale.status_code == 422 and len(recorder.events) == 1
