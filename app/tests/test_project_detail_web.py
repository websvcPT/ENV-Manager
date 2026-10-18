"""Project detail pages and fragments on the seeded application, by role and scope."""

import re
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import (
    ApiToken,
    ApiTokenRestriction,
    AuditEntry,
    Project,
    RestrictionLevel,
)
from app.tests.conftest import sign_in

HTMX = {"HX-Request": "true"}


@dataclass(frozen=True)
class ProjectIds:
    """Ids of the seeded projects, read from the application database."""

    rentals: int
    billing: int


@pytest.fixture
async def ids(web_app: FastAPI) -> ProjectIds:
    """The seeded project ids of the web application."""
    async with web_app.state.session_factory() as db:
        rows = dict((await db.execute(select(Project.name, Project.id))).all())
    return ProjectIds(rentals=rows["rentals"], billing=rows["billing"])


async def post(http: httpx.AsyncClient, url: str, **data: str) -> httpx.Response:
    """Submit a form the way an htmx request does, with the CSRF header."""
    await http.get("/healthz")
    headers = {**HTMX, "X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]}
    return await http.post(url, data=data, headers=headers)


def tab_labels(page: str) -> list[str]:
    """Visible text of the section tabs, without the Superadmin tag."""
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', page, re.DOTALL)
    assert nav is not None
    anchors = re.findall(r"<a [^>]*>(.*?)</a>", nav.group(0), re.DOTALL)
    return [re.sub(r"<[^>]+>", "", anchor).replace("Superadmin", "").strip() for anchor in anchors]


def row_names(page: str) -> list[str]:
    """Deployment names in row order."""
    return re.findall(r'data-fragment="deployment-row"><th[^>]*><a [^>]*>([^<]+)</a>', page)


async def test_superadmin_sees_all_tabs_and_actions(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """The superadmin gets four tabs, the member count and the structure actions."""
    await sign_in(web_client, "root")
    rentals = ids.rentals
    page = (await web_client.get(f"/projects/{rentals}")).text
    assert tab_labels(page) == ["Deployments", "Members and access", "Activity", "Settings"]
    assert "2 deployments · 6 members" in page
    assert "1 needs attention" in page
    assert f'href="/projects/{rentals}/deployments/new"' in page
    assert row_names(page) == ["prod", "staging"]
    assert "/opt/rentals/prod" in page
    assert 'hx-trigger="deploy-finished from:body"' in page
    assert "Failed" in page


async def test_admin_has_two_tabs_and_no_structure_actions(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """Edit actions are absent for an admin and the members count is not shown."""
    await sign_in(web_client, "admin")
    page = (await web_client.get(f"/projects/{ids.rentals}")).text
    assert tab_labels(page) == ["Deployments", "Activity"]
    assert "2 deployments" in page and "members" not in page.split("<main")[1]
    assert "New deployment" not in page


async def test_viewer_sees_the_same_read_only_page(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """A viewer reads deployments only: the audit log is not open to viewers."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get(f"/projects/{ids.rentals}")).text
    assert tab_labels(page) == ["Deployments"]
    assert row_names(page) == ["prod", "staging"]
    assert "New deployment" not in page


async def test_deployment_grant_is_told_what_is_hidden_without_names(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """A deployment grant lists the one deployment and never names the others."""
    await sign_in(web_client, "deployment_admin")
    page = (await web_client.get(f"/projects/{ids.rentals}")).text
    assert "Your access is limited to one deployment of this project." in page
    assert "You have access to prod only. Other deployments of this project are not listed." in page
    assert row_names(page) == ["prod"]
    for hidden in ("staging", "billing", ".env.api", "/opt/rentals/staging"):
        assert hidden not in page


async def test_environment_grant_hides_the_parent_path(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """The parent deployment of a granted file appears by name with no path or siblings."""
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(f"/projects/{ids.rentals}")).text
    assert "Your access is limited to selected files of this project." in page
    assert row_names(page) == ["prod"]
    assert "Limited access" in page
    for hidden in ("/opt/rentals", ".env.web", ".env.db.secrets", "staging", "mysql"):
        assert hidden not in page.split("<main")[1].replace("selected files", "")


async def test_projects_outside_the_scope_are_not_found(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """Out of scope and nonexistent projects answer the same 404 on every tab."""
    await sign_in(web_client, "viewer")
    billing = ids.billing
    for suffix in ("", "/activity", "/deployments", "/access", "/members", "/settings"):
        assert (await web_client.get(f"/projects/{billing}{suffix}")).status_code == 404
    assert (await web_client.get("/projects/9999")).status_code == 404


async def test_superadmin_only_tabs_are_forbidden_for_other_groups(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """A project in scope still refuses Access and Settings to a non-superadmin."""
    await sign_in(web_client, "project_admin")
    rentals = ids.rentals
    assert (await web_client.get(f"/projects/{rentals}/access")).status_code == 403
    assert (await web_client.get(f"/projects/{rentals}/members")).status_code == 403
    assert (await web_client.get(f"/projects/{rentals}/settings")).status_code == 403
    assert (await post(web_client, f"/projects/{rentals}/rename", name="x")).status_code == 403
    deleted = await post(web_client, f"/projects/{rentals}/delete", confirm_name="rentals")
    assert deleted.status_code == 403


async def test_deployments_fragment_is_only_the_panel(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """The refresh endpoint answers exactly the swappable section."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/projects/{ids.rentals}/deployments", headers=HTMX)
    assert response.text.lstrip().startswith('<section aria-labelledby="deployments-title"')
    assert 'data-fragment="deployments-table"' in response.text
    assert "<html" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"


async def test_empty_project_offers_the_first_deployment(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """A project without deployments says so and the superadmin can add one."""
    await sign_in(web_client, "root")
    created = await post(web_client, "/projects", name="fresh")
    assert created.status_code == 200
    project_id = re.search(r'href="/projects/(\d+)"[^>]*>fresh<', created.text)
    assert project_id is not None
    page = (await web_client.get(f"/projects/{project_id.group(1)}")).text
    assert "No deployments yet" in page
    assert "0 deployments · 0 members" in page
    assert ">Empty<" in page
    assert page.count(f"/projects/{project_id.group(1)}/deployments/new") == 2


async def test_activity_tab_lists_scoped_entries(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """The activity table shows time, actor, action, object and result, newest first."""
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/projects/{ids.rentals}/activity")).text
    assert 'data-fragment="activity-table"' in page
    assert "2026-10-17 10:05" in page
    assert page.index("2026-10-17 10:05") < page.index("2026-10-15 17:40")
    assert "file_save" in page and ".env.db" in page


async def test_activity_for_a_scoped_user_leaves_out_other_items(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """An environment grant sees its own file save and not the deploys beside it."""
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(f"/projects/{ids.rentals}/activity")).text
    assert "file_save" in page
    assert ">deploy<" not in page


@pytest.mark.parametrize("user", ["viewer", "env_scoped_viewer"])
async def test_activity_is_forbidden_for_viewers(
    web_client: httpx.AsyncClient, ids: ProjectIds, user: str
) -> None:
    """The project activity follows the audit log rule: viewers get no tab and a 403."""
    await sign_in(web_client, user)
    assert (await web_client.get(f"/projects/{ids.rentals}/activity")).status_code == 403
    page = (await web_client.get(f"/projects/{ids.rentals}")).text
    assert "/activity" not in page


async def test_activity_empty_state(web_client: httpx.AsyncClient, web_app: FastAPI) -> None:
    """A project with no entries in scope says so."""
    await sign_in(web_client, "root")
    created = await post(web_client, "/projects", name="quiet")
    project_id = re.search(r'href="/projects/(\d+)"[^>]*>quiet<', created.text)
    assert project_id is not None
    async with web_app.state.session_factory() as db:
        entries = (await db.execute(select(AuditEntry))).scalars().all()
        for entry in entries:
            await db.delete(entry)
        await db.commit()
    page = (await web_client.get(f"/projects/{project_id.group(1)}/activity")).text
    assert "No activity yet" in page


async def test_members_tab_address_redirects_to_the_access_tab(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """The old address of the Members tab still works and leads to the Access tab."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/projects/{ids.rentals}/members", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/projects/{ids.rentals}/access"
    assert response.headers["cache-control"] == "no-store"
    page = (await web_client.get(f"/projects/{ids.rentals}/members", follow_redirects=True)).text
    assert 'data-fragment="access-list"' in page


async def test_settings_tab_has_rename_and_delete(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """The settings tab holds the rename form, the danger zone and the delete dialog."""
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/projects/{ids.rentals}/settings")).text
    assert 'data-fragment="project-rename"' in page
    assert 'value="rentals"' in page
    assert 'data-target="delete-project"' in page
    assert 'id="delete-project"' in page
    assert "Type the project name to confirm" in page
    assert "its 2 deployments" in page


async def test_rename_refuses_bad_and_duplicate_names_and_keeps_the_input(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """A refused rename swaps the panel with the reason and the typed value."""
    await sign_in(web_client, "root")
    rentals = ids.rentals
    bad = await post(web_client, f"/projects/{rentals}/rename", name="Bad Name")
    assert bad.status_code == 422
    assert bad.text.lstrip().startswith("<section")
    assert 'role="alert"' in bad.text and 'value="Bad Name"' in bad.text
    duplicate = await post(web_client, f"/projects/{rentals}/rename", name="billing")
    assert duplicate.status_code == 422
    assert "already exists" in duplicate.text


async def test_rename_succeeds_and_reloads_the_settings_tab(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: ProjectIds
) -> None:
    """A good name is stored, audited, and the browser is sent back to the settings tab."""
    await sign_in(web_client, "root")
    rentals = ids.rentals
    response = await post(web_client, f"/projects/{rentals}/rename", name="rentals-two")
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/projects/{rentals}/settings?renamed=true"
    page = (await web_client.get(response.headers["hx-redirect"])).text
    assert "Project renamed" in page and "rentals-two" in page
    async with web_app.state.session_factory() as db:
        renamed = await db.get(Project, rentals)
        assert renamed is not None and renamed.name == "rentals-two"
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "project.rename"))
        ).scalar_one()
    assert entry.details == {"previous_name": "rentals"}


async def test_delete_needs_the_exact_name(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: ProjectIds
) -> None:
    """A wrong or empty confirmation keeps the project and swaps the form with the message."""
    await sign_in(web_client, "root")
    rentals = ids.rentals
    for typed in ("", "rental", "RENTALS"):
        response = await post(web_client, f"/projects/{rentals}/delete", confirm_name=typed)
        assert response.status_code == 422
        assert "Type the project name exactly to confirm." in response.text
        assert 'id="delete-project-form"' in response.text
    async with web_app.state.session_factory() as db:
        assert await db.get(Project, rentals) is not None


async def test_delete_removes_the_project_and_leaves_for_the_list(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A confirmed delete is audited and the browser is sent to the projects list."""
    await sign_in(web_client, "root")
    created = await post(web_client, "/projects", name="doomed")
    project_id = int(re.search(r'href="/projects/(\d+)"[^>]*>doomed<', created.text).group(1))  # type: ignore[union-attr]
    response = await post(web_client, f"/projects/{project_id}/delete", confirm_name="doomed")
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/projects"
    assert (await web_client.get(f"/projects/{project_id}")).status_code == 404
    async with web_app.state.session_factory() as db:
        assert await db.get(Project, project_id) is None
        actions = (
            (await db.execute(select(AuditEntry.action).where(AuditEntry.project_id == project_id)))
            .scalars()
            .all()
        )
    assert "project.delete" in actions


async def test_delete_is_blocked_while_a_token_is_restricted_inside(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: ProjectIds
) -> None:
    """Restricted tokens are named in the dialog and nothing is deleted."""
    await sign_in(web_client, "root")
    billing = ids.billing
    response = await post(web_client, f"/projects/{billing}/delete", confirm_name="billing")
    assert response.status_code == 422
    assert "billing-ci" in response.text
    assert "Change or revoke them first." in response.text
    async with web_app.state.session_factory() as db:
        assert await db.get(Project, billing) is not None
        token_stmt = select(ApiToken).where(ApiToken.name == "billing-ci")
        token = (await db.execute(token_stmt)).scalar_one()
        restriction = (
            await db.execute(
                select(ApiTokenRestriction).where(ApiTokenRestriction.token_id == token.id)
            )
        ).scalar_one()
    assert restriction.level is RestrictionLevel.ENVIRONMENT


@pytest.mark.parametrize("user", ["admin", "viewer"])
async def test_non_superadmin_pages_have_no_settings_or_delete_markup(
    web_client: httpx.AsyncClient, ids: ProjectIds, user: str
) -> None:
    """Edit actions are absent, not disabled, for groups that cannot manage structure."""
    await sign_in(web_client, user)
    page = (await web_client.get(f"/projects/{ids.rentals}")).text
    for absent in ("delete-project", "project-rename", "/settings", "/members", "/access"):
        assert absent not in page


async def test_anonymous_requests_are_refused(
    web_client: httpx.AsyncClient, ids: ProjectIds
) -> None:
    """Every detail route needs a session."""
    rentals = ids.rentals
    for suffix in ("", "/activity", "/access", "/members", "/settings", "/deployments"):
        response = await web_client.get(f"/projects/{rentals}{suffix}", follow_redirects=False)
        assert response.status_code in (303, 401)
