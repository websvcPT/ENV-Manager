"""Environment delete on the web application, by role."""

import re

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, Environment
from app.tests.conftest import sign_in
from app.tests.settings_helpers import browser_as
from app.tests.web_helpers import HTMX, environment_ids, post

OTHERS = ("admin", "viewer", "project_admin", "deployment_admin", "env_scoped_admin")


async def stored(web_app: FastAPI, environment_id: int) -> Environment | None:
    """A stored environment, or None when it was deleted."""
    async with web_app.state.session_factory() as db:
        found = await db.get(Environment, environment_id)
    return found if isinstance(found, Environment) else None


async def entries(web_app: FastAPI) -> list[AuditEntry]:
    """The environment delete audit entries, oldest first."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry)
            .where(AuditEntry.action == "environment.delete")
            .order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def test_the_superadmin_gets_the_delete_controls_and_nobody_else(web_app: FastAPI) -> None:
    ids = await environment_ids(web_app)
    editor = await stored(web_app, ids.editor)
    assert editor is not None
    async with browser_as(web_app, "root") as http:
        page = (await http.get(f"/environments/{ids.editor}")).text
        service = (await http.get(f"/services/{editor.service_id}")).text
    assert 'id="environment-delete-open"' in page and 'id="environment-delete"' in page
    assert re.search(r'data-row-action="delete"', service)
    assert 'id="environment-delete"' in service
    for user in OTHERS:
        async with browser_as(web_app, user) as http:
            response = await http.get(f"/environments/{ids.editor}")
            if response.status_code == 200:
                for absent in ("environment-delete-open", 'id="environment-delete"'):
                    assert absent not in response.text, (user, absent)
            assert (
                await http.get(f"/environments/{ids.editor}/delete", headers=HTMX)
            ).status_code in (403, 404), user
            refused = await post(
                http, f"/environments/{ids.editor}/delete", {"confirm_name": editor.name}
            )
            assert refused.status_code in (403, 404), user
    assert await stored(web_app, ids.editor) is not None
    assert await entries(web_app) == []


async def test_anonymous_visitors_are_sent_to_sign_in(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    ids = await environment_ids(web_app)
    url = f"/environments/{ids.editor}/delete"
    assert (await web_client.get(url, follow_redirects=False)).status_code in (303, 401)


async def test_the_dialog_asks_for_the_name_and_a_wrong_one_deletes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    ids = await environment_ids(web_app)
    await sign_in(web_client, "root")
    dialog = (await web_client.get(f"/environments/{ids.tuning}/delete", headers=HTMX)).text
    assert 'data-fragment="environment-delete"' in dialog and "Type the file name" in dialog
    assert "This cannot be undone." in dialog and "Delete file .env.web.tuning" in dialog
    response = await post(web_client, f"/environments/{ids.tuning}/delete", {"confirm_name": "no"})
    assert response.status_code == 422
    assert "Type the file name exactly to confirm." in response.text
    assert 'value="no"' in response.text
    assert await stored(web_app, ids.tuning) is not None
    assert await entries(web_app) == []


async def test_a_confirmed_delete_leaves_for_the_service_with_a_toast(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    ids = await environment_ids(web_app)
    environment = await stored(web_app, ids.tuning)
    assert environment is not None
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/environments/{ids.tuning}/delete", {"confirm_name": " .env.web.tuning "}
    )
    target = f"/services/{environment.service_id}?deleted=environment"
    assert response.status_code == 200 and response.headers["hx-redirect"] == target
    assert await stored(web_app, ids.tuning) is None
    assert "File deleted" in (await web_client.get(target)).text
    assert (await web_client.get(f"/environments/{ids.tuning}")).status_code == 404
    (entry,) = await entries(web_app)
    assert entry.details["target_path"] == environment.target_path


async def test_a_file_with_a_restricted_token_is_not_deleted_and_says_which(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    ids = await environment_ids(web_app)
    await sign_in(web_client, "root")
    url = f"/environments/{ids.api}/delete"
    response = await post(web_client, url, {"confirm_name": ".env.api"})
    assert response.status_code == 422
    assert "billing-ci" in response.text and "Revoke them first." in response.text
    assert await stored(web_app, ids.api) is not None
    (entry,) = await entries(web_app)
    assert entry.result.value == "blocked"


async def test_unknown_files_are_not_found(web_client: httpx.AsyncClient) -> None:
    await sign_in(web_client, "root")
    assert (await web_client.get("/environments/9999/delete", headers=HTMX)).status_code == 404
    refused = await post(web_client, "/environments/9999/delete", {"confirm_name": "x"})
    assert refused.status_code == 404
