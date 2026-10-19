"""Service rename and delete and deployment delete on the web application, by role."""

import re
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, Deployment, Project, Service
from app.tests.conftest import sign_in
from app.tests.settings_helpers import browser_as
from app.tests.web_helpers import HTMX, post

OTHERS = ("admin", "viewer", "project_admin", "deployment_admin", "env_scoped_admin")


@dataclass(frozen=True)
class Ids:
    """Ids of the seeded rows these tests act on."""

    fancy: int
    billing: int
    prod: int
    billing_main: int
    billing_legacy: int
    web: int
    mysql: int
    api: int
    cron: int


@pytest.fixture
async def ids(web_app: FastAPI) -> Ids:
    """The seeded ids of the web application."""
    async with web_app.state.session_factory() as db:
        projects = dict((await db.execute(select(Project.name, Project.id))).all())
        deployments = dict((await db.execute(select(Deployment.name, Deployment.id))).all())
        services = {
            (deployment, service): service_id
            for deployment, service, service_id in (
                await db.execute(
                    select(Deployment.name, Service.name, Service.id).join(
                        Service, Service.deployment_id == Deployment.id
                    )
                )
            ).all()
        }
    return Ids(
        fancy=projects["Fancy Proj. Name"],
        billing=projects["billing"],
        prod=deployments["prod"],
        billing_main=deployments["main"],
        billing_legacy=deployments["legacy"],
        web=services[("prod", "web")],
        mysql=services[("prod", "mysql")],
        api=services[("main", "api")],
        cron=services[("legacy", "cron")],
    )


async def stored(web_app: FastAPI, model: type, item_id: int) -> Any:
    """A stored row, or None when it was deleted."""
    async with web_app.state.session_factory() as db:
        return await db.get(model, item_id)


async def entries(web_app: FastAPI, action: str) -> list[AuditEntry]:
    """The audit entries of one action, oldest first."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


def row_keys(page: str, marker: str, name: str) -> list[str]:
    """The row action keys of the row of ``name`` in a table whose rows carry ``marker``."""
    pattern = rf'data-fragment="{marker}"><th[^>]*><a [^>]*>{re.escape(name)}</a>(.*?)</tr>'
    row = re.search(pattern, page, re.DOTALL)
    assert row is not None, name
    return re.findall(r'data-row-action="([a-z-]+)"', row.group(1))


async def test_the_superadmin_gets_the_structure_actions_and_nobody_else(
    web_app: FastAPI, ids: Ids
) -> None:
    async with browser_as(web_app, "root") as http:
        deployment = (await http.get(f"/deployments/{ids.prod}")).text
        project = (await http.get(f"/projects/{ids.fancy}")).text
        service = (await http.get(f"/services/{ids.web}")).text
    assert row_keys(deployment, "service-row", "web")[-2:] == ["rename", "delete"]
    assert row_keys(project, "deployment-row", "prod")[-1] == "delete"
    assert 'id="deployment-delete-open"' in deployment
    assert 'id="service-delete"' in deployment and 'id="deployment-delete"' in deployment
    assert 'id="deployment-delete"' in project
    assert 'id="service-rename-open"' in service and 'id="service-delete-open"' in service
    assert 'id="service-delete"' in service
    for user in OTHERS:
        async with browser_as(web_app, user) as http:
            responses = [
                await http.get(path)
                for path in (
                    f"/deployments/{ids.prod}",
                    f"/projects/{ids.fancy}",
                    f"/services/{ids.web}",
                )
            ]
        for response in responses:
            if response.status_code != 200:
                continue
            page = response.text
            assert 'data-row-action="delete"' not in page, user
            assert 'data-row-action="rename"' not in page, user
            for absent in ("-delete-open", 'id="service-delete"', 'id="deployment-delete"'):
                assert absent not in page, (user, absent)
            assert "service-rename-open" not in page, user


async def test_every_other_group_is_refused_every_route_and_nothing_changes(
    web_app: FastAPI, ids: Ids
) -> None:
    for user in OTHERS:
        async with browser_as(web_app, user) as http:
            for url in (
                f"/services/{ids.web}/rename",
                f"/services/{ids.web}/delete",
                f"/deployments/{ids.prod}/delete",
            ):
                assert (await http.get(url, headers=HTMX)).status_code in (403, 404), (user, url)
            for url, data in (
                (f"/services/{ids.web}/rename", {"name": "other"}),
                (f"/services/{ids.web}/delete", {"confirm_name": "web"}),
                (f"/deployments/{ids.prod}/delete", {"confirm_name": "prod"}),
            ):
                assert (await post(http, url, data)).status_code in (403, 404), (user, url)
    assert await stored(web_app, Service, ids.web) is not None
    assert await stored(web_app, Deployment, ids.prod) is not None
    assert await entries(web_app, "service.rename") == []
    assert await entries(web_app, "service.delete") == []
    assert await entries(web_app, "deployment.delete") == []


async def test_anonymous_visitors_are_sent_to_sign_in(
    web_client: httpx.AsyncClient, ids: Ids
) -> None:
    for url in (f"/services/{ids.web}/rename", f"/services/{ids.web}/delete"):
        assert (await web_client.get(url, follow_redirects=False)).status_code in (303, 401)


async def test_the_rename_form_shows_the_name_and_a_rename_redirects_with_a_toast(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/services/{ids.web}/rename")).text
    assert ">Rename service</h1>" in page and 'value="web"' in page
    assert f'hx-post="/services/{ids.web}/rename"' in page
    response = await post(web_client, f"/services/{ids.web}/rename", {"name": " frontend "})
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/services/{ids.web}?renamed=true"
    shown = (await web_client.get(response.headers["hx-redirect"])).text
    assert "The service is now called frontend." in shown
    (entry,) = await entries(web_app, "service.rename")
    assert entry.details == {"previous_name": "web"}


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Enter a service name."),
        ("Not A Slug", "lowercase letters, numbers and dashes"),
        ("mysql", "A service named mysql already exists in this deployment."),
    ],
)
async def test_a_refused_rename_shows_the_reason_and_keeps_what_was_typed(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids, name: str, message: str
) -> None:
    await sign_in(web_client, "root")
    response = await post(web_client, f"/services/{ids.web}/rename", {"name": name})
    assert response.status_code == 422
    assert message in response.text and 'id="service-rename"' in response.text
    assert await entries(web_app, "service.rename") == []


async def test_the_service_delete_dialog_asks_for_the_name_and_a_wrong_one_deletes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    dialog = (await web_client.get(f"/services/{ids.web}/delete", headers=HTMX)).text
    assert 'data-fragment="service-delete"' in dialog and "Type the service name" in dialog
    assert "This cannot be undone." in dialog and "Delete service" in dialog
    response = await post(web_client, f"/services/{ids.web}/delete", {"confirm_name": "nope"})
    assert response.status_code == 422
    assert "Type the service name exactly to confirm." in response.text
    assert 'value="nope"' in response.text
    assert await stored(web_app, Service, ids.web) is not None
    assert await entries(web_app, "service.delete") == []


async def test_a_confirmed_service_delete_leaves_for_the_deployment_with_a_toast(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    response = await post(web_client, f"/services/{ids.cron}/delete", {"confirm_name": " cron "})
    assert response.status_code == 200
    target = f"/deployments/{ids.billing_legacy}?deleted=service"
    assert response.headers["hx-redirect"] == target
    assert await stored(web_app, Service, ids.cron) is None
    page = (await web_client.get(target)).text
    assert "Service deleted" in page
    (entry,) = await entries(web_app, "service.delete")
    assert entry.details == {"environments": 0}
    gone = await web_client.get(f"/services/{ids.cron}")
    assert gone.status_code == 404


async def test_a_service_with_a_restricted_token_is_not_deleted_and_says_which(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    response = await post(web_client, f"/services/{ids.api}/delete", {"confirm_name": "api"})
    assert response.status_code == 422
    assert "billing-ci" in response.text and "Revoke them first." in response.text
    assert await stored(web_app, Service, ids.api) is not None
    (entry,) = await entries(web_app, "service.delete")
    assert entry.result.value == "blocked"


async def test_the_deployment_delete_dialog_confirms_by_name_and_deletes(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    dialog = (await web_client.get(f"/deployments/{ids.billing_legacy}/delete", headers=HTMX)).text
    assert 'data-fragment="deployment-delete"' in dialog
    assert "Type the deployment name" in dialog and "This cannot be undone." in dialog
    wrong = await post(
        web_client, f"/deployments/{ids.billing_legacy}/delete", {"confirm_name": "main"}
    )
    assert wrong.status_code == 422 and "Type the deployment name exactly" in wrong.text
    assert await stored(web_app, Deployment, ids.billing_legacy) is not None
    response = await post(
        web_client, f"/deployments/{ids.billing_legacy}/delete", {"confirm_name": "legacy"}
    )
    target = f"/projects/{ids.billing}?deleted=deployment"
    assert response.headers["hx-redirect"] == target
    assert await stored(web_app, Deployment, ids.billing_legacy) is None
    assert "Deployment deleted" in (await web_client.get(target)).text
    assert (await web_client.get(f"/deployments/{ids.billing_legacy}")).status_code == 404
    (entry,) = await entries(web_app, "deployment.delete")
    assert entry.details["services"] == 1


async def test_a_deployment_with_a_restricted_token_is_not_deleted_and_says_which(
    web_client: httpx.AsyncClient, web_app: FastAPI, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/deployments/{ids.billing_main}/delete", {"confirm_name": "main"}
    )
    assert response.status_code == 422
    assert "billing-ci" in response.text
    assert await stored(web_app, Deployment, ids.billing_main) is not None


async def test_unknown_items_are_not_found(web_client: httpx.AsyncClient) -> None:
    await sign_in(web_client, "root")
    for url in ("/services/9999/rename", "/services/9999/delete", "/deployments/9999/delete"):
        assert (await web_client.get(url, headers=HTMX)).status_code == 404, url
    refused = await post(web_client, "/services/9999/delete", {"confirm_name": "x"})
    assert refused.status_code == 404
