"""Service page and fragments on the seeded application, by role and scope."""

import re
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import Deployment, Service
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, main_of, section_links
from app.web import service_detail as service_detail_web

FRAGMENTS = ("environments", "postdeploy", "history")


@dataclass(frozen=True)
class ServiceIds:
    """Ids of the seeded services, read from the application database."""

    mysql: int
    web: int
    staging_web: int
    api: int
    cron: int


@pytest.fixture
async def ids(web_app: FastAPI) -> ServiceIds:
    """The seeded service ids of the web application."""
    async with web_app.state.session_factory() as db:
        rows = (
            await db.execute(
                select(Deployment.name, Service.name, Service.id).join(
                    Service, Service.deployment_id == Deployment.id
                )
            )
        ).all()
    found = {(deployment, service): service_id for deployment, service, service_id in rows}
    return ServiceIds(
        mysql=found[("prod", "mysql")],
        web=found[("prod", "web")],
        staging_web=found[("staging", "web")],
        api=found[("main", "api")],
        cron=found[("legacy", "cron")],
    )


def environment_rows(page: str) -> dict[str, str]:
    """Table rows by environment name with the status badge key of each."""
    rows = re.findall(r'<tr data-fragment="env-row" data-env="([^"]+)">(.*?)</tr>', page, re.DOTALL)
    return {name: re.search(r'data-status="(\w+)"', body).group(1) for name, body in rows}  # type: ignore[union-attr]


def row_actions(page: str, name: str) -> list[str]:
    """Keys of the row actions of one environment table row, in order."""
    pattern = rf'<tr data-fragment="env-row" data-env="{re.escape(name)}">(.*?)</tr>'
    row = re.search(pattern, page, re.DOTALL)
    assert row is not None
    cell = row.group(1).rsplit("<td", 1)[1]
    return re.findall(r'data-row-action="([a-z-]+)"', cell)


async def test_superadmin_sees_everything_and_the_command_settings(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """Header, every status, the stored command in its form, output and history."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/services/{ids.staging_web}")
    page = response.text
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"
    assert '<h1 class="font-mono' in page and ">web</h1>" in page
    assert ">Service</span>" in page and ">1 pending</span>" in page
    assert "Deployment <strong" in page and "/opt/rentals/staging" in page
    assert section_links(page) == ["Environments 4", "Deploy history", "AccessSuperadmin"]
    assert f'href="/services/{ids.staging_web}/access"' in page
    assert page.count('data-fragment="service-explainer"') == 2
    history_at = page.rindex('data-fragment="deploy-history"')
    assert history_at < page.index('data-fragment="service-explainer"')
    assert "What is a service?" in page and "Runs once, not per file" in page
    assert environment_rows(page) == {
        ".env.web": "deployed",
        ".env.web.replica": "blocked",
        ".env.web.tuning": "pending",
        "web-ca.pem": "never",
    }
    assert row_actions(page, ".env.web.tuning") == ["edit", "history", "dist"]
    assert f'href="/services/{ids.staging_web}/environments/new"' in page
    assert "Read-only view" not in page and "Deploy service" in page
    assert "Command settings" in page and "Superadmin" in page
    assert "docker compose up -d --no-deps web" in page
    assert 'placeholder="/opt/rentals/staging"' in page
    assert "web recreated" in page and "exit code 0" in page and "by admin" in page
    assert page.count('data-fragment="history-row"') == 2
    assert ">Success<" in page and ">Blocked<" in page
    assert "Command text and settings are visible to the superadmin only." not in page
    assert page.count("hx-ignore") == 2


async def test_dist_and_versions_read_from_the_stored_figures(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """Dist link, current and deployed versions and none markers show per row."""
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/services/{ids.mysql}")).text
    assert environment_rows(page) == {
        ".env.db": "failed",
        ".env.db.app": "never",
        ".env.db.secrets": "failed",
    }
    assert "Dist linked · current v2 · deployed None" in page
    assert "Dist none · current v1 · deployed None" in page
    assert "&mdash;" not in page and "\u2014" not in page
    assert "deploy did not finish" in page and "exit code" not in page


async def test_admin_reads_output_but_not_the_command(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """Admins edit and check dist, see the output, and get no add action or command settings."""
    await sign_in(web_client, "admin")
    page = (await web_client.get(f"/services/{ids.staging_web}")).text
    assert row_actions(page, ".env.web") == ["edit", "history", "dist"]
    assert "web recreated" in page
    assert "Command text and settings are visible to the superadmin only." in page
    for absent in ("Add environment", "Command settings", "docker compose up", "Read-only view"):
        assert absent not in page


async def test_viewer_reads_without_edit_dist_output_or_command(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """Viewers get View and History, a read-only note and a status-only post-deploy panel."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get(f"/services/{ids.staging_web}")).text
    assert "Read-only view. Editing and deploying are not available for the Viewer group." in page
    assert row_actions(page, ".env.web") == ["view", "history"]
    assert "Output and command text are not available for the Viewer group." in page
    for absent in (
        "Compare to dist",
        "Add environment",
        "web recreated",
        "Command settings",
        "by admin",
        "exit code",
        "visible to the superadmin only",
    ):
        assert absent not in page
    assert ">Ran<" in page


async def test_environment_grant_sees_a_name_only_view_with_notes(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """One granted file: no path, a count note, the restart note and none of the hidden names."""
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(f"/services/{ids.mysql}")).text
    body = main_of(page)
    assert environment_rows(body) == {".env.db": "failed"}
    assert section_links(page) == ["Environments 1", "Deploy history"]
    assert (
        "You see 1 environment of this service. Deploys write only the environments you can see."
        in body
    )
    assert "This service also loads environments outside your access." in body
    assert "Deployment <strong" in body and "Fancy Proj. Name" in body
    assert "deploy did not finish" in body
    assert "Command text and settings are visible to the superadmin only." in body
    for hidden in (".env.db.secrets", "/opt/rentals", "Add environment", "Read-only view"):
        assert hidden not in body
    assert ">1 pending</span>" not in body


async def test_environment_grant_viewer_gets_both_notes(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """A scoped viewer is read-only and scoped at once."""
    await sign_in(web_client, "env_scoped_viewer")
    body = main_of((await web_client.get(f"/services/{ids.mysql}")).text)
    assert "Read-only view." in body and "You see 1 environment of this service." in body
    assert row_actions(body, ".env.db") == ["view", "history"]
    assert ".env.db.secrets" not in body


async def test_deployment_grant_sees_the_root_and_every_file(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """A deployment grant covers its services, including the root path."""
    await sign_in(web_client, "deployment_admin")
    page = (await web_client.get(f"/services/{ids.mysql}")).text
    assert "/opt/rentals/prod" in page
    assert set(environment_rows(page)) == {".env.db", ".env.db.app", ".env.db.secrets"}
    assert "You see" not in page


async def test_empty_service_says_so_and_offers_the_add_only_to_the_superadmin(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """A service without environments shows the empty state, with the action for structure."""
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/services/{ids.cron}")).text
    assert "No environments yet" in page
    assert page.count(f'href="/services/{ids.cron}/environments/new"') == 2
    assert "Post-deploy command" in page and "No deploys yet." in page
    await sign_in(web_client, "admin")
    other = (await web_client.get(f"/services/{ids.cron}")).text
    assert "No environments yet" in other and "Add environment" not in other


async def test_out_of_scope_and_unknown_services_are_not_found(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """The page and every fragment answer the same 404 for what the caller may not see."""
    await sign_in(web_client, "viewer")
    for suffix in ("", *(f"/{name}" for name in FRAGMENTS)):
        assert (await web_client.get(f"/services/{ids.api}{suffix}")).status_code == 404
        assert (await web_client.get(f"/services/9999{suffix}")).status_code == 404
    await sign_in(web_client, "ungranted_admin")
    assert (await web_client.get(f"/services/{ids.mysql}")).status_code == 404
    assert (await web_client.get(f"/services/{ids.mysql}/postdeploy")).status_code == 404


async def test_fragments_return_their_panel_with_the_same_visibility(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """Each fragment is exactly its panel and honours scope and group like the page."""
    await sign_in(web_client, "env_scoped_admin")
    expected = {
        "environments": 'data-fragment="environments-table"',
        "postdeploy": 'data-fragment="postdeploy-panel"',
        "history": 'data-fragment="deploy-history"',
    }
    for name, marker in expected.items():
        response = await web_client.get(f"/services/{ids.mysql}/{name}", headers=HTMX)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "HX-Request"
        assert response.text.lstrip().startswith("<section") and marker in response.text
        assert "<html" not in response.text and ".env.db.secrets" not in response.text
    history = (await web_client.get(f"/services/{ids.mysql}/history", headers=HTMX)).text
    assert history.count('data-fragment="history-row"') == 1
    panel = (await web_client.get(f"/services/{ids.mysql}/postdeploy", headers=HTMX)).text
    assert "Command settings" not in panel


async def test_environment_fragment_failure_answers_a_retry_panel(
    web_client: httpx.AsyncClient, ids: ServiceIds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database failure becomes a 503 error panel with Retry and no details."""

    async def broken(*_args: object, **_kwargs: object) -> list[object]:
        raise OperationalError("select", {}, Exception("secret detail"))

    await sign_in(web_client, "root")
    monkeypatch.setattr(service_detail_web, "load_environment_rows", broken)
    response = await web_client.get(f"/services/{ids.mysql}/environments", headers=HTMX)
    assert response.status_code == 503
    assert 'data-fragment="inline-error"' in response.text and 'role="alert"' in response.text
    assert "Could not load environments" in response.text
    assert f'hx-get="/services/{ids.mysql}/environments"' in response.text
    assert response.text.count("hx-status:503") == 2
    assert "secret detail" not in response.text


async def test_page_links_lead_back_to_the_structure(
    web_client: httpx.AsyncClient, ids: ServiceIds
) -> None:
    """The mobile back link and breadcrumb point at the deployment and project."""
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/services/{ids.mysql}")).text
    back_link = r'<nav aria-label="Breadcrumb" class="-mb-2 md:hidden"><a href="/deployments/\d+"'
    assert re.search(back_link, page)
    assert "Fancy Proj. Name / prod" in page
    assert re.search(r'<li><a href="/projects/\d+"[^>]*>Fancy Proj. Name</a></li>', page)
    assert re.search(r'<li><a href="/deployments/\d+"[^>]*>prod</a></li>', page)
