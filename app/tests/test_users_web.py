"""User administration pages and routes on the seeded application, by role."""

import html
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.passwords import verify_password
from app.models import (
    AccessGrant,
    ApiToken,
    AuditEntry,
    AuditResult,
    Deployment,
    Environment,
    Project,
    User,
    UserGroup,
)
from app.services.permissions import load_token_scope
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, assert_never_cached, main_of, post

NOT_SUPERADMIN = (
    "admin",
    "viewer",
    "project_admin",
    "deployment_admin",
    "env_scoped_admin",
    "env_scoped_viewer",
    "ungranted_admin",
)
ROW = re.compile(
    r'<tr data-fragment="user-row" id="user-(\d+)"><th[^>]*><a[^>]*>([^<]+)</a></th>(.*?)</tr>',
    re.DOTALL,
)
SEEDED_USERS = 10
PASSWORD = re.compile(r'id="secret" readonly value="([^"]+)"')


def listed(page: str) -> dict[str, int]:
    """User ids by name, as the users table lists them."""
    return {name: int(user_id) for user_id, name, _ in ROW.findall(page)}


def password_of(page: str) -> str:
    """The generated password shown in the created dialog."""
    found = PASSWORD.search(page)
    assert found is not None
    return html.unescape(found.group(1))


async def user_named(web_app: FastAPI, name: str) -> User:
    """A seeded or created user by name."""
    async with web_app.state.session_factory() as db:
        user: User = (await db.execute(select(User).where(User.name == name))).scalar_one()
    return user


async def audit(web_app: FastAPI, action: str) -> list[AuditEntry]:
    """The audit entries of an action, oldest first."""
    async with web_app.state.session_factory() as db:
        return list(
            (
                await db.execute(
                    select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
                )
            ).scalars()
        )


async def stored_user(web_app: FastAPI, user_id: int) -> User:
    """A user as stored now."""
    async with web_app.state.session_factory() as db:
        user: User | None = await db.get(User, user_id)
    assert user is not None
    return user


async def force_active(web_app: FastAPI, name: str, active: bool) -> None:
    """Set a user's status directly, leaving no audit entry behind."""
    async with web_app.state.session_factory() as db:
        user = (await db.execute(select(User).where(User.name == name))).scalar_one()
        user.is_active = active
        await db.commit()


async def add_superadmin(web_app: FastAPI, name: str) -> None:
    """Add a second active superadmin that signs in with the seeded password."""
    async with web_app.state.session_factory() as db:
        root = (await db.execute(select(User).where(User.name == "root"))).scalar_one()
        db.add(
            User(
                name=name,
                email=f"{name}@example.test",
                password_hash=root.password_hash,
                group=UserGroup.SUPERADMIN,
            )
        )
        await db.commit()


async def item_ids(web_app: FastAPI) -> dict[str, int]:
    """Ids of a project, a deployment and an environment to grant."""
    async with web_app.state.session_factory() as db:
        project = await db.scalar(select(Project.id).where(Project.name == "billing"))
        deployment = await db.scalar(select(Deployment.id).where(Deployment.name == "staging"))
        environment = await db.scalar(select(Environment.id).where(Environment.name == ".env.api"))
    assert project and deployment and environment
    return {"project": project, "deployment": deployment, "environment": environment}


@asynccontextmanager
async def other_client(web_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser, so one test can hold two signed-in users."""
    transport = httpx.ASGITransport(app=web_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Accept": "text/html"}
    ) as http:
        yield http


NEW_USER = {"name": "new.hire", "email": "new.hire@example.test", "group": "viewer"}


async def create(http: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    """Submit the create user form."""
    return await post(http, "/users", {**NEW_USER, **overrides})


@pytest.mark.parametrize("name", NOT_SUPERADMIN)
async def test_nobody_but_the_superadmin_reaches_the_users_screens_or_their_actions(
    web_client: httpx.AsyncClient, web_app: FastAPI, name: str
) -> None:
    await sign_in(web_client, name)
    projects = (await web_client.get("/projects")).text
    assert 'href="/users"' not in projects
    for url in (
        "/users",
        "/users/table",
        "/users/new",
        "/users/1",
        "/users/1/confirm/disable",
        "/users/1/confirm/group?group=admin",
    ):
        for headers in ({}, HTMX):
            response = await web_client.get(url, headers=headers)
            assert response.status_code == 403, (name, url)
            assert "new.hire" not in response.text
    before = await audit(web_app, "user.create")
    root = await user_named(web_app, "root")
    victim = await user_named(web_app, "viewer")
    for url, data in (
        ("/users", NEW_USER),
        (f"/users/{victim.id}/disable", {}),
        (f"/users/{victim.id}/enable", {}),
        (f"/users/{victim.id}/group", {"group": "superadmin"}),
        (f"/users/{victim.id}/grants", {"grant": ["project:1"], "base": ""}),
        (f"/users/{root.id}/disable", {}),
    ):
        assert (await post(web_client, url, data)).status_code == 403, (name, url)
    assert await audit(web_app, "user.create") == before
    assert (await stored_user(web_app, victim.id)).group is UserGroup.VIEWER
    assert (await stored_user(web_app, root.id)).is_active
    async with web_app.state.session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == SEEDED_USERS


async def test_the_superadmin_sees_the_link_and_every_account_with_its_figures(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    projects = (await web_client.get("/projects")).text
    assert 'href="/users"' in projects
    page = await web_client.get("/users")
    assert_never_cached(page)
    body = main_of(page.text)
    for column in ("Name", "Email", "Group", "Status", "Last login", "Projects", "Actions"):
        assert f">{column}</th>" in body
    ids = listed(body)
    assert list(ids) == sorted(ids, key=str.lower)
    assert {"root", "admin", "viewer", "ungranted_admin"} <= set(ids)
    rows = {name: cells for _, name, cells in ROW.findall(body)}
    assert "Superadmin" in rows["root"] and ">All</td>" in rows["root"]
    assert "Admin" in rows["admin"] and ">2</td>" in rows["admin"]
    assert ">0</td>" in rows["ungranted_admin"]
    assert "Active" in rows["viewer"] and "Never" not in rows["root"]
    assert "Never" in rows["ungranted_admin"]
    assert f'hx-get="/users/{ids["viewer"]}/confirm/disable?from=list"' in body
    assert f'href="/users/{ids["viewer"]}"' in body
    assert 'hx-get="/users/new"' in body and 'data-target="user-create"' in body
    assert 'hx-trigger="user-changed from:body"' in body
    assert 'id="user-create"' in page.text and "data-clear-on-close" in page.text
    assert 'id="user-action"' in page.text
    assert 'href="/users/access"' not in page.text
    fragment = await web_client.get("/users/table", headers=HTMX)
    assert_never_cached(fragment)
    assert listed(fragment.text) == ids and "<html" not in fragment.text


async def test_the_create_dialog_offers_the_group_and_the_whole_grant_tree(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get("/users/new", headers=HTMX)
    assert_never_cached(response)
    page = response.text
    for text in ("Create user", "Superadmin only", "Name", "Email", "Group", "Access grants"):
        assert text in page
    async with web_app.state.session_factory() as db:
        grantable = sum(
            [
                await db.scalar(select(func.count()).select_from(model)) or 0
                for model in (Project, Deployment, Environment)
            ]
        )
    assert page.count('name="grant"') == grantable > 0
    assert 'hx-post="/users"' in page and 'hx-target="#user-create"' in page
    for value in ("viewer", "admin", "superadmin"):
        assert f'<option value="{value}"' in page
    assert '<option value="viewer" selected>' in page
    ids = await item_ids(web_app)
    assert f'value="project:{ids["project"]}"' in page
    assert f'value="deployment:{ids["deployment"]}"' in page
    assert f'value="environment:{ids["environment"]}"' in page
    assert "Includes future deployments, services and environments." in page
    assert "Includes future services and environments." in page
    assert "(groups environments, not a grant level)" in page
    assert "service:" not in page and 'value="service' not in page
    assert " checked" not in page
    assert "A 16-character password is generated and shown once after creation." in page


async def test_creating_a_user_shows_the_password_once_and_stores_it_hashed_with_grants(
    web_client: httpx.AsyncClient, web_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    await sign_in(web_client, "root")
    ids = await item_ids(web_app)
    grants = [f"project:{ids['project']}", f"environment:{ids['environment']}"]
    response = await create(web_client, group="admin", grant=grants)
    assert response.status_code == 200
    assert_never_cached(response)
    assert "user-changed" in response.headers["hx-trigger"]
    body = response.text
    assert "User created" in body and "new.hire · Admin" in body
    assert "Shown once. Copy it now and pass it on securely." in body
    assert 'data-js="copy"' in body and "data-copy-label" in body and "hx-ignore" in body
    password = password_of(body)
    assert len(password) == 16

    user = await user_named(web_app, "new.hire")
    assert user.group is UserGroup.ADMIN and user.is_active
    assert user.password_hash != password and verify_password(password, user.password_hash)[0]
    async with web_app.state.session_factory() as db:
        stored = (
            await db.execute(select(AccessGrant).where(AccessGrant.user_id == user.id))
        ).scalars()
        assert sorted(grant.level.value for grant in stored) == ["environment", "project"]
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    created = [entry for entry in entries if entry.action == "user.create"]
    assert [entry.details for entry in created] == [{"group": "admin", "grants": 2}]
    assert created[0].actor_name == "root" and created[0].target_name == "new.hire"
    assert sorted(entry.action for entry in entries if entry.action.startswith("grant.")) == [
        "grant.add",
        "grant.add",
    ]
    everything = " ".join(f"{entry.details!r} {entry.target_name}" for entry in entries)
    assert password not in everything and password not in caplog.text

    for url in ("/users", "/users/table", "/users/new", f"/users/{user.id}"):
        later = await web_client.get(url, headers=HTMX)
        assert password not in later.text, url
    page = await web_client.get("/users")
    assert "new.hire" in listed(page.text)

    async with other_client(web_app) as newcomer:
        await newcomer.get("/healthz")
        login = await post(newcomer, "/login", {"identifier": "new.hire", "password": password})
        assert login.status_code in (200, 204, 303)
        assert (await newcomer.get("/projects")).status_code == 200


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"name": ""}, "un", "Enter a name."),
        ({"name": "has space"}, "un", "Use 1 to 100 characters without spaces."),
        ({"name": "a@b"}, "un", "The name cannot contain @"),
        ({"name": "ROOT"}, "un", "A user named ROOT already exists."),
        ({"email": ""}, "ue", "Enter an email address."),
        ({"email": "not-an-email"}, "ue", "Enter a valid email address."),
        ({"email": "Admin@EXAMPLE.test"}, "ue", "Another user already has this email address."),
        ({"group": "owner"}, "ug", "Choose a group."),
        ({"grant": ["project:abc"]}, "grants", "A selected item is not valid."),
        ({"grant": ["project:987654"]}, "grants", "A selected item no longer exists."),
    ],
)
async def test_a_refused_create_names_the_field_keeps_the_choices_and_creates_nothing(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    overrides: dict[str, object],
    field: str,
    message: str,
) -> None:
    await sign_in(web_client, "root")
    ids = await item_ids(web_app)
    base: dict[str, object] = {"grant": [f"deployment:{ids['deployment']}"]}
    response = await create(web_client, **{**base, **overrides})
    assert response.status_code == 422
    assert_never_cached(response)
    body = response.text
    assert message in body
    assert 'role="alert"' in body and 'data-fragment="user-create"' in body
    assert "User created" not in body and 'id="secret"' not in body
    assert "hx-trigger" not in response.headers
    if "grant" not in overrides:
        assert f'value="deployment:{ids["deployment"]}" checked' in body
    if "name" not in overrides:
        assert 'value="new.hire"' in body
    assert await audit(web_app, "user.create") == []
    async with web_app.state.session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == SEEDED_USERS


async def test_the_confirm_dialogs_state_the_effect_of_each_action(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    viewer = await user_named(web_app, "viewer")
    base = f"/users/{viewer.id}"
    page = (await web_client.get(f"{base}/confirm/disable?from=list", headers=HTMX)).text
    assert "Disable viewer" in page and "active sessions end now" in page
    assert "API tokens stop working" in page
    assert f'hx-post="{base}/disable"' in page and 'name="from" value="list"' in page
    assert "Disable user" in page and "This is your own account" not in page
    url = f"{base}/confirm/group?from=detail&group=admin"
    page = (await web_client.get(url, headers=HTMX)).text
    assert "Move viewer from Viewer to Admin." in page and "The change applies now." in page
    assert f'hx-post="{base}/group"' in page and 'name="group" value="admin"' in page
    assert 'name="from" value="detail"' in page and "Change group" in page
    ungranted = await user_named(web_app, "ungranted_admin")
    await force_active(web_app, "ungranted_admin", False)
    page = (
        await web_client.get(f"/users/{ungranted.id}/confirm/enable?from=list", headers=HTMX)
    ).text
    assert "Enable ungranted_admin" in page
    assert "API tokens that were not revoked work again" in page
    await add_superadmin(web_app, "second")
    root = await user_named(web_app, "root")
    own = (await web_client.get(f"/users/{root.id}/confirm/group?group=admin", headers=HTMX)).text
    assert "This is your own account, so you may lose access to this page." in own
    own = (await web_client.get(f"/users/{root.id}/confirm/disable", headers=HTMX)).text
    assert "This is your own account: you will be signed out now." in own


async def test_a_confirm_dialog_refuses_what_changes_nothing_or_cannot_be_done(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    root = await user_named(web_app, "root")
    viewer = await user_named(web_app, "viewer")
    refused = (await web_client.get(f"/users/{root.id}/confirm/disable?from=list")).text
    assert "Cannot disable the last active superadmin" in refused
    assert "Create or enable another superadmin first." in refused
    assert f'hx-get="/users/{root.id}/confirm/disable?from=list"' in refused
    assert "<form" not in refused and "Disable user" not in refused
    demote = (await web_client.get(f"/users/{root.id}/confirm/group?group=admin")).text
    assert "Cannot demote the last active superadmin" in demote and "<form" not in demote
    same = (await web_client.get(f"/users/{root.id}/confirm/group?group=superadmin")).text
    assert "root is already in the Superadmin group." in same and "<form" not in same
    assert (
        "viewer is already active."
        in (await web_client.get(f"/users/{viewer.id}/confirm/enable")).text
    )
    for url in (
        f"/users/{viewer.id}/confirm/destroy",
        f"/users/{viewer.id}/confirm/group?group=owner",
        f"/users/{viewer.id}/confirm/group",
        "/users/987654/confirm/disable",
    ):
        assert (await web_client.get(url, headers=HTMX)).status_code == 404, url


async def test_disabling_ends_the_sessions_stops_the_tokens_and_is_audited(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    admin = await user_named(web_app, "admin")
    async with other_client(web_app) as victim:
        await sign_in(victim, "admin")
        assert (await victim.get("/projects")).status_code == 200
        await sign_in(web_client, "root")
        response = await post(web_client, f"/users/{admin.id}/disable", {"from": "list"})
        assert response.status_code == 204
        assert set(response.headers["hx-trigger"].replace(" ", "").strip("{}").split(",")) >= {
            '"user-changed":true',
            '"close-dialogs":true',
        }
        ended = await victim.get("/projects")
        assert ended.status_code == 303 and ended.headers["location"].startswith("/login")
    async with web_app.state.session_factory() as db:
        assert not (await stored_user(web_app, admin.id)).is_active
        token = (await db.execute(select(ApiToken))).scalars().first()
        assert token is not None and await load_token_scope(db, token) is None
    (entry,) = await audit(web_app, "user.disable")
    assert (entry.actor_name, entry.target_name, entry.result) == (
        "root",
        "admin",
        AuditResult.SUCCESS,
    )
    assert entry.details == {"sessions_ended": 1}
    page = await web_client.get("/users")
    row = next(cells for _, name, cells in ROW.findall(page.text) if name == "admin")
    assert "Disabled" in row and "/confirm/enable?from=list" in row

    response = await post(web_client, f"/users/{admin.id}/enable", {"from": "list"})
    assert response.status_code == 204
    assert (await stored_user(web_app, admin.id)).is_active


async def test_a_change_that_is_already_in_effect_does_nothing_and_is_not_audited(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    viewer = await user_named(web_app, "viewer")
    assert (await post(web_client, f"/users/{viewer.id}/enable", {})).status_code == 204
    same_group = await post(web_client, f"/users/{viewer.id}/group", {"group": "viewer"})
    assert same_group.status_code == 204
    assert await audit(web_app, "user.enable") == []
    assert await audit(web_app, "user.group_change") == []


async def test_the_detail_origin_goes_back_to_the_detail_page_with_a_notice(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    viewer = await user_named(web_app, "viewer")
    base = f"/users/{viewer.id}"
    for action, data, updated in (
        ("disable", {"from": "detail"}, "disabled"),
        ("enable", {"from": "detail"}, "enabled"),
        ("group", {"from": "detail", "group": "admin"}, "group"),
    ):
        response = await post(web_client, f"{base}/{action}", data)
        assert response.status_code == 200
        assert response.headers["hx-redirect"] == f"{base}?updated={updated}"
    page = (await web_client.get(f"{base}?updated=group")).text
    assert "Group changed" in page and "viewer is now an Admin." in page
    assert "Group changed" not in (await web_client.get(f"{base}?updated=nonsense")).text
    assert "User disabled" in (await web_client.get(f"{base}?updated=disabled")).text


async def test_the_last_active_superadmin_cannot_be_disabled_or_demoted(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    root = await user_named(web_app, "root")
    response = await post(web_client, f"/users/{root.id}/disable", {"from": "list"})
    assert response.status_code == 409
    assert "Cannot disable the last active superadmin" in response.text
    response = await post(web_client, f"/users/{root.id}/group", {"group": "admin"})
    assert response.status_code == 409
    assert "Cannot demote the last active superadmin" in response.text
    async with web_app.state.session_factory() as db:
        stored = await db.get(User, root.id)
        assert stored is not None and stored.is_active and stored.group is UserGroup.SUPERADMIN
    blocked = [
        *(await audit(web_app, "user.disable")),
        *(await audit(web_app, "user.group_change")),
    ]
    assert [entry.result for entry in blocked] == [AuditResult.BLOCKED] * 2
    assert (await web_client.get("/projects")).status_code == 200


async def test_a_superadmin_who_disables_or_demotes_themselves_is_sent_to_projects(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await add_superadmin(web_app, "second")
    await sign_in(web_client, "root")
    root = await user_named(web_app, "root")
    response = await post(web_client, f"/users/{root.id}/disable", {"from": "list"})
    assert response.status_code == 200 and response.headers["hx-redirect"] == "/projects"
    after = await web_client.get("/projects")
    assert after.status_code == 303 and after.headers["location"].startswith("/login")

    async with web_app.state.session_factory() as db:
        root_row = await db.get(User, root.id)
        assert root_row is not None
        root_row.is_active = True
        await db.commit()
    web_client.cookies.clear()
    await sign_in(web_client, "root")
    response = await post(web_client, f"/users/{root.id}/group", {"group": "admin"})
    assert response.headers["hx-redirect"] == "/projects"
    assert (await web_client.get("/users")).status_code == 403
    assert (await web_client.get("/projects")).status_code == 200


async def test_changing_the_group_applies_at_once_and_stops_tokens_of_a_new_viewer(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    admin = await user_named(web_app, "admin")
    async with other_client(web_app) as session:
        await sign_in(session, "admin")
        assert "Audit log" in (await session.get("/projects")).text
        await sign_in(web_client, "root")
        response = await post(web_client, f"/users/{admin.id}/group", {"group": "viewer"})
        assert response.status_code == 204
        assert "Audit log" not in (await session.get("/projects")).text
        assert (await session.get("/audit")).status_code == 403
    async with web_app.state.session_factory() as db:
        token = (await db.execute(select(ApiToken))).scalars().first()
        assert token is not None and await load_token_scope(db, token) is None
    (entry,) = await audit(web_app, "user.group_change")
    assert entry.details == {"from": "admin", "to": "viewer"} and entry.actor_name == "root"


async def test_the_detail_page_shows_group_status_grants_tokens_and_activity(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    admin = await user_named(web_app, "admin")
    response = await web_client.get(f"/users/{admin.id}")
    assert_never_cached(response)
    body = main_of(response.text)
    assert "<h1" in body and ">admin</h1>" in body
    assert "admin@example.test · never signed in" in body
    assert "Admin" in body and "Active" in body
    assert f'hx-get="/users/{admin.id}/confirm/disable?from=detail"' in body
    assert '<option value="admin" selected>' in body
    assert f'hx-get="/users/{admin.id}/confirm/group?from=detail"' in body
    assert 'hx-include="#g2"' in body
    assert f'hx-post="/users/{admin.id}/grants"' in body and 'name="base"' in body
    assert body.count(" checked") == 2 and "Save grants" in body
    assert "billing-ci" in body and "Active" in body and "Never" in body
    assert "Recent activity" in body and "Open audit log" in body
    assert 'href="/audit?actor=user:admin"' in body
    assert "No activity yet" not in body
    assert 'id="user-action"' in response.text
    assert "Revoke" not in body

    ungranted = await user_named(web_app, "ungranted_admin")
    quiet = main_of((await web_client.get(f"/users/{ungranted.id}")).text)
    assert "No API tokens" in quiet and "No activity yet" in quiet and " checked" not in quiet


async def test_a_superadmin_has_no_grants_to_edit_and_a_disabled_user_can_be_enabled(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    root = await user_named(web_app, "root")
    body = main_of((await web_client.get(f"/users/{root.id}")).text)
    assert "Superadmins reach every project, so grants do not apply" in body
    assert "Save grants" not in body and 'type="checkbox"' not in body
    viewer = await user_named(web_app, "viewer")
    await force_active(web_app, "viewer", False)
    body = main_of((await web_client.get(f"/users/{viewer.id}")).text)
    assert "Disabled" in body and "/confirm/enable?from=detail" in body
    assert "/confirm/disable" not in body


async def test_an_unknown_user_is_not_found_everywhere(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    for response in (
        await web_client.get("/users/987654"),
        await post(web_client, "/users/987654/disable", {}),
        await post(web_client, "/users/987654/group", {"group": "admin"}),
        await post(web_client, "/users/987654/grants", {"grant": []}),
    ):
        assert response.status_code == 404
        assert "987654" not in response.text


async def test_saving_grants_adds_and_removes_audits_and_applies_at_once(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    ungranted = await user_named(web_app, "ungranted_admin")
    ids = await item_ids(web_app)
    async with other_client(web_app) as person:
        await sign_in(person, "ungranted_admin")
        assert "billing" not in (await person.get("/projects")).text
        await sign_in(web_client, "root")
        panel = (await web_client.get(f"/users/{ungranted.id}")).text
        assert 'name="base" value=""' in panel
        response = await post(
            web_client,
            f"/users/{ungranted.id}/grants",
            {"grant": [f"project:{ids['project']}"], "base": ""},
        )
        assert response.status_code == 200
        assert "grants-changed" in response.headers["hx-trigger"]
        assert_never_cached(response)
        body = response.text
        assert "Grants saved. They apply now." in body
        assert f'value="project:{ids["project"]}" checked' in body
        assert f'name="base" value="project:{ids["project"]}"' in body
        assert 'data-fragment="user-grants"' in body and "<html" not in body
        assert "billing" in (await person.get("/projects")).text

        response = await post(
            web_client,
            f"/users/{ungranted.id}/grants",
            {"grant": [], "base": f"project:{ids['project']}"},
        )
        assert "Grants saved." in response.text and " checked" not in response.text
        assert "billing" not in (await person.get("/projects")).text
    entries = [*(await audit(web_app, "grant.add")), *(await audit(web_app, "grant.remove"))]
    assert [(entry.action, entry.details["item"]) for entry in entries] == [
        ("grant.add", "billing"),
        ("grant.remove", "billing"),
    ]


async def test_saving_unchanged_or_stale_or_vanished_grants_changes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    viewer = await user_named(web_app, "viewer")
    ids = await item_ids(web_app)
    async with web_app.state.session_factory() as db:
        stored = {
            f"{grant.level.value}:{grant.project_id}"
            for grant in (
                await db.execute(select(AccessGrant).where(AccessGrant.user_id == viewer.id))
            ).scalars()
        }
    assert len(stored) == 1
    same = await post(
        web_client,
        f"/users/{viewer.id}/grants",
        {"grant": sorted(stored), "base": ",".join(stored)},
    )
    assert same.status_code == 200 and "There was nothing to change." in same.text
    assert "hx-trigger" not in same.headers

    stale = await post(
        web_client,
        f"/users/{viewer.id}/grants",
        {"grant": [f"project:{ids['project']}"], "base": f"project:{ids['project']}"},
    )
    assert stale.status_code == 409
    assert "changed since the page was loaded" in stale.text
    assert "The list shows the current grants." in stale.text
    assert sorted(re.findall(r'value="([a-z]+:\d+)" checked', stale.text)) == sorted(stored)

    gone = await post(
        web_client,
        f"/users/{viewer.id}/grants",
        {"grant": [*stored, "environment:987654"], "base": ",".join(stored)},
    )
    assert gone.status_code == 409 and "no longer exists" in gone.text
    junk = await post(
        web_client, f"/users/{viewer.id}/grants", {"grant": ["junk"], "base": ",".join(stored)}
    )
    assert junk.status_code == 409 and "is not valid" in junk.text
    assert await audit(web_app, "grant.add") == [] and await audit(web_app, "grant.remove") == []

    root = await user_named(web_app, "root")
    refused = await post(web_client, f"/users/{root.id}/grants", {"grant": ["project:1"]})
    assert refused.status_code == 409
    async with web_app.state.session_factory() as db:
        held = select(func.count()).select_from(AccessGrant).where(AccessGrant.user_id == root.id)
        assert await db.scalar(held) == 0
