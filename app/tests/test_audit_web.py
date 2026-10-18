"""Audit log pages and fragments on the seeded application, by role and scope."""

import csv
import io
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import AuditActorType, AuditEntry, AuditResult, Project
from app.seeds import BULK_ACTION, EDITOR_SECRET_VALUES, ensure_audit_figures
from app.services.audit import record_audit
from app.services.audit_log import PAGE_SIZE
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, EnvironmentIds, assert_never_cached, main_of, post

AUDIT_PATHS = ("/audit", "/audit/table", "/audit/rows", "/audit/export.csv", "/audit?entry=1")
ROW = re.compile(r'<tr data-fragment="audit-row" id="audit-entry-(\d+)">(.*?)</tr>', re.DOTALL)


@pytest.fixture
async def figures(web_app: FastAPI) -> None:
    """The audit figures on top of the seeded application."""
    async with web_app.state.session_factory() as db:
        await ensure_audit_figures(db)
        await db.commit()


async def entry_id(web_app: FastAPI, action: str) -> int:
    """The id of the one entry with an action."""
    async with web_app.state.session_factory() as db:
        found: int = (
            await db.execute(select(AuditEntry.id).where(AuditEntry.action == action))
        ).scalar_one()
    return found


async def project_id(web_app: FastAPI, name: str) -> int:
    """The id of a seeded project."""
    async with web_app.state.session_factory() as db:
        found: int = (await db.execute(select(Project.id).where(Project.name == name))).scalar_one()
    return found


def row_ids(page: str) -> list[int]:
    """Entry ids of the table rows of a page or fragment, in order."""
    return [int(found.group(1)) for found in ROW.finditer(page)]


def rows_with(page: str, text: str) -> int:
    """How many table rows of a page or fragment contain the text."""
    return sum(1 for found in ROW.finditer(page) if text in found.group(2))


def row_cells(page: str) -> list[int]:
    """How many cells each table row has."""
    return [found.group(2).count("<td") for found in ROW.finditer(page)]


async def get_fragment(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """A GET the way htmx sends it."""
    return await http.get(url, headers=HTMX)


@pytest.mark.parametrize("user", ["viewer", "env_scoped_viewer"])
@pytest.mark.usefixtures("figures")
async def test_viewers_are_refused_every_audit_route(
    web_client: httpx.AsyncClient, web_app: FastAPI, user: str
) -> None:
    """No page, fragment, entry or export answers a viewer, and nothing is recorded."""
    await sign_in(web_client, user)
    for path in AUDIT_PATHS:
        assert (await web_client.get(path)).status_code == 403, path
        assert (await get_fragment(web_client, path)).status_code == 403, path
    async with web_app.state.session_factory() as db:
        exports = (
            await db.execute(select(func.count()).where(AuditEntry.action == "audit.export"))
        ).scalar_one()
    assert exports == 0


async def test_a_signed_out_visitor_is_sent_to_sign_in(web_client: httpx.AsyncClient) -> None:
    """Every audit route needs a session."""
    for path in AUDIT_PATHS:
        response = await web_client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login", path


@pytest.mark.usefixtures("figures")
async def test_the_superadmin_page_has_the_filters_the_table_and_a_second_page(
    web_client: httpx.AsyncClient,
) -> None:
    """All entries, newest first, with every column, the filters and a load more row."""
    await sign_in(web_client, "root")
    response = await web_client.get("/audit")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    body = main_of(page)
    assert "All entries." in body and "granted scope" not in body
    for field in ("f-p", "f-u", "f-a", "f-r", "f-d1", "f-d2"):
        assert f'id="{field}"' in body
    for column in ("Time", "Actor", "Source IP", "Action", "Object", "Result"):
        assert f">{column}</th>" in body
    assert 'class="sr-only">Details</span>' in body
    assert 'data-fragment="audit-table"' in body and 'data-fragment="audit-results"' in body
    ids = row_ids(body)
    assert len(ids) == PAGE_SIZE
    times = re.findall(r'<time datetime="([^"]+)"', body)
    assert times == sorted(times, reverse=True)
    assert set(row_cells(body)) == {7}
    assert 'data-fragment="audit-more"' in body and "/audit/rows?before=" in body
    assert 'href="/audit/export.csv"' in body and "download" in body
    assert 'aria-label="Show details"' in body
    assert 'hx-get="/audit/table"' in body and 'hx-trigger="change, submit"' in body


@pytest.mark.usefixtures("figures")
async def test_the_admin_page_is_limited_to_the_scope_and_says_so(
    web_client: httpx.AsyncClient,
) -> None:
    """An admin sees its items' entries only, with the scope note, and no hidden names."""
    await sign_in(web_client, "admin")
    page = (await web_client.get("/audit")).text
    body = main_of(page)
    assert "Entries for the projects, deployments and environments granted to you." in body
    assert "Showing entries within your granted scope only." in body
    ids = row_ids(body)
    assert len(ids) == 11
    assert rows_with(body, "login.success") == 1
    hidden_texts = (BULK_ACTION, "new.hire", "retired", "198.51.100.7", "user.create")
    for hidden in hidden_texts:
        assert hidden not in body, hidden
    assert 'data-fragment="audit-more"' not in body
    options = re.findall(r'<option value="([^"]*)"', body)
    assert "user:admin" in options and "api:token billing-ci" in options
    assert "cli:cli" not in options and "system:system" not in options
    assert BULK_ACTION not in options


@pytest.mark.usefixtures("figures")
async def test_a_scoped_admin_sees_its_file_and_only_its_parent_name(
    web_client: httpx.AsyncClient,
) -> None:
    """An environment grant lists the entries of that file and offers only its project."""
    await sign_in(web_client, "env_scoped_admin")
    body = main_of((await web_client.get("/audit")).text)
    assert len(row_ids(body)) == 3
    assert "environment.save" in body and "file_save" in body
    assert rows_with(body, "login.success") == 1
    assert ">rentals</option>" in body
    for hidden in ("billing", ".env.api", ".env.db.secrets", "service.deploy", "retired"):
        assert hidden not in body, hidden


@pytest.mark.usefixtures("figures")
async def test_a_user_without_grants_sees_only_their_own_entries(
    web_client: httpx.AsyncClient,
) -> None:
    """Before the user does anything the log holds only their own sign-in, naming nothing else."""
    await sign_in(web_client, "ungranted_admin")
    body = main_of((await web_client.get("/audit")).text)
    assert len(row_ids(body)) == 1 and rows_with(body, "login.success") == 1
    assert "No entries yet" not in body
    for hidden in ("rentals", "billing", "new.hire", BULK_ACTION):
        assert hidden not in body, hidden
    assert 'data-fragment="skeleton"' in body and 'aria-busy="true"' in body


async def test_the_empty_state_shows_when_nothing_matches_the_filter(
    web_client: httpx.AsyncClient,
) -> None:
    """An admin with no entries in scope for a filter reads an empty result, naming nothing."""
    await sign_in(web_client, "ungranted_admin")
    body = main_of((await web_client.get("/audit?action=service.deploy")).text)
    assert "No entries match" in body and row_ids(body) == []


@pytest.mark.usefixtures("figures")
async def test_filters_apply_on_the_server_and_are_kept_in_the_address(
    web_client: httpx.AsyncClient,
) -> None:
    """A full page and the fragment answer the same rows; the fragment pushes the page URL."""
    await sign_in(web_client, "root")
    page = main_of((await web_client.get("/audit?result=halted&from=2026-10-16")).text)
    assert len(row_ids(page)) == 1
    assert '<option value="halted" selected>halted</option>' in page
    assert 'value="2026-10-16"' in page
    assert 'href="/audit/export.csv?result=halted&amp;from=2026-10-16"' in page
    url = "/audit/table?project=&actor=&action=&result=halted&from=2026-10-16&to="
    fragment = await get_fragment(web_client, url)
    assert fragment.status_code == 200
    assert_never_cached(fragment)
    assert fragment.headers["HX-Push-Url"] == "/audit?result=halted&from=2026-10-16"
    assert row_ids(fragment.text) == row_ids(page)
    assert 'id="audit-results"' in fragment.text and "<nav" not in fragment.text
    assert (
        '<template hx type="partial" hx-target="#audit-export" hx-swap="outerHTML">'
        in fragment.text
    )
    assert 'href="/audit/export.csv?result=halted&amp;from=2026-10-16"' in fragment.text
    cleared = await get_fragment(web_client, "/audit/table?project=&result=")
    assert cleared.headers["HX-Push-Url"] == "/audit"
    assert 'href="/audit/export.csv"' in cleared.text


@pytest.mark.usefixtures("figures")
async def test_filters_combine(web_client: httpx.AsyncClient, web_app: FastAPI) -> None:
    """Project, user or token, action, result and dates narrow together."""
    await sign_in(web_client, "root")
    rentals = await project_id(web_app, "rentals")
    base = f"/audit/table?project={rentals}"
    assert len(row_ids((await get_fragment(web_client, base)).text)) == 7
    by_user = await get_fragment(web_client, f"{base}&actor=user:admin")
    assert len(row_ids(by_user.text)) == 2
    by_action = await get_fragment(web_client, f"{base}&action=file_save&result=success")
    assert len(row_ids(by_action.text)) == 1
    by_dates = await get_fragment(web_client, f"{base}&from=2026-10-16&to=2026-10-16")
    assert len(row_ids(by_dates.text)) == 2
    none = await get_fragment(web_client, f"{base}&result=halted")
    assert row_ids(none.text) == []
    assert "No entries match" in none.text and "Change the filters or the date range." in none.text


@pytest.mark.usefixtures("figures")
async def test_bad_filter_values_are_ignored_and_never_answer_an_error(
    web_client: httpx.AsyncClient,
) -> None:
    """A malformed value means no filter, so a fragment never carries an error body."""
    await sign_in(web_client, "root")
    junk = "project=abc&actor=x&action=NOT%20OK&result=zzz&from=2026-99-99&to=nope&before=junk"
    for path in ("/audit", "/audit/table", "/audit/rows"):
        response = await get_fragment(web_client, f"{path}?{junk}")
        assert response.status_code == 200, path
    fragment = await get_fragment(web_client, f"/audit/table?{junk}")
    assert fragment.headers["HX-Push-Url"] == "/audit"
    assert len(row_ids(fragment.text)) == PAGE_SIZE


@pytest.mark.usefixtures("figures")
async def test_a_hidden_project_filter_answers_like_a_project_with_no_entries(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Filtering by a project outside the scope is indistinguishable from an empty project."""
    async with web_app.state.session_factory() as db:
        empty = Project(name="no-entries")
        db.add(empty)
        await db.commit()
    billing = await project_id(web_app, "billing")
    await sign_in(web_client, "env_scoped_admin")
    hidden = await get_fragment(web_client, f"/audit/table?project={billing}")
    nothing = await get_fragment(web_client, f"/audit/table?project={empty.id}")
    missing = await get_fragment(web_client, "/audit/table?project=987654")
    assert row_ids(hidden.text) == [] == row_ids(nothing.text) == row_ids(missing.text)

    def anonymous(response: httpx.Response) -> str:
        return re.sub(r"project=\d+", "project=N", response.text)

    assert anonymous(hidden) == anonymous(nothing) == anonymous(missing)
    assert "billing" not in hidden.text


@pytest.mark.usefixtures("figures")
async def test_load_more_walks_every_entry_once_with_whole_rows(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Following the load more rows ends at the last entry, each row has its seven cells."""
    await sign_in(web_client, "root")
    page = main_of((await web_client.get("/audit")).text)
    seen = row_ids(page)
    href = re.search(r'hx-get="(/audit/rows\?[^"]*)"', page)
    steps = 0
    while href is not None:
        more = await get_fragment(web_client, href.group(1).replace("&amp;", "&"))
        assert more.status_code == 200
        assert_never_cached(more)
        assert set(row_cells(more.text)) == {7}
        assert "<nav" not in more.text and 'id="audit-results"' not in more.text
        seen.extend(row_ids(more.text))
        href = re.search(r'hx-get="(/audit/rows\?[^"]*)"', more.text)
        steps += 1
    assert steps >= 1
    async with web_app.state.session_factory() as db:
        total = (await db.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
    assert len(seen) == total == len(set(seen))


@pytest.mark.usefixtures("figures")
async def test_the_load_more_link_keeps_the_filters(web_client: httpx.AsyncClient) -> None:
    """The next page of a filtered view stays inside the filter."""
    await sign_in(web_client, "root")
    page = main_of((await web_client.get(f"/audit?action={BULK_ACTION}")).text)
    assert len(row_ids(page)) == PAGE_SIZE
    href = re.search(r'hx-get="(/audit/rows\?[^"]*)"', page)
    assert href is not None and f"action={BULK_ACTION}" in href.group(1)
    more = await get_fragment(web_client, href.group(1).replace("&amp;", "&"))
    assert len(row_ids(more.text)) == 5 and "audit-more" not in more.text


@pytest.mark.usefixtures("figures")
async def test_one_entry_is_shown_expanded_with_its_details_and_versions(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The entry page opens the row, lists details and version names and offers the full log."""
    await sign_in(web_client, "root")
    saved = await entry_id(web_app, "environment.save")
    response = await web_client.get(f"/audit?entry={saved}")
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert row_ids(body) == [saved]
    assert 'aria-expanded="true"' in body
    assert f'<tr id="audit-detail-{saved}">' in body
    assert "changed keys" in body and "DB_PORT" in body
    assert "Version" in body and ".env.db v2" in body
    assert f"Entry #{saved}" in body and 'href="/audit"' in body
    assert 'id="audit-filters"' not in body
    assert "10.0.4.31" in body and "admin" in body


@pytest.mark.usefixtures("figures")
async def test_an_entry_that_is_hidden_missing_or_malformed_gets_the_same_answer(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A scoped admin cannot tell a hidden entry from one that does not exist."""
    hidden = await entry_id(web_app, "service.deploy")
    cli = await entry_id(web_app, "user.create")
    visible = await entry_id(web_app, "environment.save")
    await sign_in(web_client, "env_scoped_admin")
    answers = {
        ref: await web_client.get(f"/audit?entry={ref}")
        for ref in (hidden, cli, 987654, "abc", "", "-1", "1.5", "0")
    }
    for ref, response in answers.items():
        assert response.status_code == 404, ref
    assert len({main_of(response.text) for response in answers.values()}) == 1
    body = main_of(answers[hidden].text)
    assert "Entry not found" in body and "audit-detail" not in body
    assert (await web_client.get(f"/audit?entry={visible}")).status_code == 200


@pytest.mark.usefixtures("figures")
async def test_names_and_details_are_escaped_and_the_details_sit_inside_hx_ignore(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Markup in an actor, object or detail is text, and no secret-looking value is printed."""
    async with web_app.state.session_factory() as db:
        entry = await record_audit(
            db,
            action="xss.test",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.USER,
            actor_name="<b>bold</b>",
            target_name="<img src=x onerror=alert(1)>",
            details={
                "note": "<script>alert(1)</script>",
                "value": "hunter2",
                "inner": {"password": "s3cr3t-value"},
                "post_deploy_output": "line one\n<svg onload=alert(2)>",
            },
        )
        await db.commit()
    await sign_in(web_client, "root")
    page = (await get_fragment(web_client, "/audit/table?action=xss.test")).text
    for raw in ("<b>bold</b>", "<img src=x", "<script>alert(1)</script>", "<svg onload"):
        assert raw not in page, raw
    escaped = ("&lt;b&gt;bold&lt;/b&gt;", "&lt;img src=x", "&lt;script&gt;alert(1)")
    for text in escaped:
        assert text in page, text
    assert "hunter2" not in page and "s3cr3t-value" not in page and "value:</span> hidden" in page
    detail = re.search(rf'<tr id="audit-detail-{entry.id}".*?</tr>', page, re.DOTALL)
    assert detail is not None and "<div hx-ignore " in detail.group(0)
    assert detail.group(0).index("<div hx-ignore ") < detail.group(0).index("&lt;script")
    assert "<pre " in detail.group(0)


async def test_nothing_secret_from_the_editor_reaches_the_page_or_the_file(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: EnvironmentIds
) -> None:
    """After a reveal and a save, neither the log page nor the export holds a value."""
    await sign_in(web_client, "root")
    revealed = await post(web_client, f"/environments/{ids.editor}/reveal/MYSQL_PASSWORD?version=3")
    assert "Ke3rH6vNa5" in revealed.text
    new_value = "Leak-Check-7q1x"
    saved = await post(
        web_client,
        f"/environments/{ids.editor}/versions",
        {"mode": "raw", "base_version": "3", "content": f"MYSQL_PASSWORD={new_value}\n"},
    )
    assert saved.status_code == 200
    values = (*EDITOR_SECRET_VALUES, new_value)
    page = (await web_client.get("/audit")).text
    assert "environment.reveal" in page and "environment.save" in page
    assert not any(value in page for value in values)
    exported = await web_client.get("/audit/export.csv")
    assert "environment.reveal" in exported.text and "environment.save" in exported.text
    assert not any(value in exported.text for value in values)
    async with web_app.state.session_factory() as db:
        details = (await db.execute(select(AuditEntry.details))).scalars().all()
    assert not any(value in repr(details) for value in values)


@pytest.mark.usefixtures("figures")
async def test_the_export_is_a_csv_download_of_the_current_filter_and_is_audited(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Headers, content of a filtered export, and the audit entry that records it."""
    await sign_in(web_client, "root")
    response = await web_client.get("/audit/export.csv?result=halted&bogus=1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(r'attachment; filename="audit-log-\d{8}-\d{6}\.csv"', disposition)
    assert response.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:6] == ["id", "time_utc", "actor", "actor_type", "source_ip", "action"]
    assert [(row[2], row[3], row[5], row[8]) for row in rows[1:]] == [
        ("billing-ci", "token", "service.deploy", "halted")
    ]
    async with web_app.state.session_factory() as db:
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "audit.export"))
        ).scalar_one()
    assert entry.actor_name == "root"
    assert entry.details == {"filters": {"result": "halted"}, "rows": 1}


@pytest.mark.usefixtures("figures")
async def test_the_export_covers_exactly_the_recorded_rows_and_the_callers_scope(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The file holds as many entries as the audit entry says, not counting itself."""
    await sign_in(web_client, "root")
    everything = await web_client.get("/audit/export.csv")
    rows = list(csv.reader(io.StringIO(everything.text)))[1:]
    async with web_app.state.session_factory() as db:
        recorded = (
            await db.execute(select(AuditEntry.details).where(AuditEntry.action == "audit.export"))
        ).scalar_one()
    assert recorded["rows"] == len(rows)
    assert "audit.export" not in {row[5] for row in rows}
    web_client.cookies.clear()
    await sign_in(web_client, "admin")
    admin_rows = list(csv.reader(io.StringIO((await web_client.get("/audit/export.csv")).text)))[1:]
    assert len(admin_rows) == 11
    assert not {BULK_ACTION, "user.create", "login.failure", "audit.export"} & {
        row[5] for row in admin_rows
    }
    assert [row[5] for row in admin_rows].count("login.success") == 1


async def test_an_export_of_an_empty_view_holds_only_the_header(
    web_client: httpx.AsyncClient,
) -> None:
    """No entries matching the filter means a file with the column names and nothing else."""
    await sign_in(web_client, "ungranted_admin")
    response = await web_client.get("/audit/export.csv?action=service.deploy")
    assert response.status_code == 200
    assert len(list(csv.reader(io.StringIO(response.text)))) == 1


@pytest.mark.usefixtures("figures")
async def test_fragments_are_not_cached_and_vary_on_htmx(web_client: httpx.AsyncClient) -> None:
    """The same URL answers a page or a fragment, so every answer varies and is not stored."""
    await sign_in(web_client, "root")
    for path in ("/audit/table", f"/audit/rows?before={'1-1'}", "/audit?entry=1"):
        assert_never_cached(await get_fragment(web_client, path))
