"""The access grid page, table and toggle routes on the seeded application, by role."""

import html
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update

from app.models import (
    AccessGrant,
    ApiTokenRestriction,
    AuditEntry,
    Deployment,
    Environment,
    Project,
    Service,
    User,
    UserGroup,
)
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, assert_never_cached, main_of, post, without_csrf_token

NOT_SUPERADMIN = (
    "admin",
    "viewer",
    "project_admin",
    "deployment_admin",
    "env_scoped_admin",
    "env_scoped_viewer",
    "ungranted_admin",
)
ROW = re.compile(r'(<tr data-fragment="grid-row" data-node="([^"]+)"[^>]*>.*?</tr>)', re.DOTALL)
COLUMN = re.compile(r'<th scope="col" id="grid-user-(\d+)"[^>]*>([^<]+)<')
SEEDED_GRANTS = 8


async def user_id(web_app: FastAPI, name: str) -> int:
    """The id of a seeded user."""
    async with web_app.state.session_factory() as db:
        found = await db.scalar(select(User.id).where(User.name == name))
    assert found is not None
    return int(found)


async def ids(web_app: FastAPI) -> dict[str, int]:
    """Ids of seeded items to toggle."""
    async with web_app.state.session_factory() as db:
        found = {
            "billing": await db.scalar(select(Project.id).where(Project.name == "billing")),
            "Fancy Proj. Name": await db.scalar(
                select(Project.id).where(Project.name == "Fancy Proj. Name")
            ),
            "api": await db.scalar(select(Environment.id).where(Environment.name == ".env.api")),
            "db": await db.scalar(select(Environment.id).where(Environment.name == ".env.db")),
            "main": await db.scalar(select(Deployment.id).where(Deployment.name == "main")),
            "api_service": await db.scalar(select(Service.id).where(Service.name == "api")),
        }
    assert all(found.values()), found
    return {key: value for key, value in found.items() if value is not None}


async def grant_rows(web_app: FastAPI) -> list[tuple[int, str, int | None, int | None, int | None]]:
    """Every stored grant as user, level and item ids, in a stable order."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(
                AccessGrant.user_id,
                AccessGrant.level,
                AccessGrant.project_id,
                AccessGrant.deployment_id,
                AccessGrant.environment_id,
            ).order_by(AccessGrant.id)
        )
        return [(uid, level.value, p, d, e) for uid, level, p, d, e in rows.all()]


async def audit_count(web_app: FastAPI, *actions: str) -> int:
    """How many audit entries exist for the actions."""
    async with web_app.state.session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(AuditEntry).where(AuditEntry.action.in_(actions))
        )
    return count or 0


def rows_of(page: str) -> dict[str, str]:
    """The markup of every grid row by its node id."""
    return {node: markup for markup, node in ROW.findall(page)}


def vals_of(page: str, uid: int, node: str) -> dict[str, str]:
    """The values the checkbox of a cell posts, as the browser would send them."""
    found = re.search(rf'id="grant-{uid}-{node}"[^>]*?hx-vals="([^"]*)"', page)
    assert found, (uid, node)
    values: dict[str, str] = json.loads(html.unescape(found.group(1)))
    return values


@asynccontextmanager
async def second_client(web_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser, so one test can hold the superadmin and the user they change."""
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Accept": "text/html"}
    ) as http:
        yield http


@pytest.mark.parametrize("name", NOT_SUPERADMIN)
async def test_nobody_but_the_superadmin_reaches_the_grid_or_changes_a_grant(
    web_client: httpx.AsyncClient, web_app: FastAPI, name: str
) -> None:
    await sign_in(web_client, name)
    for url in (
        "/access/grid",
        "/access/grid/table",
        "/access/grid?group=viewer",
        "/access/grid/project/1",
    ):
        for headers in ({}, HTMX):
            response = await web_client.get(url, headers=headers)
            assert response.status_code == 403, (name, url)
            assert "Access grid" not in response.text
    before = await grant_rows(web_app)
    entries = await audit_count(web_app, "grant.add", "grant.remove")
    item = await ids(web_app)
    victim = await user_id(web_app, "ungranted_admin")
    for target in (f"project:{item['billing']}", f"environment:{item['db']}"):
        for was in ("0", "1"):
            data = {"user": victim, "target": target, "was": was}
            assert (await post(web_client, "/access/grid/toggle", data)).status_code == 403
    assert await grant_rows(web_app) == before
    assert await audit_count(web_app, "grant.add", "grant.remove") == entries


async def test_the_page_lists_the_tree_by_users_with_a_control_only_where_a_grant_can_be_set(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    page = await web_client.get("/access/grid")
    assert page.status_code == 200
    assert_never_cached(page)
    body = main_of(page.text)
    assert '<h1 class="text-[26px] font-bold leading-tight md:text-3xl">Access grid</h1>' in body
    assert "Rows are projects, deployments, services and environments." in body
    assert 'href="/access/grid" aria-current="page"' in body
    assert 'href="/users" class=' in body and 'href="/users" aria-current' not in body
    names = [name for _, name in COLUMN.findall(body)]
    assert names == sorted(names, key=str.lower)
    assert {"admin", "viewer", "ungranted_admin"} <= set(names)
    assert "root" not in names
    assert "Superadmins reach everything, so they have no column." in body
    for label in ("Direct grant", "Inherited, not editable", "Service result"):
        assert f"{label}</li>" in body
    rows = rows_of(body)
    assert f"project-{item['billing']}" in rows and f"environment-{item['api']}" in rows
    for node, markup in rows.items():
        boxes = markup.count('type="checkbox"')
        if node.startswith("service-"):
            assert boxes == 0, node
            assert "Sees " in markup or "No access" in markup
        else:
            assert boxes + markup.count('data-cell="inherited"') == len(names), node
    admin = await user_id(web_app, "admin")
    assert f'id="grant-{admin}-project-{item["billing"]}" checked' in body
    project_row = rows[f"project-{item['billing']}"]
    assert "+ future items" in project_row
    environment_row = rows[f"environment-{item['api']}"]
    assert "from billing" in environment_row and "+ future items" not in environment_row
    assert 'data-parent="service-' in environment_row
    assert f'data-parent="project-{item["billing"]}"' in rows[f"deployment-{item['main']}"]
    assert "data-parent" not in project_row.split(">", 1)[0]
    assert 'hx-trigger="grants-changed' not in body
    assert body.count('data-js="tree-toggle"') >= 3


async def test_only_the_first_project_has_rows_and_the_others_load_them_when_expanded(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    fancy = item["Fancy Proj. Name"]
    body = main_of((await web_client.get("/access/grid")).text)
    rows = rows_of(body)
    assert f"deployment-{item['main']}" in rows
    assert f"environment-{item['db']}" not in rows
    assert [node for node in rows if node.startswith("project-")] == [
        f"project-{item['billing']}",
        f"project-{fancy}",
    ]
    assert body.count('<tbody id="grid-project-') == 2
    closed = rows[f"project-{fancy}"]
    assert 'aria-expanded="false"' in closed and 'data-js="tree-toggle"' not in closed
    assert f'hx-get="/access/grid/project/{fancy}"' in closed
    assert 'hx-target="closest tbody" hx-swap="outerHTML"' in closed
    filtered = main_of((await web_client.get(f"/access/grid?group=viewer&project={fancy}")).text)
    assert f"environment-{item['db']}" in rows_of(filtered)
    assert 'hx-get="/access/grid/project/' not in filtered
    grouped = main_of((await web_client.get("/access/grid?group=viewer")).text)
    assert f'hx-get="/access/grid/project/{fancy}?group=viewer"' in grouped


async def test_a_project_answers_its_rows_in_its_own_tbody_under_the_group_columns(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    fancy = item["Fancy Proj. Name"]
    response = await web_client.get(f"/access/grid/project/{fancy}?group=viewer", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    assert response.text.lstrip().startswith(
        f'<tbody id="grid-project-{fancy}" data-fragment="grid-project">'
    )
    assert "<html" not in response.text and "<table" not in response.text
    rows = rows_of(response.text)
    assert next(iter(rows)) == f"project-{fancy}"
    assert f"environment-{item['db']}" in rows
    viewer = await user_id(web_app, "viewer")
    admin = await user_id(web_app, "admin")
    assert f'id="grant-{viewer}-project-{fancy}" checked' in response.text
    assert f'id="cell-{admin}-' not in response.text
    opened = rows[f"project-{fancy}"]
    assert 'aria-expanded="true"' in opened and 'data-js="tree-toggle"' in opened
    assert "hx-get" not in opened
    assert 'data-fragment="grid-row"' in response.text


@pytest.mark.parametrize("project", ["999999", "0", "9" * 16, "abc"])
async def test_the_rows_of_a_project_that_does_not_exist_or_is_not_an_id_are_refused(
    web_client: httpx.AsyncClient, project: str
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/access/grid/project/{project}", headers=HTMX)
    assert response.status_code in (404, 422)
    assert "<tbody" not in response.text


async def test_the_page_does_not_grow_with_the_rows_of_a_collapsed_project(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Files added below a project that is not open leave the page as it was."""
    await sign_in(web_client, "root")
    item = await ids(web_app)
    fancy = item["Fancy Proj. Name"]
    before = without_csrf_token((await web_client.get("/access/grid")).text)
    async with web_app.state.session_factory() as db:
        service = await db.scalar(
            select(Service.id).join(Deployment).where(Deployment.project_id == fancy).limit(1)
        )
        db.add_all(
            Environment(service_id=service, name=f".env.extra{number}", target_path="/x")
            for number in range(40)
        )
        await db.commit()
    after = without_csrf_token((await web_client.get("/access/grid")).text)
    assert len(after) == len(before)
    rows = await web_client.get(f"/access/grid/project/{fancy}", headers=HTMX)
    assert rows.text.count(".env.extra") >= 40


async def test_the_table_fragment_follows_the_filters_and_pushes_the_page_address(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    response = await web_client.get(
        f"/access/grid/table?group=viewer&project={item['billing']}", headers=HTMX
    )
    assert response.status_code == 200
    assert_never_cached(response)
    assert response.headers["HX-Push-Url"] == f"/access/grid?group=viewer&project={item['billing']}"
    assert response.text.lstrip().startswith('<div id="grid-table" data-fragment="grid-table">')
    assert "<html" not in response.text
    names = [name for _, name in COLUMN.findall(response.text)]
    assert "viewer" in names and "admin" not in names
    assert f"project-{item['billing']}" in rows_of(response.text)
    assert f"project-{item['Fancy Proj. Name']}" not in rows_of(response.text)
    plain = await web_client.get("/access/grid/table?group=bogus&project=zz", headers=HTMX)
    assert plain.headers["HX-Push-Url"] == "/access/grid"
    assert "admin" in [name for _, name in COLUMN.findall(plain.text)]
    full = await web_client.get(f"/access/grid?group=admin&project={item['Fancy Proj. Name']}")
    assert (
        f'<option value="{item["Fancy Proj. Name"]}" selected>Fancy Proj. Name</option>'
        in full.text
    )
    assert '<option value="admin" selected>Admin</option>' in full.text
    assert set(rows_of(full.text)) >= {f"project-{item['Fancy Proj. Name']}"}
    assert f"project-{item['billing']}" not in rows_of(full.text)


async def test_a_toggle_sets_the_grant_audits_it_and_answers_the_cells_that_changed(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    victim = await user_id(web_app, "ungranted_admin")
    node = f"project-{item['billing']}"
    page = (await web_client.get("/access/grid")).text
    values = vals_of(page, victim, node)
    assert values == {"user": victim, "target": f"project:{item['billing']}", "was": "0"}
    response = await post(web_client, "/access/grid/toggle", values)
    assert response.status_code == 200
    assert_never_cached(response)
    assert json.loads(response.headers["HX-Trigger"]) == {"grants-changed": True}
    assert f'id="cell-{victim}-{node}"' in response.text
    assert f'id="grant-{victim}-{node}" checked' in response.text
    assert f'id="cell-{victim}-environment-{item["api"]}"' in response.text
    assert "from billing" in response.text
    assert response.text.count('hx-swap-oob="true"') == response.text.count("<td ")
    assert f'id="cell-{victim}-project-{item["Fancy Proj. Name"]}"' not in response.text
    rows = await grant_rows(web_app)
    assert (victim, "project", item["billing"], None, None) in rows
    assert await audit_count(web_app, "grant.add") == 1
    again = await post(web_client, "/access/grid/toggle", {**values, "was": "1"})
    assert again.status_code == 200
    assert json.loads(again.headers["HX-Trigger"]) == {"grants-changed": True}
    assert (victim, "project", item["billing"], None, None) not in await grant_rows(web_app)
    assert await audit_count(web_app, "grant.add") == 1
    assert await audit_count(web_app, "grant.remove") == 1
    assert len(await grant_rows(web_app)) == SEEDED_GRANTS


async def test_a_toggle_applies_at_once_to_the_user_it_changes(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    victim = await user_id(web_app, "ungranted_admin")
    values = {"user": victim, "target": f"project:{item['billing']}", "was": "0"}
    async with second_client(web_app) as other:
        await sign_in(other, "ungranted_admin")
        assert "billing" not in (await other.get("/projects")).text
        assert (await post(web_client, "/access/grid/toggle", values)).status_code == 200
        assert "billing" in (await other.get("/projects")).text
        assert (await post(web_client, "/access/grid/toggle", {**values, "was": "1"})).status_code
        assert "billing" not in (await other.get("/projects")).text


async def test_a_cell_from_an_old_page_is_refused_with_the_current_column(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    viewer = await user_id(web_app, "viewer")
    before = await grant_rows(web_app)
    entries = await audit_count(web_app, "grant.add", "grant.remove")
    values = {"user": viewer, "target": f"project:{item['Fancy Proj. Name']}", "was": "0"}
    response = await post(web_client, "/access/grid/toggle", values)
    assert response.status_code == 409
    assert "HX-Trigger" not in response.headers
    assert_never_cached(response)
    assert "changed since the page was loaded" in response.text
    assert f'id="grant-{viewer}-project-{item["Fancy Proj. Name"]}" checked' in response.text
    assert response.text.count('role="alert"') == 1
    assert f'id="cell-{viewer}-environment-{item["db"]}"' in response.text
    assert await grant_rows(web_app) == before
    assert await audit_count(web_app, "grant.add", "grant.remove") == entries


async def test_an_item_already_included_from_above_cannot_be_granted_in_the_grid(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    viewer = await user_id(web_app, "viewer")
    before = await grant_rows(web_app)
    values = {"user": viewer, "target": f"environment:{item['db']}", "was": "0"}
    response = await post(web_client, "/access/grid/toggle", values)
    assert response.status_code == 409
    assert "already included through Fancy Proj. Name" in response.text
    assert 'data-cell="inherited"' in response.text
    assert "HX-Trigger" not in response.headers
    assert await grant_rows(web_app) == before


async def test_a_toggle_for_something_that_cannot_be_granted_writes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    item = await ids(web_app)
    victim = await user_id(web_app, "ungranted_admin")
    root = await user_id(web_app, "root")
    before = await grant_rows(web_app)
    entries = await audit_count(web_app, "grant.add", "grant.remove")
    for target in (
        f"service:{item['api_service']}",
        "environment:x",
        "project:0",
        "project:-1",
        "",
        "bogus",
        f"project:{'9' * 16}",
    ):
        response = await post(web_client, "/access/grid/toggle", {"user": victim, "target": target})
        assert response.status_code == 422, target
    for bad_user in ("abc", "0", "-4", "9" * 16, ""):
        data = {"user": bad_user, "target": f"project:{item['billing']}", "was": "0"}
        assert (await post(web_client, "/access/grid/toggle", data)).status_code == 422, bad_user
    missing_user = await post(
        web_client,
        "/access/grid/toggle",
        {"user": 999_999, "target": f"project:{item['billing']}", "was": "0"},
    )
    assert missing_user.status_code == 404
    superadmin = await post(
        web_client,
        "/access/grid/toggle",
        {"user": root, "target": f"project:{item['billing']}", "was": "0"},
    )
    assert superadmin.status_code == 409
    assert "Superadmins reach every project" in superadmin.text
    gone = await post(
        web_client,
        "/access/grid/toggle",
        {"user": victim, "target": "environment:424242", "was": "0"},
    )
    assert gone.status_code == 409
    assert "no longer exists" in gone.text
    assert f'id="cell-{victim}-environment-424242"' in gone.text
    assert "checkbox" not in gone.text
    assert await grant_rows(web_app) == before
    assert await audit_count(web_app, "grant.add", "grant.remove") == entries


async def test_the_users_list_and_the_user_detail_link_to_the_grid(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    users = main_of((await web_client.get("/users")).text)
    assert users.count('href="/access/grid"') == 2
    assert 'href="/users" aria-current="page"' in users
    assert users.index('href="/access/grid"') < users.index('data-target="user-create"')
    assert "Superadmin</span>" in users
    for name in ("viewer", "root"):
        detail = main_of((await web_client.get(f"/users/{await user_id(web_app, name)}")).text)
        assert ">Open access grid</a>" in detail
        assert 'href="/access/grid"' in detail


async def test_the_grid_says_so_when_there_are_no_users_to_show_or_no_projects(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    async with web_app.state.session_factory() as db:
        await db.execute(
            update(User).where(User.group != UserGroup.SUPERADMIN).values(is_active=False)
        )
        await db.commit()
    for url in ("/access/grid", "/access/grid/table"):
        page = (await web_client.get(url, headers=HTMX)).text
        assert "No users to show" in page
        assert "<table" not in page
    async with web_app.state.session_factory() as db:
        await db.execute(delete(ApiTokenRestriction))
        await db.execute(delete(Project))
        await db.commit()
    page = (await web_client.get("/access/grid")).text
    assert "No projects yet" in page
    assert "No users to show" not in page
    assert "<table" not in main_of(page)
