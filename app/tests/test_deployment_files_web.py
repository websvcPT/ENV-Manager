"""A deployment's own files: created on first use, shown apart from the services, never renamed
or deleted on their own, and managed on a page of their own."""

import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import DEPLOYMENT_FILES_NAME, AuditEntry, Deployment, Environment, Service
from app.services.services import DEPLOYMENT_FILES_MESSAGE
from app.tests.conftest import sign_in
from app.tests.web_helpers import breadcrumb_of, main_of, post


@pytest.fixture
async def prod(web_app: FastAPI) -> int:
    """The id of the seeded prod deployment."""
    async with web_app.state.session_factory() as db:
        query = select(Deployment.id).where(Deployment.name == "prod")
        deployment_id: int = (await db.execute(query)).scalar_one()
        return deployment_id


async def open_files(web_client: httpx.AsyncClient, deployment_id: int) -> int:
    """Create the deployment files through the panel and return the id of their service."""
    response = await post(web_client, f"/deployments/{deployment_id}/deployment-files", {})
    assert response.status_code == 200
    found = re.fullmatch(r"/services/(\d+)/environments/new", response.headers["hx-redirect"])
    assert found is not None
    return int(found.group(1))


async def add_file(
    web_client: httpx.AsyncClient, web_app: FastAPI, files_id: int, name: str
) -> int:
    """Add a file with pasted content to the deployment files and return its id."""
    response = await post(
        web_client,
        f"/services/{files_id}/environments/new",
        {
            "service": str(files_id),
            "name": name,
            "target_path": name,
            "source": "paste",
            "content": "STACK=prod\n",
        },
    )
    assert response.status_code == 200, response.text

    async with web_app.state.session_factory() as db:
        query = select(Environment.id).where(
            Environment.service_id == files_id, Environment.name == name
        )
        environment_id: int = (await db.execute(query)).scalar_one()
    return environment_id


async def test_the_panel_offers_to_add_the_first_file_and_creates_the_files_once(
    web_client: httpx.AsyncClient, web_app: FastAPI, prod: int
) -> None:
    await sign_in(web_client, "root")
    page = main_of((await web_client.get(f"/deployments/{prod}/files")).text)
    panel = re.search(r'<section[^>]*data-fragment="deployment-files".*?</section>', page, re.S)
    assert panel is not None and "No deployment files yet" in panel.group(0)
    assert f'hx-post="/deployments/{prod}/deployment-files"' in panel.group(0)
    assert page.index('data-fragment="services-table"') < page.index(
        'data-fragment="deployment-files"'
    )
    first = await open_files(web_client, prod)
    again = await open_files(web_client, prod)
    assert first == again
    async with web_app.state.session_factory() as db:
        service = await db.get(Service, first)
        assert service is not None and service.name == DEPLOYMENT_FILES_NAME
        created = select(func.count(AuditEntry.id)).where(
            AuditEntry.action == "service.create", AuditEntry.target_id == first
        )
        assert (await db.execute(created)).scalar_one() == 1


async def test_the_files_are_never_listed_as_a_service(
    web_client: httpx.AsyncClient, web_app: FastAPI, prod: int
) -> None:
    await sign_in(web_client, "root")
    files_id = await open_files(web_client, prod)
    await add_file(web_client, web_app, files_id, ".env")
    page = main_of((await web_client.get(f"/deployments/{prod}/files")).text)
    services = re.search(r'<section[^>]*data-fragment="services-table".*?</section>', page, re.S)
    assert services is not None and DEPLOYMENT_FILES_NAME not in services.group(0)
    files = re.search(r'<section[^>]*data-fragment="deployment-files".*?</section>', page, re.S)
    assert files is not None
    assert "1 file" in files.group(0) and f'href="/services/{files_id}"' in files.group(0)
    assert f'hx-post="/services/{files_id}/deploy/preflight"' in files.group(0)


async def test_the_files_page_reads_as_deployment_files_and_has_no_rename_or_delete(
    web_client: httpx.AsyncClient, web_app: FastAPI, prod: int
) -> None:
    await sign_in(web_client, "root")
    files_id = await open_files(web_client, prod)
    await add_file(web_client, web_app, files_id, ".env")
    page = main_of((await web_client.get(f"/services/{files_id}")).text)
    assert f"<span data-item-name>{DEPLOYMENT_FILES_NAME}</span>" in page
    assert "Service:" not in page
    assert "service-rename-open" not in page and "service-delete-open" not in page
    assert "Deploy deployment files" in page and "Add file" in page
    rename = await post(web_client, f"/services/{files_id}/rename", {"name": "renamed"})
    assert rename.status_code == 422 and DEPLOYMENT_FILES_MESSAGE in rename.text
    delete = await post(
        web_client, f"/services/{files_id}/delete", {"confirm_name": DEPLOYMENT_FILES_NAME}
    )
    assert delete.status_code == 422 and DEPLOYMENT_FILES_MESSAGE in delete.text


async def test_a_file_page_names_the_deployment_files_where_a_service_would_be(
    web_client: httpx.AsyncClient, web_app: FastAPI, prod: int
) -> None:
    await sign_in(web_client, "root")
    files_id = await open_files(web_client, prod)
    environment_id = await add_file(web_client, web_app, files_id, ".env")
    page = (await web_client.get(f"/environments/{environment_id}")).text
    assert [(crumb.label, crumb.type) for crumb in breadcrumb_of(page)] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("prod", "Deployment"),
        (DEPLOYMENT_FILES_NAME, None),
        (".env", "Environment"),
    ]
    async with web_app.state.session_factory() as db:
        environment = await db.get(Environment, environment_id)
        assert environment is not None and environment.service_id == files_id


@pytest.mark.parametrize("user", ["admin", "viewer", "deployment_admin"])
async def test_only_the_superadmin_creates_the_files(
    web_client: httpx.AsyncClient, prod: int, user: str
) -> None:
    await sign_in(web_client, user)
    response = await post(web_client, f"/deployments/{prod}/deployment-files", {})
    assert response.status_code in (403, 404)
    page = (await web_client.get(f"/deployments/{prod}/files")).text
    assert 'data-fragment="deployment-files"' not in page


async def test_the_access_grid_names_the_files_without_calling_them_a_service(
    web_client: httpx.AsyncClient, web_app: FastAPI, prod: int
) -> None:
    await sign_in(web_client, "root")
    files_id = await open_files(web_client, prod)
    async with web_app.state.session_factory() as db:
        project_id: int = (
            await db.execute(select(Deployment.project_id).where(Deployment.id == prod))
        ).scalar_one()
    body = (await web_client.get(f"/access/grid/project/{project_id}")).text
    row = re.search(rf'<tr[^>]*service-{files_id}".*?</th>', body, re.S)
    assert row is not None, body[:500]
    assert DEPLOYMENT_FILES_NAME in row.group(0) and ">Service<" not in row.group(0)
