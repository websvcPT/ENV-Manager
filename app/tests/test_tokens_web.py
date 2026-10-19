"""The API tokens pages and routes on the seeded application, by role.

The secret of a new token must exist only in the answer that creates it: these tests look for it
in every other response, header, audit entry and log line.
"""

import html
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import ApiToken, AuditEntry, Project, Service, User
from app.models.types import utc_now
from app.services.permissions import load_token_scope
from app.services.tokens import hash_secret
from app.tests.audit_helpers import web_audit_entries
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, assert_never_cached, main_of, post

VIEWER_LIKE = ("viewer", "env_scoped_viewer")
SECRET = re.compile(r'id="secret" readonly value="([^"]+)"')
ROW = re.compile(r'<tr data-fragment="token-row" id="token-(\d+)">(.*?)</tr>', re.DOTALL)
NAME = re.compile(r'<span class="font-mono text-\[13px\]">([^<]+)</span>')
COLUMNS = re.compile(r'<th scope="col"[^>]*>([^<]+)</th>')


@asynccontextmanager
async def other_client(web_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser, so one test can hold two signed-in users."""
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Accept": "text/html"}
    ) as http:
        yield http


def rows(page: str) -> dict[str, tuple[int, str]]:
    """Token id and row markup by token name, as the table lists them."""
    found: dict[str, tuple[int, str]] = {}
    for token_id, markup in ROW.findall(page):
        name = NAME.search(markup)
        assert name is not None
        found[html.unescape(name.group(1))] = (int(token_id), markup)
    return found


def secret_of(page: str) -> str:
    """The secret shown in the created dialog."""
    found = SECRET.search(page)
    assert found is not None
    return html.unescape(found.group(1))


async def stored(web_app: FastAPI, name: str) -> ApiToken:
    """A token by name, as stored now."""
    async with web_app.state.session_factory() as db:
        token: ApiToken = (
            await db.execute(select(ApiToken).where(ApiToken.name == name))
        ).scalar_one()
    return token


async def count_tokens(web_app: FastAPI) -> int:
    """How many tokens exist."""
    async with web_app.state.session_factory() as db:
        return len((await db.execute(select(ApiToken.id))).scalars().all())


async def user_id(web_app: FastAPI, name: str) -> int:
    """The id of a seeded user."""
    async with web_app.state.session_factory() as db:
        found: int = (await db.execute(select(User.id).where(User.name == name))).scalar_one()
    return found


async def create(http: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    """Post the create form with the defaults of its first state."""
    data: dict[str, object] = {"name": "ci-deploy", "operation": ["deploy"], **overrides}
    return await post(http, "/tokens", data)


async def service_id(web_app: FastAPI, name: str) -> int:
    """The id of the first seeded service with the given name."""
    async with web_app.state.session_factory() as db:
        ids = (
            await db.execute(select(Service.id).where(Service.name == name).order_by(Service.id))
        ).scalars()
        found = next(iter(ids))
    return int(found)


async def test_an_admin_sees_only_their_own_tokens_with_the_columns_of_the_handoff(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "admin")
    response = await web_client.get("/tokens")
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert "<h1" in body and "API tokens</h1>" in body
    assert "Your tokens. A token acts as you and never exceeds your access." in body
    assert COLUMNS.findall(body) == [
        "Name",
        "Scope",
        "Project",
        "Created",
        "Last used",
        "Expiry",
        "Status",
        "Actions",
    ]
    listed = rows(body)
    assert list(listed) == ["billing-ci"]
    token_id, markup = listed["billing-ci"]
    assert "emt_seed" in markup and "Deploy" in markup
    assert "billing / main / api / .env.api" in markup
    assert ">Never<" in markup and 'data-status="deployed"' in markup and "Active" in markup
    assert f'hx-get="/tokens/{token_id}/revoke"' in markup
    assert 'hx-get="/tokens/new"' in body and "Create token" in body
    assert 'id="token-create"' in body and 'id="token-revoke"' in body
    assert 'hx-get="/tokens/table"' in body and 'hx-trigger="token-changed from:body"' in body


async def test_the_superadmin_sees_every_token_with_its_owner(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    own = await create(web_client, name="roots")
    assert own.status_code == 200
    body = main_of((await web_client.get("/tokens")).text)
    assert "Your tokens and the tokens of other users." in body
    assert COLUMNS.findall(body)[:3] == ["Name", "Owner", "Scope"]
    listed = rows(body)
    assert list(listed) == ["roots", "billing-ci"]
    assert ">root<" in listed["roots"][1] and ">admin<" in listed["billing-ci"][1]
    assert "Revoke" in listed["billing-ci"][1]


async def test_the_table_fragment_is_only_the_panel_and_an_empty_one_offers_a_create_button(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "project_admin")
    response = await web_client.get("/tokens/table", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    assert response.text.lstrip().startswith('<section data-fragment="tokens-table"')
    assert "<html" not in response.text and "<h1" not in response.text
    assert "No API tokens" in response.text and "The secret is shown once." in response.text
    assert 'hx-get="/tokens/new"' in response.text and "billing-ci" not in response.text


@pytest.mark.parametrize("user", VIEWER_LIKE)
async def test_viewers_get_403_everywhere_and_can_create_or_revoke_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, user: str
) -> None:
    await sign_in(web_client, user)
    seeded = (await stored(web_app, "billing-ci")).id
    for url in ("/tokens", "/tokens/table", "/tokens/new", f"/tokens/{seeded}/revoke"):
        assert (await web_client.get(url)).status_code == 403, url
        assert (await web_client.get(url, headers=HTMX)).status_code == 403, url
    assert (await create(web_client)).status_code == 403
    assert (await post(web_client, f"/tokens/{seeded}/revoke")).status_code == 403
    assert (await stored(web_app, "billing-ci")).revoked_at is None
    assert await count_tokens(web_app) == 1
    assert (
        await web_audit_entries(web_app, "token.create") == []
        and await web_audit_entries(web_app, "token.revoke") == []
    )
    assert "/tokens" not in (await web_client.get("/projects")).text


async def test_a_signed_out_visitor_is_sent_to_sign_in(web_client: httpx.AsyncClient) -> None:
    response = await web_client.get("/tokens")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    refused = await web_client.post("/tokens", data={"name": "x", "operation": "deploy"})
    assert refused.status_code == 303 and refused.headers["location"].startswith("/login")


async def test_the_create_dialog_lists_the_operations_and_only_what_the_caller_reaches(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    response = await web_client.get("/tokens/new", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert 'data-fragment="token-create"' in page and 'hx-post="/tokens"' in page
    assert 'id="tn"' in page and 'placeholder="e.g. ci-deploy"' in page
    assert page.count('name="operation"') == 2
    assert re.search(r'value="deploy" checked', page)
    assert not re.search(r'value="read_last_deployment" checked', page)
    assert "Deploy a service." in page and "Read the last deploy result of a service." in page
    assert "Check dist" not in page and "placeholder and is defined later" not in page
    assert "A token never exceeds its owner's access." in page
    assert '<option value="">All of my projects</option>' in page
    assert '<optgroup label="Projects">' in page and ">billing</option>" in page
    assert ">Fancy Proj. Name / prod / mysql</option>" in page
    assert 'id="te"' in page and 'type="date"' in page and f'min="{utc_now().date()}"' in page
    assert "Create token" in page and 'data-js="dialog-close"' in page

    async with other_client(web_app) as fresh:
        await sign_in(fresh, "ungranted_admin")
        quiet = (await fresh.get("/tokens/new", headers=HTMX)).text
    assert '<option value="">All of my projects</option>' in quiet and "<optgroup" not in quiet


async def test_creating_a_token_shows_the_secret_once_and_keeps_it_out_of_everything_else(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await sign_in(web_client, "admin")
    response = await create(
        web_client,
        name="ci-deploy",
        operation=["deploy", "read_last_deployment"],
        expires=(utc_now() + timedelta(days=30)).date().isoformat(),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.headers["hx-trigger"]) == {"token-changed": True}
    page = response.text
    secret = secret_of(page)
    assert secret.startswith("envm_") and len(secret) == 53
    assert 'data-fragment="token-created"' in page and "data-wide" in page
    assert 'data-fragment="secret-once" hx-ignore' in page
    assert "Shown once. It is stored hashed and cannot be shown again." in page
    assert 'data-js="copy" data-target="secret"' in page and "Token created" in page
    assert "Usage example" in page and "Authorization: Bearer $ENVM_TOKEN" in page
    assert "http://localhost:8080/api/v1/services/SERVICE_ID/deploy" in page
    assert "/api/v1/services/SERVICE_ID/deployments/last" in page
    assert page.count(secret) == 1
    for name, value in response.headers.items():
        assert secret not in value, name

    token = await stored(web_app, "ci-deploy")
    assert token.token_hash == hash_secret(secret) and token.prefix == secret[:13]
    assert token.owner_id == await user_id(web_app, "admin")

    admin_id = await user_id(web_app, "admin")
    for url in ("/tokens", "/tokens/table", f"/users/{admin_id}"):
        other = await web_client.get(url, headers=HTMX)
        assert secret not in other.text, url
    shown = rows(main_of((await web_client.get("/tokens")).text))
    assert shown["ci-deploy"][0] == token.id and token.prefix in shown["ci-deploy"][1]
    async with web_app.state.session_factory() as db:
        every = json.dumps(
            [
                [e.action, e.actor_name, e.target_name, e.ip_address, e.details]
                for e in (await db.execute(select(AuditEntry))).scalars()
            ],
            default=str,
        )
    assert secret not in every and token.token_hash not in every
    assert secret not in caplog.text and token.token_hash not in caplog.text
    (entry,) = await web_audit_entries(web_app, "token.create")
    assert (entry.actor_name, entry.target_name) == ("admin", "ci-deploy")


async def test_the_example_names_the_service_a_restricted_token_is_limited_to(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    mysql = await service_id(web_app, "mysql")
    response = await create(
        web_client,
        name="reader",
        operation=["read_last_deployment"],
        restriction=f"service:{mysql}",
    )
    assert response.status_code == 200
    page = response.text
    assert f"/api/v1/services/{mysql}/deployments/last" in page
    assert "-X POST" not in page and "/deploy " not in page
    listed = rows(main_of((await web_client.get("/tokens")).text))
    assert "Read last deployment" in listed["reader"][1]
    assert "Fancy Proj. Name / prod / mysql" in listed["reader"][1]


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"name": ""}, "tn", "Enter a name."),
        ({"operation": []}, "to", "Choose at least one operation."),
        ({"expires": "2020-01-01"}, "te", "The expiry date must be today or later."),
        ({"expires": "someday"}, "te", "Enter a valid date."),
        ({"restriction": "project:999999"}, "tp", "Choose an item from the list."),
    ],
)
async def test_a_refused_form_is_redrawn_with_the_reason_and_the_choices_and_creates_nothing(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    overrides: dict[str, object],
    field: str,
    message: str,
) -> None:
    await sign_in(web_client, "admin")
    response = await create(web_client, **{"name": "keep-me", **overrides})
    assert response.status_code == 422
    assert_never_cached(response)
    page = response.text
    assert f'id="{field}-e"' in page and message in page
    assert 'data-fragment="token-create"' in page and "envm_" not in page
    if "name" not in overrides:
        assert 'value="keep-me"' in page
    assert await count_tokens(web_app) == 1
    assert await web_audit_entries(web_app, "token.create") == []


async def test_a_duplicate_name_is_refused_on_the_name_field(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    assert (await create(web_client, name="Same")).status_code == 200
    refused = await create(web_client, name="same")
    assert refused.status_code == 422
    assert 'id="tn-e"' in refused.text and "already have an active token" in refused.text
    assert await count_tokens(web_app) == 2


async def test_a_restriction_to_an_item_the_caller_cannot_reach_is_refused(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "project_admin")
    async with web_app.state.session_factory() as db:
        billing = (
            await db.execute(select(Project.id).where(Project.name == "billing"))
        ).scalar_one()
    response = await create(web_client, restriction=f"project:{billing}")
    assert response.status_code == 422 and 'id="tp-e"' in response.text
    assert await count_tokens(web_app) == 1


async def test_the_revoke_dialog_states_the_effect_and_posts_to_the_revoke_route(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    token_id = (await stored(web_app, "billing-ci")).id
    response = await web_client.get(f"/tokens/{token_id}/revoke", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert "Revoke token billing-ci" in page and "Owner" not in page
    assert "The token stops working immediately. Calls made with it stay in the audit log." in page
    assert f'hx-post="/tokens/{token_id}/revoke"' in page
    assert "Revoke token</button>" in page and "Cancel" in page


async def test_revoking_stops_the_token_audits_it_and_updates_the_row(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    token_id = (await stored(web_app, "billing-ci")).id
    async with web_app.state.session_factory() as db:
        token = await db.get(ApiToken, token_id)
        assert token is not None and await load_token_scope(db, token) is not None

    response = await post(web_client, f"/tokens/{token_id}/revoke")
    assert response.status_code == 204
    assert json.loads(response.headers["hx-trigger"]) == {
        "token-changed": True,
        "close-dialogs": True,
    }
    async with web_app.state.session_factory() as db:
        token = await db.get(ApiToken, token_id)
        assert token is not None and token.revoked_at is not None
        assert await load_token_scope(db, token) is None
    (entry,) = await web_audit_entries(web_app, "token.revoke")
    assert (entry.actor_name, entry.target_name) == ("admin", "billing-ci")
    assert entry.details == {"owner": "admin", "reason": "requested"}

    row = rows(main_of((await web_client.get("/tokens")).text))["billing-ci"][1]
    assert "Revoked" in row and 'data-status="never"' in row and "Revoke<" not in row

    again = await post(web_client, f"/tokens/{token_id}/revoke")
    assert again.status_code == 409
    assert "billing-ci is already revoked." in again.text and "hx-post" not in again.text
    assert json.loads(again.headers["hx-trigger"]) == {"token-changed": True}
    assert len(await web_audit_entries(web_app, "token.revoke")) == 1
    dialog = await web_client.get(f"/tokens/{token_id}/revoke", headers=HTMX)
    assert "is already revoked." in dialog.text and "hx-post" not in dialog.text


async def test_another_admins_token_is_not_found_for_them_and_stays_untouched(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "project_admin")
    token_id = (await stored(web_app, "billing-ci")).id
    missing = 999999
    for url in (f"/tokens/{token_id}/revoke", f"/tokens/{missing}/revoke"):
        assert (await web_client.get(url, headers=HTMX)).status_code == 404, url
        assert (await post(web_client, url)).status_code == 404, url
    assert (await stored(web_app, "billing-ci")).revoked_at is None
    assert await web_audit_entries(web_app, "token.revoke") == []


async def test_a_superadmin_revokes_someone_elses_token_and_the_owner_is_named(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    token_id = (await stored(web_app, "billing-ci")).id
    dialog = await web_client.get(f"/tokens/{token_id}/revoke", headers=HTMX)
    assert "Revoke token billing-ci" in dialog.text and "Owner admin" in dialog.text
    assert (await post(web_client, f"/tokens/{token_id}/revoke")).status_code == 204
    (entry,) = await web_audit_entries(web_app, "token.revoke")
    assert (entry.actor_name, entry.details) == (
        "root",
        {"owner": "admin", "reason": "requested"},
    )
    async with other_client(web_app) as owner:
        await sign_in(owner, "admin")
        row = rows(main_of((await owner.get("/tokens")).text))["billing-ci"][1]
        assert "Revoked" in row


async def test_a_token_stopped_by_its_owner_state_shows_why_and_can_still_be_revoked(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    async with web_app.state.session_factory() as db:
        owner = (await db.execute(select(User).where(User.name == "admin"))).scalar_one()
        owner.is_active = False
        await db.commit()
    await sign_in(web_client, "root")
    row = rows(main_of((await web_client.get("/tokens")).text))["billing-ci"][1]
    assert "Stopped" in row and "User disabled" in row and "Revoke" in row


async def test_an_expired_token_shows_expired_and_offers_no_revoke(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    async with web_app.state.session_factory() as db:
        token = (await db.execute(select(ApiToken))).scalar_one()
        token.expires_at = utc_now() - timedelta(days=1)
        await db.commit()
    await sign_in(web_client, "admin")
    row = rows(main_of((await web_client.get("/tokens")).text))["billing-ci"][1]
    assert "Expired" in row and "Revoke" not in row


async def test_the_users_detail_panel_lists_tokens_with_revoke_and_refreshes_itself(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    admin_id = await user_id(web_app, "admin")
    token_id = (await stored(web_app, "billing-ci")).id
    page = await web_client.get(f"/users/{admin_id}")
    body = main_of(page.text)
    assert 'data-fragment="user-tokens"' in body and 'id="user-tokens"' in body
    assert f'hx-get="/users/{admin_id}/tokens"' in body
    assert 'hx-trigger="token-changed from:body"' in body
    assert COLUMNS.findall(body.split('id="user-tokens"', 1)[1])[:4] == [
        "Name",
        "Last used",
        "Status",
        "Actions",
    ]
    assert f'hx-get="/tokens/{token_id}/revoke"' in body and 'id="token-revoke"' in page.text

    assert (await post(web_client, f"/tokens/{token_id}/revoke")).status_code == 204
    panel = await web_client.get(f"/users/{admin_id}/tokens", headers=HTMX)
    assert panel.status_code == 200
    assert_never_cached(panel)
    assert panel.text.lstrip().startswith("<section")
    assert "Revoked" in panel.text and "/revoke" not in panel.text


async def test_the_users_token_panel_is_for_the_superadmin_only(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "admin")
    admin_id = await user_id(web_app, "admin")
    assert (await web_client.get(f"/users/{admin_id}/tokens", headers=HTMX)).status_code == 403
    async with other_client(web_app) as root:
        await sign_in(root, "root")
        assert (await root.get("/users/999999/tokens", headers=HTMX)).status_code == 404


async def test_disabling_a_user_from_the_users_page_revokes_their_tokens(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    admin_id = await user_id(web_app, "admin")
    disabled = await post(web_client, f"/users/{admin_id}/disable", {"from": "detail"})
    assert disabled.status_code == 200
    assert disabled.headers["hx-redirect"] == f"/users/{admin_id}?updated=disabled"
    panel = await web_client.get(f"/users/{admin_id}/tokens", headers=HTMX)
    assert "Revoked" in panel.text and "/revoke" not in panel.text
    page = await web_client.get(f"/users/{admin_id}?updated=disabled")
    assert "Sessions ended and API tokens revoked." in page.text
