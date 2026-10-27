"""The Access tabs of projects, deployments, services and environments, by role and state."""

import html
import re
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.models import (
    AccessGrant,
    AuditEntry,
    Deployment,
    Environment,
    GrantLevel,
    Project,
    Service,
    User,
)
from app.tests.conftest import sign_in
from app.tests.web_helpers import (
    HTMX,
    assert_never_cached,
    breadcrumb_of,
    events_of,
    item_header_of,
    main_of,
    post,
)

PLURALS = {
    "project": "projects",
    "deployment": "deployments",
    "service": "services",
    "environment": "environments",
}
ROW = re.compile(r'<tr id="access-row-(\d+)" data-fragment="access-row">(.*?)</tr>', re.DOTALL)
ROW_NAME = re.compile(
    r'<th scope="row"[^>]*><span class="[^"]*"><span class="font-semibold">([^<]+)</span>'
)


@dataclass(frozen=True)
class Items:
    """Ids of the seeded items and users the tests name."""

    fancy: int
    billing: int
    prod: int
    staging: int
    main: int
    mysql: int
    web: int
    staging_web: int
    api: int
    db_env: int
    secrets_env: int
    web_env: int
    tuning_env: int
    api_env: int
    users: dict[str, int]


@pytest.fixture
async def items(web_app: FastAPI) -> Items:
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
        environments = {
            (deployment, service, name): environment
            for deployment, service, name, environment in (
                await db.execute(
                    select(Deployment.name, Service.name, Environment.name, Environment.id)
                    .join(Service, Service.deployment_id == Deployment.id)
                    .join(Environment, Environment.service_id == Service.id)
                )
            ).all()
        }
        users = dict((await db.execute(select(User.name, User.id))).all())
    return Items(
        fancy=projects["Fancy Proj. Name"],
        billing=projects["billing"],
        prod=deployments["prod"],
        staging=deployments["staging"],
        main=deployments["main"],
        mysql=services[("prod", "mysql")],
        web=services[("prod", "web")],
        staging_web=services[("staging", "web")],
        api=services[("main", "api")],
        db_env=environments[("prod", "mysql", ".env.db")],
        secrets_env=environments[("prod", "mysql", ".env.db.secrets")],
        web_env=environments[("prod", "web", ".env.web")],
        tuning_env=environments[("staging", "web", ".env.web.tuning")],
        api_env=environments[("main", "api", ".env.api")],
        users=users,
    )


def base_of(kind: str, item_id: int) -> str:
    """The path of the Access tab of an item."""
    return f"/{PLURALS[kind]}/{item_id}/access"


def section_labels(page: str) -> list[str]:
    """The text of the section links under the header, without the Superadmin tag."""
    nav = re.search(r'<nav aria-label="Sections".*?</nav>', page, re.DOTALL)
    assert nav is not None
    anchors = re.findall(r"<a [^>]*>(.*?)</a>", nav.group(0), re.DOTALL)
    return [re.sub(r"<[^>]+>", "", anchor).replace("Superadmin", "").strip() for anchor in anchors]


def plain(markup: str) -> str:
    """Markup as one line of text."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def row_actions(page: str) -> dict[str, list[str]]:
    """The keys of the row actions of every row of the access list by user name."""
    rows: dict[str, list[str]] = {}
    for _user_id, markup in ROW.findall(page):
        name = ROW_NAME.search(markup)
        assert name is not None
        rows[name.group(1)] = re.findall(r'data-row-action="([a-z-]+)"', markup)
    return rows


def listed(page: str) -> dict[str, str]:
    """The text of every row of the access list by user name."""
    rows: dict[str, str] = {}
    for _user_id, markup in ROW.findall(page):
        name = ROW_NAME.search(markup)
        assert name is not None
        rows[name.group(1)] = plain(markup)
    return rows


def options_of(fragment: str) -> list[str]:
    """The labels of the user choices of the add dialog."""
    labels = re.findall(r"<option [^>]*>([^<]*)</option>", fragment)
    return [html.unescape(label) for label in labels]


async def grants_of(web_app: FastAPI, name: str) -> set[tuple[str, int]]:
    """The stored direct grants of a user as (level, item id)."""
    async with web_app.state.session_factory() as db:
        rows = (
            await db.execute(
                select(AccessGrant)
                .join(User, User.id == AccessGrant.user_id)
                .where(User.name == name)
            )
        ).scalars()
        found = set()
        for grant in rows:
            item = {
                GrantLevel.PROJECT: grant.project_id,
                GrantLevel.DEPLOYMENT: grant.deployment_id,
                GrantLevel.ENVIRONMENT: grant.environment_id,
            }[grant.level]
            assert item is not None
            found.add((grant.level.value, item))
        return found


async def audit_details(web_app: FastAPI, action: str) -> list[dict[str, object]]:
    """The details of the audit entries of one action, oldest first."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry.details).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return [dict(row[0] or {}) for row in rows.all()]


async def disable(web_app: FastAPI, *names: str) -> None:
    """Disable seeded users, as an administrator would."""
    async with web_app.state.session_factory() as db:
        await db.execute(update(User).where(User.name.in_(names)).values(is_active=False))
        await db.commit()


async def test_the_project_tab_lists_users_with_how_they_reach_the_project(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(base_of("project", items.fancy))
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    assert section_labels(page) == ["Deployments", "Members and access", "Activity", "Settings"]
    assert f'href="/projects/{items.fancy}/access" aria-current="page"' in page
    body = main_of(page)
    rows = listed(body)
    assert set(rows) == {
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
        "env_scoped_admin",
        "env_scoped_viewer",
    }
    assert "Direct Includes future items" in rows["viewer"]
    actions = row_actions(body)
    assert actions["viewer"] == ["remove"] and actions["deployment_admin"] == ["remove"]
    assert "Inherited from prod (Deployment)" in rows["deployment_admin"]
    assert "from mysql/.env.db (Environment)" in rows["env_scoped_admin"]
    assert "2 deployments · 6 members" in body
    assert (
        '<strong class="text-ink">project Fancy Proj. Name</strong>, direct or inherited.' in body
    )
    assert "A project grant includes the deployments, services and environments" in body
    assert "Superadmins reach everything, so they are not listed." in body
    assert "root" not in rows
    assert 'data-fragment="access-list"' in body and 'id="access-list"' in body
    assert f'hx-get="/projects/{items.fancy}/access/list"' in body
    assert 'hx-trigger="grants-changed from:body"' in body
    assert f'hx-get="/projects/{items.fancy}/access/add"' in body
    assert 'id="grant-dialog"' in body and 'id="access-remove"' in body
    assert f'hx-get="/projects/{items.fancy}/access/remove/{items.users["viewer"]}"' in body
    assert "Members and access" in body and "Add access" in body


async def test_the_deployment_tab_has_the_handoff_header_and_the_other_tabs_as_links(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(base_of("deployment", items.prod))
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert section_labels(response.text) == ["Overview", "Files", "History", "Settings", "Access"]
    assert f'href="/deployments/{items.prod}" ' in body
    for tab in ("files", "activity", "settings"):
        assert f'href="/deployments/{items.prod}/{tab}"' in body
    assert f'href="/deployments/{items.prod}/access" aria-current="page"' in body
    assert dict(item_header_of(body).location) == {"root": "Deployment path: /opt/fancy/prod"}
    assert [(crumb.label, crumb.type) for crumb in breadcrumb_of(response.text)] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("prod", "Deployment"),
    ]
    assert "Check dist" not in body
    assert f'href="/deployments/{items.prod}/edit"' not in body
    rows = listed(body)
    assert "Direct Includes future items" in rows["deployment_admin"]
    assert "Inherited from Fancy Proj. Name (Project)" in rows["admin"]
    assert row_actions(body)["admin"] == ["remove"]
    assert "from mysql/.env.db (Environment)" in rows["env_scoped_viewer"]
    assert "A deployment grant includes the services and environments added later." in body
    assert 'id="grant-dialog"' in body


async def test_the_service_tab_has_no_grant_controls_and_links_to_the_grant_levels(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(base_of("service", items.mysql))
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert section_labels(response.text) == ["Environments", "Deploy history", "Access"]
    assert f'href="/services/{items.mysql}" ' in body
    assert dict(item_header_of(body).location) == {"root": "Deployment path: /opt/fancy/prod"}
    assert [(crumb.label, crumb.type) for crumb in breadcrumb_of(response.text)] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("prod", "Deployment"),
        ("mysql", "Service"),
    ]
    assert item_header_of(body).title == "Service: mysql" and "Deploy service" not in body
    assert "Add access" not in body and "grant-dialog" not in body
    assert 'id="access-remove"' in body and "Remove the grant on project Fancy Proj. Name" in body
    assert "Services are not a grant level" in body
    assert f'href="/projects/{items.fancy}/access"' in body
    assert f'href="/deployments/{items.prod}/access"' in body
    rows = listed(body)
    assert "from Fancy Proj. Name (Project)" in rows["admin"]
    assert "from prod (Deployment)" in rows["deployment_admin"]
    assert "from mysql/.env.db (Environment), sees 1 of 3" in rows["env_scoped_admin"]
    assert set(map(tuple, row_actions(body).values())) == {("remove",)}


async def test_the_environment_tab_lists_direct_and_inherited_access(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(base_of("environment", items.db_env))
    assert response.status_code == 200
    assert_never_cached(response)
    body = main_of(response.text)
    assert section_labels(response.text) == ["Edit", "History", "Compare to dist", "Access"]
    assert f'href="/environments/{items.db_env}/access" aria-current="page"' in body
    header = item_header_of(body)
    assert (header.type, header.title) == ("environment", "Environment: .env.db")
    assert dict(header.location) == {
        "root": "Deployment path: /opt/fancy/prod",
        "target": "Target: /opt/fancy/prod/environments/.env.db",
    }
    assert [(crumb.label, crumb.type) for crumb in breadcrumb_of(response.text)] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("prod", "Deployment"),
        ("mysql", "Service"),
        (".env.db", "Environment"),
    ]
    assert header.details is not None and header.details.startswith("Version ")
    rows = listed(body)
    assert "Direct" in rows["env_scoped_admin"]
    assert row_actions(body)["env_scoped_admin"] == ["remove"]
    assert "environment mysql/.env.db" in plain(body)
    assert "Includes future items" not in rows["env_scoped_admin"]
    assert "from prod (Deployment)" in rows["deployment_admin"]
    assert "from Fancy Proj. Name (Project)" in rows["viewer"]
    secrets = await web_client.get(base_of("environment", items.secrets_env))
    assert set(listed(main_of(secrets.text))) == {
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
    }


async def test_the_other_pages_of_an_environment_carry_the_access_tab_for_the_superadmin(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    for suffix in ("", "/history", "/dist"):
        page = (await web_client.get(f"/environments/{items.db_env}{suffix}")).text
        assert section_labels(page)[-1] == "Access", suffix
        assert f'href="/environments/{items.db_env}/access"' in page


async def test_the_list_fragment_is_exactly_the_panel(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    for kind, item_id in (
        ("project", items.fancy),
        ("deployment", items.prod),
        ("service", items.mysql),
        ("environment", items.db_env),
    ):
        response = await web_client.get(f"{base_of(kind, item_id)}/list", headers=HTMX)
        assert response.status_code == 200
        assert_never_cached(response)
        assert response.text.lstrip().startswith("<section")
        assert 'data-fragment="access-list"' in response.text
        assert "<html" not in response.text and "hx-swap-oob" not in response.text


async def test_a_list_with_nobody_has_an_empty_state(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    secrets = f"{base_of('environment', items.secrets_env)}/list"
    await disable_everyone_but_root(web_client, items)
    assert "No users have access yet" in (await web_client.get(secrets)).text
    service = (await web_client.get(f"{base_of('service', items.staging_web)}/list")).text
    assert "No users reach this service" in service


async def disable_everyone_but_root(web_client: httpx.AsyncClient, items: Items) -> None:
    """Disable every seeded account but the superadmin through the application's own route."""
    for name, user_id in items.users.items():
        if name != "root":
            await post(web_client, f"/users/{user_id}/disable", {"from": "list"})


@pytest.mark.parametrize(
    ("kind", "item", "note"),
    [
        ("project", "fancy", "environments added under project Fancy Proj. Name later"),
        ("deployment", "prod", "services and environments added under deployment prod"),
        ("environment", "db_env", "Grants only the named environments."),
    ],
)
async def test_the_add_dialog_offers_the_users_a_grant_would_give_something_new(
    web_client: httpx.AsyncClient, items: Items, kind: str, item: str, note: str
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"{base_of(kind, getattr(items, item))}/add", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    fragment = response.text
    assert 'data-fragment="grant-dialog"' in fragment
    assert note in fragment
    assert f'hx-post="{base_of(kind, getattr(items, item))}/grants"' in fragment
    choices = options_of(fragment)
    assert choices[0] == "Choose a user…"
    assert "ungranted_admin (ungranted_admin@example.test, Admin)" in choices
    assert not [choice for choice in choices if choice.startswith("root (")]
    assert "hx-swap-oob" not in fragment
    if kind == "project":
        not_offered = {
            "admin (admin@example.test, Admin)",
            "viewer (viewer@example.test, Viewer)",
            "project_admin (project_admin@example.test, Admin)",
        }
        assert not not_offered & set(choices)
        assert {
            "deployment_admin (deployment_admin@example.test, Admin)",
            "env_scoped_viewer (env_scoped_viewer@example.test, Viewer)",
        } <= set(choices)


async def test_the_add_dialog_says_when_everyone_already_has_access(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    await disable(
        web_app,
        "deployment_admin",
        "env_scoped_admin",
        "env_scoped_viewer",
        "ungranted_admin",
        "totp_user",
        "totp_user_alt",
    )
    fragment = (await web_client.get(f"{base_of('project', items.fancy)}/add")).text
    assert "Every active admin and viewer already has access to this project." in fragment
    assert "<select" not in fragment


async def test_adding_a_grant_answers_204_with_the_events_and_writes_one_audited_grant(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    user = items.users["ungranted_admin"]
    base = base_of("deployment", items.staging)
    response = await post(web_client, f"{base}/grants", {"user": user})
    assert response.status_code == 204
    assert events_of(response) == {"grants-changed": True, "close-dialogs": True}
    assert_never_cached(response)
    assert await grants_of(web_app, "ungranted_admin") == {("deployment", items.staging)}
    assert await audit_details(web_app, "grant.add") == [
        {"level": "deployment", "item": "Fancy Proj. Name/staging"}
    ]
    page = (await web_client.get(base_of("deployment", items.staging))).text
    assert "Direct Includes future items" in listed(main_of(page))["ungranted_admin"]


async def test_the_project_header_counts_the_new_member(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    user = items.users["ungranted_admin"]
    await post(web_client, f"{base_of('project', items.billing)}/grants", {"user": user})
    page = (await web_client.get(f"/projects/{items.billing}/access")).text
    assert "2 deployments · 3 members" in main_of(page)
    assert len(listed(main_of(page))) == 3


async def test_adding_without_a_choice_swaps_the_dialog_with_a_field_message(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    base = base_of("project", items.billing)
    for value in ("", "abc", "0", "-3"):
        response = await post(web_client, f"{base}/grants", {"user": value})
        assert response.status_code == 422, value
        assert "Choose a user." in response.text
        assert 'role="alert"' in response.text and "hx-swap-oob" not in response.text
        assert "hx-trigger" not in response.headers
    assert await audit_details(web_app, "grant.add") == []


async def test_a_refused_add_swaps_the_dialog_with_the_reason_and_redraws_the_list(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    users = items.users
    cases = [
        (
            base_of("project", items.fancy),
            users["viewer"],
            "This access changed since the page was loaded. The list shows the current state.",
        ),
        (
            base_of("environment", items.web_env),
            users["viewer"],
            "Access to this item is already included through Fancy Proj. Name.",
        ),
        (
            base_of("project", items.fancy),
            users["root"],
            "Superadmins reach every project, so they hold no grants.",
        ),
        (base_of("project", items.fancy), 987654, "This user no longer exists."),
    ]
    for base, user, reason in cases:
        response = await post(web_client, f"{base}/grants", {"user": user})
        assert response.status_code == 409, reason
        assert reason in html.unescape(response.text)
        assert 'data-fragment="grant-dialog"' in response.text
        assert 'id="access-list"' in response.text and 'hx-swap-oob="true"' in response.text
        assert "hx-trigger" not in response.headers
    assert await grants_of(web_app, "viewer") == {("project", items.fancy)}
    assert await audit_details(web_app, "grant.add") == []


async def test_adding_for_a_disabled_user_is_refused(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    await disable(web_app, "ungranted_admin")
    response = await post(
        web_client,
        f"{base_of('project', items.billing)}/grants",
        {"user": items.users["ungranted_admin"]},
    )
    assert response.status_code == 409
    assert "This account is disabled, so it is not listed here." in response.text
    assert await grants_of(web_app, "ungranted_admin") == set()


async def test_the_remove_dialog_states_the_effect_and_posts_to_remove(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    viewer = items.users["viewer"]
    base = base_of("project", items.fancy)
    response = await web_client.get(f"{base}/remove/{viewer}", headers=HTMX)
    assert response.status_code == 200
    assert_never_cached(response)
    text = plain(response.text)
    assert 'data-fragment="access-remove"' in response.text
    assert "Remove access for viewer" in text
    assert (
        "viewer loses the grant on project Fancy Proj. Name and on the items added under it" in text
    )
    assert "including to their API tokens" in text and "Their audit entries stay." in text
    assert f'hx-post="{base}/remove/{viewer}"' in response.text
    assert "hx-swap-oob" not in response.text
    scoped = items.users["env_scoped_admin"]
    env = await web_client.get(f"{base_of('environment', items.db_env)}/remove/{scoped}")
    assert "and on the items added under it later" not in plain(env.text)
    assert "Access they have through other grants stays." in plain(env.text)


async def test_the_remove_dialog_of_a_redundant_grant_names_what_still_covers_the_user(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    async with web_app.state.session_factory() as db:
        db.add(
            AccessGrant(
                user_id=items.users["viewer"],
                level=GrantLevel.ENVIRONMENT,
                environment_id=items.db_env,
            )
        )
        await db.commit()
    base = base_of("environment", items.db_env)
    page = (await web_client.get(base)).text
    assert "Also included through Fancy Proj. Name (Project)" in listed(main_of(page))["viewer"]
    response = await web_client.get(f"{base}/remove/{items.users['viewer']}")
    assert "They keep access through Fancy Proj. Name (Project)." in plain(response.text)


async def test_the_remove_dialog_refuses_an_old_page_and_unknown_users(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    base = base_of("project", items.fancy)
    stale = await web_client.get(f"{base}/remove/{items.users['ungranted_admin']}")
    assert stale.status_code == 200
    assert "This access changed since the page was loaded." in stale.text
    assert "Remove access</button>" not in stale.text
    assert 'hx-swap-oob="true"' in stale.text and 'id="access-list"' in stale.text
    assert (await web_client.get(f"{base}/remove/987654")).status_code == 404


async def test_removing_a_grant_answers_204_with_the_events_and_audits_it(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"{base_of('environment', items.db_env)}/remove/{items.users['env_scoped_admin']}",
    )
    assert response.status_code == 204
    assert events_of(response) == {"grants-changed": True, "close-dialogs": True}
    assert_never_cached(response)
    assert await grants_of(web_app, "env_scoped_admin") == set()
    assert await audit_details(web_app, "grant.remove") == [
        {"level": "environment", "item": "Fancy Proj. Name/prod/mysql/.env.db"}
    ]
    page = (await web_client.get(base_of("environment", items.db_env))).text
    assert "env_scoped_admin" not in listed(main_of(page))


async def test_removing_a_grant_that_is_gone_is_refused_with_the_current_list(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    base = base_of("project", items.fancy)
    first = await post(web_client, f"{base}/remove/{items.users['viewer']}")
    assert first.status_code == 204
    again = await post(web_client, f"{base}/remove/{items.users['viewer']}")
    assert again.status_code == 409
    assert "This access changed since the page was loaded." in again.text
    assert 'hx-swap-oob="true"' in again.text and "viewer" in again.text
    assert "hx-trigger" not in again.headers
    missing = await post(web_client, f"{base}/remove/987654")
    assert missing.status_code == 409 and "This user no longer exists." in missing.text
    assert len(await audit_details(web_app, "grant.remove")) == 1


async def test_a_service_has_no_add_routes(web_client: httpx.AsyncClient, items: Items) -> None:
    await sign_in(web_client, "root")
    base = base_of("service", items.mysql)
    assert (await web_client.get(f"{base}/add")).status_code == 404
    assert (await post(web_client, f"{base}/grants", {"user": 1})).status_code == 404


async def test_removing_an_inherited_access_names_and_takes_away_the_grant_that_gives_it(
    web_client: httpx.AsyncClient, web_app: FastAPI, items: Items
) -> None:
    await sign_in(web_client, "root")
    base = base_of("deployment", items.prod)
    user = items.users["admin"]
    dialog = (await web_client.get(f"{base}/remove/{user}")).text
    assert "access comes from the grant on project Fancy Proj. Name" in html.unescape(dialog)
    assert "also lose access to everything else under project Fancy Proj. Name" in dialog
    assert 'data-fragment="access-remove-sources"' in dialog and "Remove the grant" in dialog
    response = await post(web_client, f"{base}/remove/{user}")
    assert response.status_code == 204
    details = await audit_details(web_app, "grant.remove")
    assert details == [{"level": "project", "item": "Fancy Proj. Name"}]
    assert "admin" not in listed(main_of((await web_client.get(base)).text))


async def test_the_service_tab_removes_access_given_by_a_file_inside(
    web_client: httpx.AsyncClient, web_app: FastAPI, items: Items
) -> None:
    await sign_in(web_client, "root")
    base = base_of("service", items.mysql)
    user = items.users["env_scoped_admin"]
    dialog = (await web_client.get(f"{base}/remove/{user}")).text
    assert "through a grant on an item inside it" in dialog and "mysql/.env.db" in dialog
    response = await post(web_client, f"{base}/remove/{user}")
    assert response.status_code == 204
    details = await audit_details(web_app, "grant.remove")
    assert [item["level"] for item in details] == ["environment"]
    assert "env_scoped_admin" not in listed(main_of((await web_client.get(base)).text))


async def test_missing_items_answer_404_for_the_superadmin(
    web_client: httpx.AsyncClient,
) -> None:
    await sign_in(web_client, "root")
    for kind in PLURALS:
        for suffix in ("", "/list"):
            assert (await web_client.get(f"{base_of(kind, 987654)}{suffix}")).status_code == 404


def scope_matrix(role: str, items: Items) -> list[tuple[str, int, int]]:
    """The item and the status each role gets from its Access tab: 403 in scope, else 404."""
    everything = {
        "project": [items.fancy, items.billing],
        "deployment": [items.prod, items.staging, items.main],
        "service": [items.mysql, items.web, items.staging_web, items.api],
        "environment": [
            items.db_env,
            items.secrets_env,
            items.web_env,
            items.tuning_env,
            items.api_env,
        ],
    }
    reach: dict[str, dict[str, list[int]]] = {
        "admin": everything,
        "viewer": {
            "project": [items.fancy],
            "deployment": [items.prod, items.staging],
            "service": [items.mysql, items.web, items.staging_web],
            "environment": [items.db_env, items.secrets_env, items.web_env, items.tuning_env],
        },
        "deployment_admin": {
            "project": [items.fancy],
            "deployment": [items.prod],
            "service": [items.mysql, items.web],
            "environment": [items.db_env, items.secrets_env, items.web_env],
        },
        "env_scoped_admin": {
            "project": [items.fancy],
            "deployment": [items.prod],
            "service": [items.mysql],
            "environment": [items.db_env],
        },
        "env_scoped_viewer": {
            "project": [items.fancy, items.billing],
            "deployment": [items.prod, items.main],
            "service": [items.mysql, items.api],
            "environment": [items.db_env, items.api_env],
        },
        "ungranted_admin": {kind: [] for kind in everything},
    }
    reach["project_admin"] = reach["viewer"]
    return [
        (kind, item_id, 403 if item_id in reach[role][kind] else 404)
        for kind, ids in everything.items()
        for item_id in ids
    ]


@pytest.mark.parametrize(
    "role",
    [
        "admin",
        "viewer",
        "project_admin",
        "deployment_admin",
        "env_scoped_admin",
        "env_scoped_viewer",
        "ungranted_admin",
    ],
)
async def test_only_the_superadmin_reaches_an_access_tab_and_scope_decides_403_or_404(
    web_client: httpx.AsyncClient, items: Items, web_app: FastAPI, role: str
) -> None:
    await sign_in(web_client, role)
    victim = items.users["ungranted_admin"]
    for kind, item_id, expected in scope_matrix(role, items):
        base = base_of(kind, item_id)
        requests = [("GET", base), ("GET", f"{base}/list")]
        if kind != "service":
            requests += [
                ("GET", f"{base}/add"),
                ("POST", f"{base}/grants"),
                ("GET", f"{base}/remove/{items.users['viewer']}"),
                ("POST", f"{base}/remove/{items.users['viewer']}"),
            ]
        for method, url in requests:
            if method == "GET":
                response = await web_client.get(url, headers=HTMX)
            else:
                response = await post(web_client, url, {"user": victim})
            assert response.status_code == expected, (role, method, url)
            assert "access-row" not in response.text, (role, method, url)
    assert await grants_of(web_app, "ungranted_admin") == set()
    assert await grants_of(web_app, "viewer") == {("project", items.fancy)}
    assert await audit_details(web_app, "grant.add") == []
    assert await audit_details(web_app, "grant.remove") == []


@pytest.mark.parametrize("role", ["admin", "viewer", "deployment_admin", "env_scoped_admin"])
async def test_no_other_group_sees_an_access_tab_on_any_page(
    web_client: httpx.AsyncClient, items: Items, role: str
) -> None:
    await sign_in(web_client, role)
    pages = [
        f"/projects/{items.fancy}",
        f"/deployments/{items.prod}",
        f"/services/{items.mysql}",
        f"/environments/{items.db_env}",
        f"/environments/{items.db_env}/history",
    ]
    for url in pages:
        response = await web_client.get(url)
        assert response.status_code == 200, (role, url)
        assert "Access" not in section_labels(response.text), (role, url)
        assert "/access" not in response.text, (role, url)
        assert 'id="grant-dialog"' not in response.text


async def test_the_old_members_address_leads_to_the_tab_and_stays_closed_to_others(
    web_client: httpx.AsyncClient, items: Items
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/projects/{items.fancy}/members", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (
        307,
        f"/projects/{items.fancy}/access",
    )
