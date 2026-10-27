"""Deployment tabs and fragments on the seeded application, by role and scope."""

import re
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import Deployment
from app.tests.conftest import sign_in
from app.tests.web_helpers import (
    HTMX,
    breadcrumb_of,
    current_section,
    item_header_of,
    main_of,
    section_links,
)

ALL_TABS = ["Overview", "Files", "History", "Settings", "AccessSuperadmin"]
TAB_PATHS = ("", "/files", "/activity", "/settings")


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
    """Service names in the rows of the services table of the Files tab."""
    return re.findall(r'data-fragment="service-row"><th[^>]*><a [^>]*>([^<]+)</a>', page)


def summary_names(page: str) -> list[str]:
    """Service names in the rows of the services summary of the Overview."""
    return re.findall(r'data-fragment="service-summary-row"><th[^>]*><a [^>]*>([^<]+)</a>', page)


def state_cell(page: str) -> str:
    """The text of the State cell of the status strip."""
    cell = re.search(r">State</p>(.*?)</div></div>", page, re.DOTALL)
    assert cell is not None
    return " ".join(re.sub(r"<[^>]+>", " ", cell.group(1)).split())


async def get_tab(client: httpx.AsyncClient, deployment: int, path: str) -> str:
    """The page of one tab as the signed in user gets it."""
    response = await client.get(f"/deployments/{deployment}{path}")
    assert response.status_code == 200, (path, response.status_code)
    return response.text


async def test_the_page_top_has_the_title_and_the_path_only_on_every_tab(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The header carries no badges and no actions; the breadcrumb names the item types."""
    await sign_in(web_client, "root")
    for path in (*TAB_PATHS, "/access"):
        page = await get_tab(web_client, ids.prod, path)
        header = item_header_of(page)
        assert (header.type, header.title) == ("deployment", "Deployment: prod"), path
        assert header.subtitle is None and header.details is None, path
        assert dict(header.location) == {"root": "Deployment path: /opt/fancy/prod"}, path
        markup = page.split('data-fragment="page-header"', 1)[1].split("</section>", 1)[0]
        assert "<button" not in markup and "<a " not in markup and "badge" not in markup, path
        crumbs = breadcrumb_of(page)
        assert [(crumb.label, crumb.type) for crumb in crumbs] == [
            ("Projects", None),
            ("Fancy Proj. Name", "Project"),
            ("prod", "Deployment"),
        ]
        assert re.fullmatch(r"/projects/\d+", crumbs[1].href or "")
        assert crumbs[2].href is None


async def test_each_tab_marks_itself_as_the_current_page(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The tabs are links to real pages and the open one carries aria-current."""
    await sign_in(web_client, "root")
    base = f"/deployments/{ids.prod}"
    for path in (*TAB_PATHS, "/access"):
        page = await get_tab(web_client, ids.prod, path)
        assert section_links(page) == ALL_TABS, path
        assert current_section(page) == base + path, path
        assert 'aria-current="page"' in page
        assert "#services" not in page and "#hist" not in page
    for path, label in (("/files", "Files"), ("/activity", "History"), ("/settings", "Settings")):
        page = await get_tab(web_client, ids.prod, path)
        assert f'href="{base}{path}"' in page and label in section_links(page)


async def test_the_tabs_follow_what_the_caller_sees(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Access is the superadmin's; Settings needs the whole deployment."""
    for user, tabs in (
        ("admin", ["Overview", "Files", "History", "Settings"]),
        ("deployment_admin", ["Overview", "Files", "History", "Settings"]),
        ("viewer", ["Overview", "Files", "History", "Settings"]),
        ("env_scoped_admin", ["Overview", "Files", "History"]),
    ):
        await sign_in(web_client, user)
        for path in ("", "/files", "/activity"):
            assert section_links(await get_tab(web_client, ids.prod, path)) == tabs, (user, path)


async def test_superadmin_overview_has_the_status_strip_services_and_latest_deploys(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The strip carries the state flags, the services are read only and the history is cut."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{ids.prod}")
    page = response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"
    assert ">Failed<" in page
    assert state_cell(page) == "Failed"
    for status_cell in ("State", "Last deploy", "Drift state", "Dist check", "Post-deploy"):
        assert f">{status_cell}</p>" in page
    assert page.count("Not checked yet") == 2
    assert summary_names(page) == ["mysql", "web"]
    assert "Environments in your scope" not in page
    assert 'data-fragment="service-row"' not in page
    for absent in ("/services/new", "/edit", "/dist", "import-compose", "Add service"):
        assert absent not in page
    assert 'href="/services/' in page
    assert f'href="/deployments/{ids.prod}/activity"' in page
    latest = re.findall(r'data-fragment="recent-deploy-row"', page)
    history = await get_tab(web_client, ids.prod, "/activity")
    assert 1 <= len(latest) == min(5, history.count('data-fragment="history-row"'))
    for moved in ("policies-panel", "compose-files", 'id="hist"', 'id="services"'):
        assert moved not in page
    assert "deploy-finished" not in page


async def test_viewer_and_admin_see_the_same_read_only_overview(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """No structure or deploy action on the Overview, whoever looks at it."""
    for user in ("admin", "viewer"):
        await sign_in(web_client, user)
        page = await get_tab(web_client, ids.prod, "")
        assert summary_names(page) == ["mysql", "web"]
        for absent in ("/edit", "/services/new", "/dist", "Check dist", "#deploy"):
            assert absent not in page, (user, absent)


async def test_environment_grant_gets_a_name_only_overview_with_a_note(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Only the granted file's service shows: no path and no other names."""
    await sign_in(web_client, "env_scoped_admin")
    page = await get_tab(web_client, ids.prod, "")
    body = main_of(page)
    assert (
        "You have access to one environment of this deployment. Only its service is shown." in body
    )
    assert "Environments in your scope" in body
    assert summary_names(page) == ["mysql"]
    for hidden in ("web</a>", "/opt/fancy", "Policies", ".env.web"):
        assert hidden not in body
    assert not item_header_of(body).location
    assert breadcrumb_of(page)[1].label == "Fancy Proj. Name"


async def test_files_tab_gives_the_superadmin_the_structure_actions(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Check dist, the services with their actions, the dialogs and the live refresh."""
    await sign_in(web_client, "root")
    page = await get_tab(web_client, ids.prod, "/files")
    assert service_names(page) == ["mysql", "web"]
    assert "Environments in your scope" not in page
    assert f'href="/deployments/{ids.prod}/dist"' in page
    assert f'href="/deployments/{ids.prod}/services/new"' in page
    assert 'hx-trigger="deploy-finished from:body"' in page
    assert f'hx-get="/deployments/{ids.prod}/services"' in page
    assert 'id="deploy-dialog"' in page and 'id="service-delete"' in page
    assert 'data-fragment="deployment-files"' in page or "Deployment files" in page
    assert 'data-row-action="rename"' in page and 'data-row-action="delete"' in page
    assert 'data-row-action="deploy"' in page
    for elsewhere in ("compose-files", "policies-panel", 'id="hist"', "status-strip"):
        assert elsewhere not in page
    assert page.index("Check dist") < page.index('id="services"')


async def test_files_tab_of_an_admin_deploys_but_does_not_edit(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """An admin can check and deploy but has no edit, add service or delete."""
    await sign_in(web_client, "admin")
    page = await get_tab(web_client, ids.prod, "/files")
    assert f'href="/deployments/{ids.prod}/dist"' in page
    assert 'data-row-action="deploy"' in page and 'id="deploy-dialog"' in page
    for absent in ("/edit", "/services/new", 'data-row-action="rename"', "service-delete"):
        assert absent not in page


async def test_files_tab_of_a_viewer_is_read_only(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """No check, deploy or edit control for a viewer."""
    await sign_in(web_client, "viewer")
    page = await get_tab(web_client, ids.prod, "/files")
    assert service_names(page) == ["mysql", "web"]
    for absent in ("/dist", "deploy-dialog", "/edit", "/services/new", "Check dist"):
        assert absent not in page


async def test_files_tab_of_a_scoped_admin_lists_only_their_service(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The scope note and the one service in scope, with the check kept."""
    await sign_in(web_client, "env_scoped_admin")
    page = await get_tab(web_client, ids.prod, "/files")
    assert service_names(page) == ["mysql"]
    assert "Environments in your scope" in page and "web</a>" not in page
    assert "You have access to one environment of this deployment." in page
    assert "compose-files" not in page


async def test_outside_allowed_path_disables_the_check_and_says_why(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """A deployment outside the allowed paths gets a note, a disabled check and no deploy."""
    await sign_in(web_client, "root")
    for path in ("", "/files", "/activity", "/settings"):
        page = await get_tab(web_client, ids.legacy, path)
        assert "/srv/old/legacy" in page
        assert (
            "This deployment is outside the allowed paths. It cannot be deployed or checked" in page
        )
    page = await get_tab(web_client, ids.legacy, "/files")
    check = re.search(r"<button[^>]*disabled[^>]*>.*?Check dist</button>", page, re.DOTALL)
    assert check is not None
    assert "/dist" not in page
    assert "#deploy-dialog" not in page and 'id="deploy-dialog"' not in page


async def test_files_tab_shows_its_toasts_only(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Import and service delete results show on Files; the other tabs ignore them."""
    await sign_in(web_client, "root")
    base = f"/deployments/{ids.prod}"
    imported = (await web_client.get(f"{base}/files?imported=2,3,1,2,0,0")).text
    assert "Import finished" in imported and "Imported 2 services and 3 files" in imported
    deleted = (await web_client.get(f"{base}/files?deleted=service")).text
    assert "Service deleted" in deleted and "Import finished" not in deleted
    assert "Service deleted" not in (await web_client.get(f"{base}/files?deleted=other")).text
    assert "Import finished" not in (await web_client.get(f"{base}/files?imported=bad")).text
    for path in ("", "/activity", "/settings"):
        page = (await web_client.get(f"{base}{path}?imported=2,3,1,2,0,0&deleted=service")).text
        assert "Import finished" not in page and "Service deleted" not in page, path


async def test_history_tab_has_the_panel_and_its_refresh(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The deploy history of the superadmin, with the environments and the output detail."""
    await sign_in(web_client, "root")
    page = await get_tab(web_client, ids.prod, "/activity")
    assert 'id="hist"' in page and "Deploy history across services" in page
    assert 'hx-trigger="deploy-finished from:body"' in page
    assert f'hx-get="/deployments/{ids.prod}/history"' in page
    assert ".env.db v2, .env.db.secrets v1" in page
    assert "Post-deploy exit code 0, output truncated." in page
    for elsewhere in ("service-row", "policies-panel", "status-strip", "compose-files"):
        assert elsewhere not in page


async def test_history_tab_hides_the_output_from_a_viewer_and_out_of_scope_runs(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """A viewer sees runs without the exit code; a scoped admin only the runs of their file."""
    await sign_in(web_client, "viewer")
    viewer = await get_tab(web_client, ids.prod, "/activity")
    assert 'data-fragment="history-row"' in viewer and "exit code" not in viewer
    await sign_in(web_client, "env_scoped_admin")
    scoped = main_of(await get_tab(web_client, ids.prod, "/activity"))
    assert ".env.db v2" in scoped and "Post-deploy exit code n/a, output truncated." in scoped
    for hidden in (".env.db.secrets", ".env.web"):
        assert hidden not in scoped


async def test_settings_tab_of_the_superadmin_edits_and_deletes(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Policies with Edit policies, the Compose files panel, Edit deployment and the delete zone."""
    await sign_in(web_client, "root")
    page = await get_tab(web_client, ids.prod, "/settings")
    assert 'data-fragment="policies-panel"' in page
    assert "Edit policies" in page and "Read-only" not in page
    assert page.count(f'href="/deployments/{ids.prod}/edit"') == 2
    assert "Edit deployment" in page
    assert "0600, www-data:www-data" in page
    assert 'data-fragment="compose-files"' in page and 'id="compose-link-open"' in page
    assert 'id="deployment-delete-open"' in page
    assert f'hx-get="/deployments/{ids.prod}/delete"' in page
    assert 'id="deployment-delete"' in page
    for elsewhere in ("status-strip", 'id="services"', 'id="hist"', "deploy-dialog"):
        assert elsewhere not in page


async def test_settings_tab_is_read_only_for_admins_and_viewers(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The policies show without any edit, Compose link or delete control."""
    for user in ("admin", "viewer"):
        await sign_in(web_client, user)
        page = await get_tab(web_client, ids.prod, "/settings")
        assert "Read-only" in page and "0600, www-data:www-data" in page, user
        for absent in (
            "/edit",
            "Edit policies",
            "Edit deployment",
            "compose-link",
            "deployment-delete",
            "Delete deployment",
        ):
            assert absent not in page, (user, absent)


async def test_settings_tab_needs_the_whole_deployment(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """A caller who reaches the deployment through one environment gets 403, a stranger 404."""
    await sign_in(web_client, "env_scoped_admin")
    assert (await web_client.get(f"/deployments/{ids.prod}/settings")).status_code == 403
    await sign_in(web_client, "viewer")
    assert (await web_client.get(f"/deployments/{ids.main}/settings")).status_code == 404


async def test_settings_tab_shows_its_toasts_only(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """The saved and Compose link results show on Settings and nowhere else."""
    await sign_in(web_client, "root")
    base = f"/deployments/{ids.prod}"
    saved = (await web_client.get(f"{base}/settings?saved=true")).text
    assert "Deployment saved" in saved and "The changes to prod are saved." in saved
    linked = (await web_client.get(f"{base}/settings?compose_linked=2,1")).text
    assert "Compose files saved" in linked and "2 Compose files linked." in linked
    assert (
        "Compose files saved"
        not in (await web_client.get(f"{base}/settings?compose_linked=x")).text
    )
    assert "Deployment saved" not in (await web_client.get(f"{base}/settings")).text
    for path in ("", "/files", "/activity"):
        page = (await web_client.get(f"{base}{path}?saved=true&compose_linked=2,1")).text
        assert "Deployment saved" not in page and "Compose files saved" not in page, path


async def test_deployments_outside_the_scope_are_not_found(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Out of scope and nonexistent deployments answer the same 404 on every tab and fragment."""
    await sign_in(web_client, "viewer")
    for suffix in (*TAB_PATHS, "/services", "/history"):
        assert (await web_client.get(f"/deployments/{ids.main}{suffix}")).status_code == 404
        assert (await web_client.get(f"/deployments/9999{suffix}")).status_code == 404


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


async def test_empty_deployment_shows_empty_states_on_each_tab(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: DeploymentIds
) -> None:
    """Without services or runs the panels say so, and only the superadmin may add a service."""
    async with web_app.state.session_factory() as db:
        project_id = (
            await db.execute(select(Deployment.project_id).where(Deployment.id == ids.prod))
        ).scalar_one()
        empty = Deployment(project_id=project_id, name="empty", root_path="/opt/fancy/empty")
        db.add(empty)
        await db.commit()
        empty_id = empty.id
    await sign_in(web_client, "root")
    overview = await get_tab(web_client, empty_id, "")
    assert overview.count("No services yet") == 1 and overview.count("No deploys yet") == 2
    assert "/services/new" not in overview
    assert "No flags" in state_cell(overview) and overview.count("Never deployed") == 1
    files = await get_tab(web_client, empty_id, "/files")
    assert "No services yet" in files
    assert files.count(f'href="/deployments/{empty_id}/services/new"') == 2
    history = await get_tab(web_client, empty_id, "/activity")
    assert "No deploys yet" in history
    await sign_in(web_client, "admin")
    files = await get_tab(web_client, empty_id, "/files")
    assert "No services yet" in files and "/services/new" not in files


async def test_anonymous_requests_are_refused(
    web_client: httpx.AsyncClient, ids: DeploymentIds
) -> None:
    """Every deployment route needs a session."""
    for suffix in (*TAB_PATHS, "/services", "/history", "/access"):
        response = await web_client.get(f"/deployments/{ids.prod}{suffix}", follow_redirects=False)
        assert response.status_code in (303, 401), suffix
