"""The Compose files panel of a deployment: linking, the read only view, who sees what."""

import html
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import AllowedPath, AuditEntry, Deployment, Project
from app.tests.compose_helpers import GEOTRAVEL, write_project
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, main_of, post

COMPOSE = "docker-compose.yml"
INLINE = "services:\n  web:\n    image: x\n    environment:\n      NOTE: <b>bold</b>\n"


@dataclass(frozen=True)
class Geo:
    """A deployment of the fancy project on a real GeoTravel folder, and that folder."""

    id: int
    root: Path


@pytest.fixture
async def geo(web_app: FastAPI, tmp_path: Path) -> Geo:
    """Allow a temporary folder and put a deployment with no Compose files linked on it."""
    host = (tmp_path / "vhosts").resolve()
    root = host / "geotravel"
    root.mkdir(parents=True)
    write_project(root, {**GEOTRAVEL, "docker-compose.override.yml": INLINE})
    async with web_app.state.session_factory() as db:
        db.add(AllowedPath(path=str(host)))
        project = (
            await db.execute(select(Project.id).where(Project.name == "Fancy Proj. Name"))
        ).scalar_one()
        deployment = Deployment(project_id=project, name="Geo Travel", root_path=str(root))
        db.add(deployment)
        await db.commit()
        return Geo(deployment.id, root)


def panel_of(page: str) -> str:
    """The Compose files panel of a page, empty when it is not there."""
    found = re.search(r'<section[^>]*data-fragment="compose-files".*?</section>', page, re.S)
    return found.group(0) if found else ""


async def link(web_client: httpx.AsyncClient, geo: Geo, *paths: str) -> httpx.Response:
    """Link Compose files through the dialog as the signed-in superadmin."""
    return await post(
        web_client, f"/deployments/{geo.id}/compose-files/link", {"compose_file": list(paths)}
    )


async def views(web_app: FastAPI) -> int:
    """How many Compose file views were audited."""
    async with web_app.state.session_factory() as db:
        query = select(func.count(AuditEntry.id)).where(
            AuditEntry.action == "deployment.compose_view"
        )
        count: int = (await db.execute(query)).scalar_one()
        return count


async def test_the_superadmin_links_the_compose_files_from_the_panel(
    web_client: httpx.AsyncClient, web_app: FastAPI, geo: Geo
) -> None:
    await sign_in(web_client, "root")
    panel = panel_of(main_of((await web_client.get(f"/deployments/{geo.id}")).text))
    assert "No Compose files linked" in panel
    assert f'hx-get="/deployments/{geo.id}/compose-files/link"' in panel
    dialog = (await web_client.get(f"/deployments/{geo.id}/compose-files/link", headers=HTMX)).text
    offers = re.findall(r'name="compose_file" value="([^"]+)"[^>]*?( checked)?[ >]', dialog)
    assert [path for path, _ in offers] == [COMPOSE, "docker-compose.override.yml"]
    assert all(checked == "" for _, checked in offers)

    done = await link(web_client, geo, "docker-compose.override.yml", COMPOSE)
    assert done.status_code == 200
    assert done.headers["hx-redirect"] == f"/deployments/{geo.id}?compose_linked=2,0"
    page = (await web_client.get(done.headers["hx-redirect"])).text
    assert "2 Compose files linked." in page
    rows = re.findall(r'data-fragment="compose-file-row" data-file="([^"]+)"', page)
    assert rows == [COMPOSE, "docker-compose.override.yml"]
    assert "Change links" in panel_of(main_of(page))
    again = (await web_client.get(f"/deployments/{geo.id}/compose-files/link", headers=HTMX)).text
    assert re.search(rf'value="{re.escape(COMPOSE)}"[^>]*checked', again)

    unlinked = await link(web_client, geo)
    assert unlinked.headers["hx-redirect"] == f"/deployments/{geo.id}?compose_linked=0,0"
    page = (await web_client.get(unlinked.headers["hx-redirect"])).text
    assert "Compose files unlinked." in page and "No Compose files linked" in page


async def test_a_path_that_cannot_be_linked_keeps_the_dialog_with_the_reason(
    web_client: httpx.AsyncClient, web_app: FastAPI, geo: Geo
) -> None:
    await sign_in(web_client, "root")
    refused = await post(
        web_client,
        f"/deployments/{geo.id}/compose-files/link",
        {"compose_file": [COMPOSE], "extra_path": "../elsewhere.yml"},
    )
    assert refused.status_code == 422
    assert 'data-fragment="compose-link"' in refused.text
    assert "The Compose files cannot be linked" in refused.text
    assert 'value="../elsewhere.yml"' in refused.text
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, geo.id)
        assert deployment is not None and deployment.compose_files is None


async def test_an_admin_over_the_deployment_reads_a_compose_file_as_escaped_text(
    web_client: httpx.AsyncClient, web_app: FastAPI, geo: Geo
) -> None:
    await sign_in(web_client, "root")
    await link(web_client, geo, COMPOSE, "docker-compose.override.yml")
    await sign_in(web_client, "admin")
    panel = panel_of(main_of((await web_client.get(f"/deployments/{geo.id}")).text))
    assert f'hx-get="/deployments/{geo.id}/compose-files/1"' in panel
    assert "compose-link-open" not in panel
    shown = await web_client.get(f"/deployments/{geo.id}/compose-files/1", headers=HTMX)
    assert shown.status_code == 200
    assert "<b>bold</b>" not in shown.text and html.escape("<b>bold</b>") in shown.text
    assert "hx-ignore" in shown.text and "docker-compose.override.yml" in shown.text
    assert await views(web_app) == 1
    missing = await web_client.get(f"/deployments/{geo.id}/compose-files/5", headers=HTMX)
    assert missing.status_code == 422 and "not linked" in missing.text
    assert await views(web_app) == 1


@pytest.mark.parametrize(("user", "code"), [("viewer", 403), ("deployment_admin", 404)])
async def test_who_cannot_read_them_does_not_see_the_panel_or_the_text(
    web_client: httpx.AsyncClient, web_app: FastAPI, geo: Geo, user: str, code: int
) -> None:
    await sign_in(web_client, "root")
    await link(web_client, geo, COMPOSE)
    await sign_in(web_client, user)
    page = await web_client.get(f"/deployments/{geo.id}")
    if page.status_code == 200:
        assert panel_of(main_of(page.text)) == ""
    shown = await web_client.get(f"/deployments/{geo.id}/compose-files/0", headers=HTMX)
    assert shown.status_code == code
    assert await views(web_app) == 0


@pytest.mark.parametrize("user", ["admin", "viewer"])
async def test_only_the_superadmin_links_them(
    web_client: httpx.AsyncClient, geo: Geo, user: str
) -> None:
    await sign_in(web_client, user)
    dialog = await web_client.get(f"/deployments/{geo.id}/compose-files/link", headers=HTMX)
    assert dialog.status_code == 403
    refused = await link(web_client, geo, COMPOSE)
    assert refused.status_code == 403


async def test_the_deployment_files_page_shows_them_read_only(
    web_client: httpx.AsyncClient, web_app: FastAPI, geo: Geo
) -> None:
    await sign_in(web_client, "root")
    await link(web_client, geo, COMPOSE)
    made = await post(web_client, f"/deployments/{geo.id}/deployment-files", {})
    files_id = int(made.headers["hx-redirect"].split("/")[2])
    panel = panel_of(main_of((await web_client.get(f"/services/{files_id}")).text))
    assert f'data-file="{COMPOSE}"' in panel
    assert f'hx-get="/deployments/{geo.id}/compose-files/0"' in panel
    assert "compose-link-open" not in panel


async def test_linking_a_file_the_deployment_files_hold_removes_it_and_says_so(
    web_client: httpx.AsyncClient, web_app: FastAPI, geo: Geo
) -> None:
    await sign_in(web_client, "root")
    made = await post(web_client, f"/deployments/{geo.id}/deployment-files", {})
    files_id = int(made.headers["hx-redirect"].split("/")[2])
    added = await post(
        web_client,
        f"/services/{files_id}/environments/new",
        {"service": str(files_id), "name": COMPOSE, "target_path": COMPOSE, "source": "import"},
    )
    assert added.status_code == 200, added.text
    done = await link(web_client, geo, COMPOSE)
    assert done.headers["hx-redirect"] == f"/deployments/{geo.id}?compose_linked=1,1"
    page = (await web_client.get(done.headers["hx-redirect"])).text
    assert "1 Compose file linked. 1 file removed from the deployment files." in page
    assert (geo.root / COMPOSE).read_text() == GEOTRAVEL[COMPOSE]
