"""Deployment detail page and fragments on the seeded application, by role and scope."""

import re
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import Deployment
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, item_header_of, main_of, section_links


@dataclass(frozen=True)
class DeploymentIds:
    """Ids of the seeded deployments by name, read from the application database."""

    prod: int
    staging: int
    main: int
    legacy: int


@pytest.fixture
async def ids(web_app: FastAPI) -> DeploymentIds:
    """The seeded deployment ids of the web application."""
    async with web_app.state.session_factory() as db:
        rows = dict((await db.execute(select(Deployment.name, Deployment.id))).all())
    return DeploymentIds(
        prod=rows["prod"], staging=rows["staging"], main=rows["main"], legacy=rows["legacy"]
    )


def service_names(page: str) -> list[str]:
    """Service names in row order."""
    return re.findall(r'data-fragment="service-row"><th[^>]*><a [^>]*>([^<]+)</a>', page)


async def test_superadmin_sees_everything_and_the_edit_actions(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The full page: header, status strip, services, history and the editable policies."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{ids.prod}")
    page = response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"
    header = item_header_of(page)
    assert (header.type, header.title) == ("deployment", "Deployment: prod")
    assert header.subtitle == "Project Fancy Proj. Name · /opt/rentals/prod"
    assert header.details is None
    assert ">Failed<" in page
    assert section_links(page) == ["Services", "Deploy history", "AccessSuperadmin"]
    assert f'href="/deployments/{ids.prod}/access"' in page
    assert f'href="/deployments/{ids.prod}/edit"' in page
    assert f'href="/deployments/{ids.prod}/dist"' in page
    assert f'href="/deployments/{ids.prod}/services/new"' in page
    assert "Edit policies" in page and "Read-only" not in page
    assert service_names(page) == ["mysql", "web"]
    assert "Environments in your scope" not in page
    for status_cell in ("Last deploy", "Drift state", "Dist check", "Post-deploy"):
        assert status_cell in page
    assert page.count("Not checked yet") == 2
    assert ".env.db v2, .env.db.secrets v1" in page
    assert "Post-deploy exit code 0, output truncated." in page
    assert "0600, www-data:www-data" in page
    assert 'hx-trigger="deploy-finished from:body"' in page


async def test_admin_has_no_structure_actions(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """An admin can check and deploy but has no edit, add service or policy edit link."""
    await sign_in(web_client, "admin")
    page = (await web_client.get(f"/deployments/{ids.prod}")).text
    assert f'href="/deployments/{ids.prod}/dist"' in page
    assert 'href="/services/' in page and "#deploy" in page
    for absent in ("/edit", "/services/new", "Edit policies"):
        assert absent not in page
    assert "Read-only" in page
    assert "Post-deploy exit code" in page


async def test_viewer_reads_without_check_deploy_or_output(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Edit actions are absent for a viewer, and so are the exit code and the check link."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get(f"/deployments/{ids.prod}")).text
    assert service_names(page) == ["mysql", "web"]
    for absent in ("/dist", "#deploy", "/edit", "/services/new", "exit code", "Check dist"):
        assert absent not in page
    assert "Read-only" in page
    assert "audit entry" not in page


async def test_environment_grant_gets_a_name_only_view_with_a_note(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Only the granted file's service and runs show: no path, policies or other names."""
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(f"/deployments/{ids.prod}")).text
    body = main_of(page)
    assert (
        "You have access to one environment of this deployment. Only its service is shown." in body
    )
    assert "Environments in your scope" in body
    assert service_names(page) == ["mysql"]
    assert ".env.db v2" in body
    assert "Post-deploy exit code n/a, output truncated." in body
    for hidden in (".env.db.secrets", ".env.web", "web</a>", "/opt/rentals", "Policies"):
        assert hidden not in body
    assert "Project Fancy Proj. Name" in body


async def test_outside_allowed_path_disables_the_check_and_says_why(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """A deployment outside the allowed paths gets a note, a disabled check and no deploy."""
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{ids.legacy}")).text
    assert "/srv/old/legacy" in page
    assert "This deployment is outside the allowed paths. It cannot be deployed or checked" in page
    check = re.search(r"<button[^>]*disabled[^>]*>.*?Check dist</button>", page, re.DOTALL)
    assert check is not None
    assert "/dist" not in page
    assert "#deploy" not in page


async def test_deployments_outside_the_scope_are_not_found(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Out of scope and nonexistent deployments answer the same 404 on the page and fragments."""
    await sign_in(web_client, "viewer")
    for suffix in ("", "/services", "/history"):
        assert (await web_client.get(f"/deployments/{ids.main}{suffix}")).status_code == 404
    assert (await web_client.get("/deployments/9999")).status_code == 404


async def test_fragments_are_exactly_the_panels(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The refresh endpoints answer one section each, never stored and varied on HX-Request."""
    await sign_in(web_client, "root")
    for suffix, fragment in (("services", "services-table"), ("history", "deploy-history")):
        response = await web_client.get(f"/deployments/{ids.prod}/{suffix}", headers=HTMX)
        assert response.text.lstrip().startswith("<section aria-labelledby=")
        assert f'data-fragment="{fragment}"' in response.text
        assert "<html" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "HX-Request"


async def test_empty_deployment_shows_empty_states(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: DeploymentIds
) -> None:
    """Without services or runs both panels say so, and only the superadmin may add a service."""
    async with web_app.state.session_factory() as db:
        project_id = (
            await db.execute(select(Deployment.project_id).where(Deployment.id == ids.prod))
        ).scalar_one()
        empty = Deployment(project_id=project_id, name="empty", root_path="/opt/rentals/empty")
        db.add(empty)
        await db.commit()
        empty_id = empty.id
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{empty_id}")).text
    assert "No services yet" in page and "No deploys yet" in page
    assert page.count(f'href="/deployments/{empty_id}/services/new"') == 2
    assert page.count("Never deployed") == 1
    await sign_in(web_client, "admin")
    page = (await web_client.get(f"/deployments/{empty_id}")).text
    assert "No services yet" in page and "/services/new" not in page


async def test_anonymous_requests_are_refused(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Every deployment route needs a session."""
    for suffix in ("", "/services", "/history"):
        response = await web_client.get(f"/deployments/{ids.prod}{suffix}", follow_redirects=False)
        assert response.status_code in (303, 401)
