"""User administration pages and routes on the seeded application, by role."""

import html
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from app.core.passwords import verify_password
from app.models import (
    AccessGrant,
    ApiToken,
    AuditEntry,
    AuditResult,
    Deployment,
    Environment,
    PendingLogin,
    Project,
    User,
    UserGroup,
    UserSession,
)
from app.services import totp
from app.services.auth_policy import AuthPolicy
from app.services.pending_logins import create_pending_login
from app.services.permissions import load_token_scope
from app.services.sessions import create_session
from app.services.user_detail import PAGE_SIZE
from app.tests.audit_helpers import web_audit_entries
from app.tests.conftest import sign_in
from app.tests.grant_helpers import grow
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


def more_href(page: str) -> str | None:
    """The address the load more row of the users table requests, None on the last page."""
    found = re.search(r'data-fragment="users-more".*?hx-get="([^"]+)"', page, re.DOTALL)
    return html.unescape(found.group(1)) if found else None


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


async def seeded_token_id(web_app: FastAPI) -> int:
    """The id of the seeded token."""
    async with web_app.state.session_factory() as db:
        token_id: int = (await db.execute(select(ApiToken.id))).scalar_one()
    return token_id


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
        "/users/rows?after=1",
        "/users/new",
        "/users/1",
        "/users/1/confirm/disable",
        "/users/1/confirm/group?group=admin",
        "/users/1/confirm/reset-2fa",
    ):
        for headers in ({}, HTMX):
            response = await web_client.get(url, headers=headers)
            assert response.status_code == 403, (name, url)
            assert "new.hire" not in response.text
    before = await web_audit_entries(web_app, "user.create")
    root = await user_named(web_app, "root")
    victim = await user_named(web_app, "viewer")
    for url, data in (
        ("/users", NEW_USER),
        (f"/users/{victim.id}/disable", {}),
        (f"/users/{victim.id}/enable", {}),
        (f"/users/{victim.id}/group", {"group": "superadmin"}),
        (f"/users/{victim.id}/reset-2fa", {}),
        (f"/users/{victim.id}/grants", {"grant": ["project:1"], "base": ""}),
        (f"/users/{root.id}/disable", {}),
    ):
        assert (await post(web_client, url, data)).status_code == 403, (name, url)
    assert await web_audit_entries(web_app, "user.create") == before
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
    assert fragment.text.lstrip().startswith('<div id="users-table" data-fragment="users-table"')
    assert 'hx-include="#users-filters"' in fragment.text
    assert 'data-fragment="users-more"' not in body


async def add_bulk_users(web_app: FastAPI, count: int, disabled: tuple[str, ...] = ()) -> None:
    """Add viewers named bulk-0 and on, the ones named in ``disabled`` already disabled."""
    async with web_app.state.session_factory() as db:
        await grow(db, projects=0, users=count)
        await db.execute(update(User).where(User.name.in_(disabled)).values(is_active=False))
        await db.commit()


async def test_the_list_pages_with_load_more_and_the_filters_stay_in_the_links(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await add_bulk_users(web_app, 60, disabled=("bulk-5", "bulk-6"))
    await sign_in(web_client, "root")
    page = await web_client.get("/users?q=bulk&status=active")
    body = main_of(page.text)
    shown = listed(body)
    assert len(shown) == PAGE_SIZE and "root" not in shown
    href = more_href(body)
    assert href is not None and href.startswith("/users/rows?q=bulk&status=active&after=")
    assert 'id="u-q" name="q" type="search" value="bulk"' in body
    assert '<option value="active" selected>Active</option>' in body
    rest = await web_client.get(href, headers=HTMX)
    assert_never_cached(rest)
    assert rest.status_code == 200 and "<html" not in rest.text and "<table" not in rest.text
    more_rows = listed(rest.text)
    assert len(more_rows) == 58 - PAGE_SIZE and not set(more_rows) & set(shown)
    assert 'data-fragment="users-more"' not in rest.text
    assert not {"bulk-5", "bulk-6"} & (set(shown) | set(more_rows))


async def test_disabled_accounts_follow_the_active_ones_across_the_pages(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await add_bulk_users(web_app, 55, disabled=("bulk-0", "bulk-1"))
    await sign_in(web_client, "root")
    page = main_of((await web_client.get("/users")).text)
    first = listed(page)
    assert "bulk-0" not in first and len(first) == PAGE_SIZE
    href = more_href(page)
    assert href is not None
    second = await web_client.get(href, headers=HTMX)
    assert list(listed(second.text))[-2:] == ["bulk-0", "bulk-1"]
    assert more_href(second.text) is None


async def test_the_filter_form_swaps_the_results_and_pushes_the_address_of_the_view(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get("/users/table?q=ADMIN&status=active", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    assert response.headers["HX-Push-Url"] == "/users?q=ADMIN&status=active"
    names = set(listed(response.text))
    assert {"admin", "ungranted_admin"} <= names and "viewer" not in names
    refresh = await web_client.get(
        "/users/table?q=ADMIN&status=active",
        headers={**HTMX, "HX-Current-URL": "http://test/users?q=ADMIN&status=active"},
    )
    assert "HX-Push-Url" not in refresh.headers and listed(refresh.text) == listed(response.text)
    plain = await web_client.get("/users/table", headers=HTMX)
    assert plain.headers["HX-Push-Url"] == "/users"
    reset = await web_client.get(
        "/users/table", headers={**HTMX, "HX-Current-URL": "http://test/users"}
    )
    assert "HX-Push-Url" not in reset.headers


async def test_a_search_with_no_match_says_so_and_a_bad_cursor_starts_again(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    empty = await web_client.get("/users?q=nobody-by-this-name")
    assert "No users match" in empty.text and "<table" not in main_of(empty.text)
    assert 'value="nobody-by-this-name"' in empty.text
    disabled = await web_client.get("/users/table?status=disabled", headers=HTMX)
    assert "No users match" in disabled.text
    everyone = listed((await web_client.get("/users")).text)
    for after in ("zz", "-4", "0", "9" * 16, ""):
        rows = await web_client.get(f"/users/rows?after={after}", headers=HTMX)
        assert rows.status_code == 200 and listed(rows.text) == everyone, after


async def test_the_header_links_the_access_grid_and_the_api_tokens(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get("/users")).text
    header = re.search(r'<section data-fragment="page-header".*?</section>', page, re.DOTALL)
    assert header is not None
    actions = header.group(0)
    assert actions.index('href="/access/grid"') < actions.index('href="/tokens"')
    assert actions.index('href="/tokens"') < actions.index("Create user")


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
        login = await post(
            newcomer, "/login", {"identifier": "new.hire@example.test", "password": password}
        )
        assert login.status_code in (200, 204, 303)
        assert (await newcomer.get("/projects")).status_code == 200


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"name": ""}, "un", "Enter a name."),
        ({"name": "a@b"}, "un", "Use 1 to 100 characters."),
        ({"name": "under_score"}, "un", "Use 1 to 100 characters."),
        ({"name": "x" * 101}, "un", "Use 1 to 100 characters."),
        ({"name": "zero\N{ZERO WIDTH SPACE}width"}, "un", "Use 1 to 100 characters."),
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
    assert await web_audit_entries(web_app, "user.create") == []
    async with web_app.state.session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == SEEDED_USERS


async def test_two_users_may_share_a_name_and_the_email_is_the_only_key(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    shared = await create(web_client, name="root", email="other.root@example.test")
    assert shared.status_code == 200 and "User created" in shared.text
    page = (await web_client.get("/users", headers=HTMX)).text
    assert [name for _, name, _ in ROW.findall(page)].count("root") == 2
    assert "other.root@example.test" in page
    async with web_app.state.session_factory() as db:
        names = list((await db.scalars(select(User.name).where(User.name == "root"))).all())
    assert names == ["root", "root"]
    again = await create(web_client, name="Someone Else", email="OTHER.ROOT@example.test")
    assert again.status_code == 422
    assert "Another user already has this email address." in again.text


async def test_the_confirm_dialogs_state_the_effect_of_each_action(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    viewer = await user_named(web_app, "viewer")
    base = f"/users/{viewer.id}"
    page = (await web_client.get(f"{base}/confirm/disable?from=list", headers=HTMX)).text
    assert "Disable viewer" in page and "active sessions end now" in page
    assert "All of their API tokens are revoked for good" in page
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
    assert "Their old API tokens stay revoked" in page
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


async def totp_user(web_app: FastAPI) -> User:
    """The seeded user who has two-factor sign-in on."""
    user = await user_named(web_app, "totp_user")
    assert user.totp_enabled
    return user


async def test_the_reset_dialog_says_what_it_does_and_refuses_what_it_cannot(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    target = await totp_user(web_app)
    url = f"/users/{target.id}/confirm/reset-2fa?from=detail"
    page = (await web_client.get(url, headers=HTMX)).text
    assert "Reset two-factor for totp_user" in page
    assert "they sign in with their password alone" in page and "lost their authenticator" in page
    assert "active sessions end now" in page and "waiting for a code is cancelled" in page
    assert f'hx-post="/users/{target.id}/reset-2fa"' in page
    assert 'name="from" value="detail"' in page
    assert ">Reset two-factor</button>" in page
    viewer = await user_named(web_app, "viewer")
    none = (await web_client.get(f"/users/{viewer.id}/confirm/reset-2fa")).text
    assert "viewer has no two-factor sign-in to reset." in none and "<form" not in none
    root = await user_named(web_app, "root")
    await enable_totp_for(web_app, "root")
    own = (await web_client.get(f"/users/{root.id}/confirm/reset-2fa")).text
    assert "This is your own account. Remove two-factor from your Account page" in own
    assert "<form" not in own and "Reset two-factor</button>" not in own


async def enable_totp_for(web_app: FastAPI, name: str) -> None:
    """Turn the second factor on for a user directly, with a secret nobody needs to know."""
    async with web_app.state.session_factory() as db:
        user = (await db.execute(select(User).where(User.name == name))).scalar_one()
        totp.activate(user, totp.generate_secret(), 1, web_app.state.crypto)
        await db.commit()


async def test_resetting_turns_the_factor_off_ends_sessions_and_pending_sign_ins(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    target = await totp_user(web_app)
    async with web_app.state.session_factory() as db:
        user = await db.get(User, target.id)
        assert user is not None
        await create_session(db, user, AuthPolicy())
        await create_pending_login(db, user, AuthPolicy())
        await db.commit()
    response = await post(web_client, f"/users/{target.id}/reset-2fa", {"from": "detail"})
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/users/{target.id}?updated=two-factor-reset"
    assert_never_cached(response)
    stored = await stored_user(web_app, target.id)
    assert (stored.totp_enabled, stored.totp_secret, stored.totp_last_step) == (False, None, None)
    async with web_app.state.session_factory() as db:
        for model in (UserSession, PendingLogin):
            count = await db.scalar(
                select(func.count()).select_from(model).where(model.user_id == target.id)
            )
            assert count == 0, model
    (entry,) = await web_audit_entries(web_app, "user.totp_reset")
    assert (entry.actor_name, entry.target_name, entry.result) == (
        "root",
        "totp_user",
        AuditResult.SUCCESS,
    )
    assert entry.details == {"sessions_ended": 1, "pending_logins_ended": 1}
    page = (await web_client.get(f"/users/{target.id}?updated=two-factor-reset")).text
    assert "Two-factor reset" in page and "totp_user signs in with the password alone." in page
    assert "Two-factor on" not in page and "/confirm/reset-2fa" not in page


async def test_from_the_list_a_reset_refreshes_the_table(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    target = await totp_user(web_app)
    response = await post(web_client, f"/users/{target.id}/reset-2fa", {"from": "list"})
    assert response.status_code == 204
    assert not (await stored_user(web_app, target.id)).totp_enabled


async def test_a_reset_that_cannot_be_done_is_refused_with_the_dialog_and_changes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    viewer = await user_named(web_app, "viewer")
    none = await post(web_client, f"/users/{viewer.id}/reset-2fa", {"from": "detail"})
    assert none.status_code == 409 and "viewer has no two-factor sign-in to reset." in none.text
    await enable_totp_for(web_app, "root")
    root = await user_named(web_app, "root")
    own = await post(web_client, f"/users/{root.id}/reset-2fa", {"from": "detail"})
    assert own.status_code == 409 and "This is your own account." in own.text
    assert (await stored_user(web_app, root.id)).totp_enabled
    assert (await web_client.get("/projects")).status_code == 200
    assert await web_audit_entries(web_app, "user.totp_reset") == []
    assert (await post(web_client, "/users/987654/reset-2fa", {})).status_code == 404


async def test_the_detail_page_shows_whether_two_factor_is_on_and_offers_the_reset_only_then(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    target = await totp_user(web_app)
    on = (await web_client.get(f"/users/{target.id}")).text
    assert "Two-factor on" in on
    assert f'hx-get="/users/{target.id}/confirm/reset-2fa?from=detail"' in on
    viewer = await user_named(web_app, "viewer")
    off = (await web_client.get(f"/users/{viewer.id}")).text
    assert "Two-factor on" not in off and "/confirm/reset-2fa" not in off


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
        assert token.revoked_at is not None
    (entry,) = await web_audit_entries(web_app, "user.disable")
    assert (entry.actor_name, entry.target_name, entry.result) == (
        "root",
        "admin",
        AuditResult.SUCCESS,
    )
    (revoked,) = await web_audit_entries(web_app, "token.revoke")
    assert (revoked.actor_name, revoked.target_name) == ("root", "billing-ci")
    assert revoked.details == {"owner": "admin", "reason": "user_disabled"}
    assert entry.details == {"sessions_ended": 1, "tokens_revoked": 1}
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
    assert await web_audit_entries(web_app, "user.enable") == []
    assert await web_audit_entries(web_app, "user.group_change") == []


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
        assert_never_cached(response)
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
        *(await web_audit_entries(web_app, "user.disable")),
        *(await web_audit_entries(web_app, "user.group_change")),
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
    assert_never_cached(response)
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
    assert_never_cached(response)
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
        assert token.revoked_at is not None
    (entry,) = await web_audit_entries(web_app, "user.group_change")
    assert entry.details == {"from": "admin", "to": "viewer", "tokens_revoked": 1}
    assert entry.actor_name == "root"
    (revoked,) = await web_audit_entries(web_app, "token.revoke")
    assert revoked.details == {"owner": "admin", "reason": "moved_to_viewer"}


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
    assert f'href="/audit?actor=user:{admin.id}"' in body
    assert "No activity yet" not in body
    assert 'id="user-action"' in response.text and 'id="token-revoke"' in response.text
    token_id = await seeded_token_id(web_app)
    assert f'hx-get="/tokens/{token_id}/revoke"' in body

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
    entries = [
        *(await web_audit_entries(web_app, "grant.add")),
        *(await web_audit_entries(web_app, "grant.remove")),
    ]
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
    assert (
        await web_audit_entries(web_app, "grant.add") == []
        and await web_audit_entries(web_app, "grant.remove") == []
    )

    root = await user_named(web_app, "root")
    refused = await post(web_client, f"/users/{root.id}/grants", {"grant": ["project:1"]})
    assert refused.status_code == 409
    async with web_app.state.session_factory() as db:
        held = select(func.count()).select_from(AccessGrant).where(AccessGrant.user_id == root.id)
        assert await db.scalar(held) == 0
