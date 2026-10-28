"""Projects page, table fragment and project creation, on the seeded application."""

import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import ApiTokenRestriction, AuditEntry, Project, User
from app.services.projects import delete_project
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, main_of
from app.web import projects as projects_web

TABLE = re.compile(r'<section data-fragment="projects-table".*?</section>', re.DOTALL)


def table_html(page: str) -> str:
    """The projects table panel of a page or fragment."""
    match = TABLE.search(page)
    assert match is not None
    return match.group(0)


def project_names(page: str) -> list[str]:
    """Project names in row order."""
    return re.findall(r'<a href="/projects/\d+"[^>]*>([^<]+)</a>', table_html(page))


async def post_project(http: httpx.AsyncClient, name: str) -> httpx.Response:
    """Submit the new project form the way the dialog does."""
    await http.get("/healthz")
    headers = {**HTMX, "X-CSRFToken": http.cookies[CSRF_COOKIE_NAME]}
    return await http.post("/projects", data={"name": name}, headers=headers)


async def test_superadmin_page_has_members_and_create(web_client: httpx.AsyncClient) -> None:
    """The superadmin sees every project, the Members column, the create button and dialog."""
    await sign_in(web_client, "root")
    page = (await web_client.get("/projects")).text
    assert project_names(page) == ["billing", "Fancy Proj. Name"]
    assert "Members" in table_html(page)
    assert "All projects." in page
    assert 'data-target="project-new"' in page
    assert 'id="project-new"' in page
    assert 'hx-post="/projects"' in page
    assert "Superadmin" not in main_of(page)


@pytest.mark.parametrize("user", ["admin", "viewer"])
async def test_other_groups_get_no_create_or_members(
    web_client: httpx.AsyncClient, user: str
) -> None:
    """Edit actions are absent, not disabled, and who has access is not shown."""
    await sign_in(web_client, user)
    page = (await web_client.get("/projects")).text
    assert "Projects you have access to." in page
    assert "Members" not in table_html(page)
    assert "New project" not in page
    assert 'id="project-new"' not in page


async def test_deployment_scope_never_shows_hidden_names(web_client: httpx.AsyncClient) -> None:
    """A deployment grant lists the project as limited and leaks nothing outside the grant."""
    await sign_in(web_client, "deployment_admin")
    page = (await web_client.get("/projects")).text
    assert project_names(page) == ["Fancy Proj. Name"]
    assert "Access limited to prod" in page
    assert "of your scope" in page
    for hidden in ("billing", "staging", "worker", ".env.api"):
        assert hidden not in page


async def test_environment_scope_never_shows_hidden_names(web_client: httpx.AsyncClient) -> None:
    """Environment grants name only the granted files, in both projects."""
    await sign_in(web_client, "env_scoped_viewer")
    page = (await web_client.get("/projects")).text
    assert "Access limited to .env.db" in page
    assert "Access limited to .env.api" in page
    for hidden in ("staging", ".env.web", "worker", ".env.db.secrets"):
        assert hidden not in page


async def test_user_without_grants_sees_the_assigned_empty_state(
    web_client: httpx.AsyncClient,
) -> None:
    """With nothing granted the panel explains how to get access."""
    await sign_in(web_client, "ungranted_admin")
    page = (await web_client.get("/projects")).text
    assert "No projects assigned" in page
    assert 'data-fragment="empty-state"' in page
    assert "New project" not in page


async def test_superadmin_with_no_projects_is_offered_the_first_one(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """An empty system shows the create call to action to the superadmin."""
    async with web_app.state.session_factory() as db:
        root = (await db.execute(select(User).where(User.name == "root"))).scalar_one()
        for restriction in (await db.execute(select(ApiTokenRestriction))).scalars().all():
            await db.delete(restriction)
        await db.flush()
        for project in (await db.execute(select(Project))).scalars().all():
            await delete_project(db, project, actor=root)
    await sign_in(web_client, "root")
    page = (await web_client.get("/projects")).text
    assert "No projects yet" in table_html(page)
    assert 'data-target="project-new"' in table_html(page)


async def test_table_fragment_is_only_the_panel(web_client: httpx.AsyncClient) -> None:
    """The fragment endpoint returns exactly the swappable section, uncached and varied."""
    await sign_in(web_client, "root")
    response = await web_client.get("/projects/table", headers=HTMX)
    assert response.text.lstrip().startswith('<section data-fragment="projects-table"')
    assert "<html" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "HX-Request" in response.headers["vary"]


async def test_search_filters_and_escapes_the_query(web_client: httpx.AsyncClient) -> None:
    """The query narrows the list; with no match the panel says so without echoing markup."""
    await sign_in(web_client, "root")
    assert project_names((await web_client.get("/projects?q=fancy")).text) == ["Fancy Proj. Name"]
    assert 'value="fancy"' in (await web_client.get("/projects?q=fancy")).text
    none = (await web_client.get("/projects/table", params={"q": "<script>x"}, headers=HTMX)).text
    assert "No projects match" in none
    assert "<script>" not in none
    assert "&lt;script&gt;x" in none


async def test_table_load_failure_answers_a_retry_panel(
    web_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database failure becomes a 503 error panel with Retry and no details."""

    async def broken(*_args: object, **_kwargs: object) -> list[object]:
        raise OperationalError("select", {}, Exception("secret detail"))

    await sign_in(web_client, "root")
    monkeypatch.setattr(projects_web, "list_project_rows", broken)
    response = await web_client.get("/projects/table", headers=HTMX)
    assert response.status_code == 503
    assert 'data-fragment="inline-error"' in response.text
    assert 'role="alert"' in response.text
    assert "Could not load projects" in response.text
    assert 'hx-get="/projects/table"' in response.text
    assert response.text.count("hx-status:503") == 2
    assert 'hx-include="#ps"' in response.text
    assert "secret detail" not in response.text


async def test_create_project_goes_to_the_new_project_with_a_toast(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A valid name creates the project, audits it and sends the browser to its page."""
    await sign_in(web_client, "root")
    response = await post_project(web_client, " fleetops ")
    assert response.status_code == 200
    async with web_app.state.session_factory() as db:
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "project.create"))
        ).scalar_one()
    assert (entry.target_name, entry.actor_name) == ("fleetops", "root")
    assert response.headers["hx-redirect"] == f"/projects/{entry.target_id}?created=1"
    page = (await web_client.get(response.headers["hx-redirect"])).text
    assert "Project created" in page and "fleetops" in page
    assert "Project created" not in (await web_client.get(f"/projects/{entry.target_id}")).text


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Enter a project name."),
        ("Bad Name!", "Use 3 to 100 characters"),
        ("ab", "Use 3 to 100 characters"),
        ("...", "at least one letter or number"),
        ("fancy PROJ. name", "already exists"),
        ("Fancy  Proj.   Name", "already exists"),
        ("Fancy Proj. Name", "already exists"),
    ],
)
async def test_refused_name_swaps_the_form_with_the_reason(
    web_client: httpx.AsyncClient, name: str, message: str
) -> None:
    """Validation failures answer 422 with the form, the typed value and an alert."""
    await sign_in(web_client, "root")
    response = await post_project(web_client, name)
    assert response.status_code == 422
    assert response.text.lstrip().startswith('<form id="project-new-form"')
    assert 'role="alert"' in response.text
    assert message in response.text
    assert 'aria-invalid="true"' in response.text
    assert "hx-trigger" not in response.headers


@pytest.mark.parametrize("user", ["admin", "viewer", "deployment_admin"])
async def test_only_the_superadmin_may_create(web_client: httpx.AsyncClient, user: str) -> None:
    """Every structure change is superadmin only, whatever the grants."""
    await sign_in(web_client, user)
    response = await post_project(web_client, "sneaky")
    assert response.status_code == 403
    page = (await web_client.get("/projects")).text
    assert "sneaky" not in page


async def test_create_requires_a_session_and_a_csrf_token(web_client: httpx.AsyncClient) -> None:
    """Signed out is refused, and a signed-in post without the token is refused too."""
    assert (await post_project(web_client, "anon")).status_code == 401
    await sign_in(web_client, "root")
    response = await web_client.post("/projects", data={"name": "forged"}, headers=HTMX)
    assert response.status_code == 403
