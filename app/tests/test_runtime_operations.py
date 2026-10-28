"""Pins on the work a request does: statements per page, what is read once and what is opened once.

Each limit is the number measured after the repeats were removed, so a page that loads the whole
structure a second time, reads the settings again, decrypts a version again or probes Docker again
fails here. They count the SQL statements of the application's engine while one request runs; the
seeded database is small, so a limit is a pin, not a budget.
"""

import re
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select

from app.core.crypto import CryptoService
from app.core.csrf import CSRF_COOKIE_NAME
from app.models import (
    AccessGrant,
    AuditEntry,
    Deployment,
    Environment,
    GrantLevel,
    HookType,
    Project,
    Service,
    ServiceHook,
    User,
)
from app.services.dist_check import run_deployment_dist_checks
from app.services.drift import load_drift_view
from app.services.editor import load_editor_header
from app.services.permissions import (
    Capability,
    ItemLevel,
    load_items,
    permitted_environment_ids,
)
from app.services.versions import create_version
from app.tests.conftest import sign_in
from app.tests.deploy_helpers import CountingCrypto, Harness
from app.tests.dist_helpers import link_dist
from app.tests.fake_docker import FakeDocker
from app.tests.grant_helpers import count_statements
from app.tests.runtime_helpers import BEHAVIOR, inspect_entry, log_row, ps_entry, serve, serve_logs
from app.tests.web_helpers import HTMX, post, stage_deployments

STRUCTURE_LOAD = "SELECT projects.id, NULL AS anon_1"
SETTINGS_READ = re.compile(r"FROM app_settings$")
COMMAND = "docker compose up -d --no-deps mysql"
# Statements of the runtime fragment: what the page runs plus the matching of the stored services
# with the containers and the last deploy writes.
RUNTIME_LIMIT = 24
CONTAINER_DIALOG_LIMIT = 17
CONTAINER_POST_LIMIT = 18
CONTAINER_POST_FAILED_LIMIT = 19
# The Logs tab loads the deployment row for its Compose project (one more than the other tabs); with
# a project it also reads the containers and the deploys that mark them (the runtime of the page).
LOGS_PAGE_LIMIT = 21
# A read of lines loads the deployment row and the newest two versions of every environment (each
# opened once, pinned below). A first read also asks the audit table whether the same filter was
# opened in the last ten minutes (one more statement) and writes its entry when it was not; a poll
# does neither.
LOGS_LINES_LIMIT = 18
LOGS_POLL_LIMIT = 17


@dataclass(frozen=True)
class Ids:
    """Ids of the seeded items the pins use."""

    project: int
    deployment: int
    service: int
    environment: int


async def seeded_ids(web_app: FastAPI) -> Ids:
    """The Fancy project, its prod deployment, the mysql service and its ``.env.db`` file."""
    async with web_app.state.session_factory() as db:
        ids = [
            await db.scalar(select(Project.id).where(Project.name == "Fancy Proj. Name")),
            await db.scalar(select(Deployment.id).where(Deployment.name == "prod")),
            await db.scalar(select(Service.id).where(Service.name == "mysql")),
            await db.scalar(select(Environment.id).where(Environment.name == ".env.db")),
        ]
    assert all(ids)
    return Ids(*ids)


async def measure(web_app: FastAPI, request: Awaitable[httpx.Response]) -> tuple[int, list[str]]:
    """The status of an answer and every statement run while it was made.

    Notifications announced before (the sign-in from a new address, for one) are delivered in
    the background and read the settings; they are waited for first, so they never land in the
    count of the request.
    """
    notifier = getattr(web_app.state, "notifier", None)
    if notifier is not None and hasattr(notifier, "idle"):
        await notifier.idle()
    with count_statements(web_app.state.engine) as seen:
        response = await request
    return response.status_code, seen


def structure_loads(statements: list[str]) -> int:
    """How many times the whole structure was read to work out what a caller may see."""
    return sum(1 for statement in statements if statement.startswith(STRUCTURE_LOAD))


def settings_reads(statements: list[str]) -> int:
    """How many times every setting was read in one query."""
    return sum(1 for statement in statements if SETTINGS_READ.search(statement.rstrip()))


@pytest.fixture
async def ids(web_app: FastAPI, tmp_path: Path) -> Ids:
    """The seeded deployments on a real folder with a command on the mysql service."""
    await stage_deployments(web_app, tmp_path, COMMAND)
    return await seeded_ids(web_app)


# Every limit counts the one statement that asks whether the allowed path list is empty, for the
# first-run banner of a superadmin page.
PAGE_LIMITS = (
    ("deployment overview", "/deployments/{deployment}", 18),
    ("deployment files tab", "/deployments/{deployment}/files", 18),
    ("deployment history tab", "/deployments/{deployment}/activity", 18),
    # One more than the other tabs: the row of the deployment is read for its Compose project.
    ("deployment logs tab", "/deployments/{deployment}/logs", 19),
    ("deployment settings tab", "/deployments/{deployment}/settings", 18),
    ("deployment runtime fragment", "/deployments/{deployment}/runtime", RUNTIME_LIMIT),
    ("deployment access tab", "/deployments/{deployment}/access", 24),
    ("deployment edit form", "/deployments/{deployment}/edit", 20),
    ("new deployment page", "/projects/{project}/deployments/new", 20),
    ("new deployment folder form", "/projects/{project}/deployments/new?source=folder", 20),
    ("project page", "/projects/{project}", 20),
    ("project activity", "/projects/{project}/activity", 21),
    ("project settings", "/projects/{project}/settings", 20),
    ("service page", "/services/{service}", 20),
    ("editor page", "/environments/{environment}", 19),
    ("drift page", "/environments/{environment}/drift", 21),
    ("dist report", "/environments/{environment}/dist", 21),
    ("smtp tab", "/settings/smtp", 6),
    ("sso tab", "/settings/sso", 6),
    ("git tab", "/settings/git", 6),
    ("notifications tab", "/settings/notifications", 6),
    ("registries tab", "/settings/registries", 7),
)


@pytest.mark.parametrize(("label", "path", "limit"), PAGE_LIMITS)
async def test_a_page_runs_no_more_statements_than_it_needs_and_reads_each_thing_once(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    label: str,
    path: str,
    limit: int,
) -> None:
    await sign_in(web_client, "root")
    url = path.format(
        project=ids.project,
        deployment=ids.deployment,
        service=ids.service,
        environment=ids.environment,
    )
    status, statements = await measure(web_app, web_client.get(url))
    assert status == 200, label
    assert len(statements) <= limit, f"{label}: {len(statements)} statements"
    assert structure_loads(statements) <= 1, f"{label} read the whole structure again"
    assert settings_reads(statements) == 1, f"{label} read the settings again"


async def test_the_post_deploy_panel_and_the_pre_flight_read_the_settings_once(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    status, panel = await measure(
        web_app, web_client.get(f"/services/{ids.service}/postdeploy", headers=HTMX)
    )
    assert status == 200 and len(panel) <= 16
    assert settings_reads(panel) == 1 and structure_loads(panel) <= 1
    status, preflight = await measure(
        web_app, post(web_client, f"/services/{ids.service}/deploy/preflight")
    )
    assert status == 200 and len(preflight) <= 37
    assert settings_reads(preflight) == 1 and structure_loads(preflight) <= 1


def registry_reads(statements: list[str]) -> int:
    """How many statements read the Docker registry logins."""
    return sum(1 for statement in statements if "FROM docker_registries" in statement)


async def test_a_deploy_reads_the_settings_twice_by_design_and_the_structure_once(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    """The second read is the switch of the post-deploy feature, checked at deploy time.

    The limit counts one statement for the registry logins a command runs with (31 before they
    were read); a deploy without a command never reads them (next test).
    """
    await sign_in(web_client, "root")
    data = {"selection": "explicit", "environment": [ids.environment]}
    status, statements = await measure(
        web_app, post(web_client, f"/services/{ids.service}/deploy", data)
    )
    assert status == 200 and len(statements) <= 32
    assert settings_reads(statements) == 2 and structure_loads(statements) <= 1
    assert registry_reads(statements) == 1


async def test_a_deploy_without_a_command_costs_what_it_did_before_and_reads_no_registry(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    async with web_app.state.session_factory() as db:
        await db.execute(delete(ServiceHook).where(ServiceHook.service_id == ids.service))
        await db.commit()
    await sign_in(web_client, "root")
    data = {"selection": "explicit", "environment": [ids.environment]}
    status, statements = await measure(
        web_app, post(web_client, f"/services/{ids.service}/deploy", data)
    )
    assert status == 200 and len(statements) <= 29
    assert registry_reads(statements) == 0


async def test_a_scoped_caller_loads_the_structure_once_and_a_revoked_grant_applies_next_request(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    """The visibility lives for one request: the next one sees a grant taken away."""
    await sign_in(web_client, "env_scoped_admin")
    url = f"/services/{ids.service}"
    status, first = await measure(web_app, web_client.get(url))
    assert status == 200 and structure_loads(first) == 1
    async with web_app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.name == "env_scoped_admin"))
        assert user is not None
        await db.execute(delete(AccessGrant).where(AccessGrant.user_id == user.id))
        await db.commit()
    status, second = await measure(web_app, web_client.get(url))
    assert status == 404 and structure_loads(second) == 1


async def test_a_grant_added_between_two_requests_shows_in_the_second(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    await sign_in(web_client, "ungranted_admin")
    url = f"/deployments/{ids.deployment}"
    assert (await web_client.get(url)).status_code == 404
    async with web_app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.name == "ungranted_admin"))
        assert user is not None
        db.add(
            AccessGrant(user_id=user.id, level=GrantLevel.DEPLOYMENT, deployment_id=ids.deployment)
        )
        await db.commit()
    assert (await web_client.get(url)).status_code == 200


async def test_the_environments_of_one_service_are_filtered_in_sql(harness: Harness) -> None:
    h = harness
    scope = await h.scope_of("superadmin")
    service = h.seed.services.mysql.id
    with count_statements(h.database) as seen:
        found = await permitted_environment_ids(h.db, scope, Capability.DEPLOY, service_id=service)
    assert len(seen) == 1 and "services.id = ?" in seen[0]
    everything = await load_items(h.db, ItemLevel.ENVIRONMENT)
    expected = sorted(item.item_id for item in everything if item.service_id == service)
    assert found == expected and len(found) < len(everything)
    prod = h.seed.deployments.fancy_prod.id
    inside = await load_items(h.db, ItemLevel.ENVIRONMENT, deployment_id=prod)
    assert {item.deployment_id for item in inside} == {prod}
    assert set(found) <= {item.item_id for item in inside}


async def test_the_grid_toggle_reads_the_grants_of_the_user_once(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    async with web_app.state.session_factory() as db:
        nobody = await db.scalar(select(User).where(User.name == "ungranted_admin"))
        assert nobody is not None
    data = {"user": str(nobody.id), "target": f"deployment:{ids.deployment}", "was": "0"}
    status, statements = await measure(web_app, post(web_client, "/access/grid/toggle", data))
    assert status == 200
    reads = [s for s in statements if s.startswith("SELECT access_grants.id")]
    assert len(reads) == 1, reads


async def test_a_rename_reads_the_project_row_once(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids
) -> None:
    await sign_in(web_client, "root")
    status, statements = await measure(
        web_app, post(web_client, f"/projects/{ids.project}/rename", {"name": "Fancy Proj. Name"})
    )
    assert status == 200
    reads = [s for s in statements if s.startswith("SELECT projects.id, projects.name")]
    assert len(reads) == 1, reads


async def test_the_dist_overview_costs_the_same_statements_for_any_number_of_files(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, crypto: CryptoService
) -> None:
    await sign_in(web_client, "root")
    entries = select(func.count()).select_from(AuditEntry)
    checks = entries.where(AuditEntry.action == "environment.dist_check")

    async def overview() -> tuple[list[str], int]:
        async with web_app.state.session_factory() as db:
            before = await db.scalar(checks) or 0
        status, statements = await measure(
            web_app, web_client.get(f"/deployments/{ids.deployment}/dist")
        )
        assert status == 200
        async with web_app.state.session_factory() as db:
            return statements, (await db.scalar(checks) or 0) - before

    small, small_entries = await overview()
    async with web_app.state.session_factory() as db:
        root = await db.scalar(select(User).where(User.name == "root"))
        assert root is not None
        for number in range(6):
            environment = Environment(
                service_id=ids.service, name=f".env.extra{number}", target_path=f"extra{number}"
            )
            db.add(environment)
            await db.flush()
            await create_version(db, crypto, environment.id, "A=1\n", author=root)
        await db.commit()
    large, large_entries = await overview()

    assert len(small) == len(large) <= 23
    assert large_entries - small_entries == 6, "one entry per file, as before"
    assert len([s for s in large if s.startswith("INSERT INTO audit_entries")]) == 1


async def test_a_pre_flight_opens_each_version_once(harness: Harness) -> None:
    h = harness
    await link_dist(h.db, h.seed.envs.db, None)
    counting = CountingCrypto(h.crypto)
    h.engine.crypto = counting
    outcome = await h.deploy(h.seed.services.mysql.id, dry_run=True)
    assert len(outcome.environments) >= 3
    assert counting.opened and counting.most_often_opened() == 1


async def test_a_deploy_with_a_command_opens_each_version_once_scrubber_included(
    harness: Harness,
) -> None:
    h = harness
    await link_dist(h.db, h.seed.envs.db, None)
    h.db.add(
        ServiceHook(
            service_id=h.seed.services.mysql.id,
            type=HookType.POST_DEPLOY,
            commands="docker compose up -d",
        )
    )
    await h.db.commit()
    counting = CountingCrypto(h.crypto)
    h.engine.crypto = counting
    outcome = await h.deploy(h.seed.services.mysql.id)
    assert len(outcome.environments) >= 3
    assert len(h.runner.requests) == 1
    assert counting.opened and counting.most_often_opened() == 1


async def test_the_drift_view_opens_one_version_once_when_managed_and_deployed_are_the_same(
    harness: Harness,
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    await create_version(h.db, h.crypto, web.id, "A=1\n", author=h.seed.users.admin)
    await h.db.commit()
    outcome = await h.deploy(h.seed.services.staging_web.id, environments=[web])
    assert outcome.environments[0].status.value == "written"
    header = await load_editor_header(h.db, await h.scope_of("superadmin"), web.id)
    assert header is not None
    counting = CountingCrypto(h.crypto)
    view = await load_drift_view(h.db, counting, header)
    assert view is not None and view.deployed is not None
    assert len(counting.opened) == 1


async def test_the_outcome_of_a_run_carries_the_command_it_ran_with(harness: Harness) -> None:
    h = harness
    await link_dist(h.db, h.seed.envs.db, None)
    h.db.add(
        ServiceHook(
            service_id=h.seed.services.mysql.id,
            type=HookType.POST_DEPLOY,
            commands="docker compose up -d",
        )
    )
    await h.db.commit()
    outcome = await h.deploy(h.seed.services.mysql.id, dry_run=True)
    assert outcome.hook is not None and outcome.hook.commands == "docker compose up -d"


async def test_the_overview_checks_every_file_and_writes_one_entry_each_in_order(
    harness: Harness,
) -> None:
    h = harness
    scope = await h.scope_of("superadmin")
    action = AuditEntry.action == "environment.dist_check"
    before = await h.db.scalar(select(func.count()).select_from(AuditEntry).where(action)) or 0
    checks = await run_deployment_dist_checks(
        h.db,
        h.crypto,
        scope,
        h.seed.deployments.fancy_prod.id,
        actor=h.seed.users.superadmin,
        ip_address=None,
    )
    names = [(check.subject.service_name, check.subject.name) for check in checks]
    assert names == sorted(names) and len(names) >= 3
    entries = list(
        (await h.db.scalars(select(AuditEntry).where(action).order_by(AuditEntry.id))).all()
    )[before:]
    assert len(entries) == len(checks)
    assert {entry.target_name for entry in entries} == {name for _service, name in names}
    assert all(entry.id is not None and entry.actor_name == "root" for entry in entries)


async def test_the_stacks_list_and_the_stack_form_run_a_fixed_number_of_statements(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, tmp_path: Path
) -> None:
    """Finding the stacks reads the allowed paths and the deployments once each, whatever the
    number of stacks, and the form a pick fills needs the settings only."""
    for number in range(6):
        folder = tmp_path / "host" / f"stack-{number}"
        folder.mkdir()
        (folder / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    await sign_in(web_client, "root")
    base = f"/projects/{ids.project}/deployments/new"
    assert (await web_client.get(f"{base}/stacks")).text.count("stack-row") >= 6
    status, listing = await measure(web_app, web_client.get(f"{base}/stacks"))
    assert status == 200
    assert len(listing) <= 22, f"the stacks list: {len(listing)} statements"
    assert structure_loads(listing) <= 1 and settings_reads(listing) == 1
    picked = {"folder": str(tmp_path / "host" / "stack-0")}
    status, filled = await measure(web_app, web_client.get(f"{base}/use", params=picked))
    assert status == 200
    assert len(filled) <= 21, f"the stack form: {len(filled)} statements"
    assert structure_loads(filled) <= 1 and settings_reads(filled) == 1


async def test_the_overview_page_never_asks_docker_and_its_fragment_reads_it_once_in_five_seconds(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, tmp_path: Path
) -> None:
    fake = FakeDocker(tmp_path / "fake")
    serve(
        fake,
        ps=[ps_entry("mysql", 1)],
        inspect=[inspect_entry(1)],
        network_ls=[],
        network_inspect=[],
    )
    web_app.state.docker_cli = fake.cli()
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, ids.deployment)
        assert deployment is not None
        deployment.compose_project = "fancy"
        await db.commit()
    await sign_in(web_client, "root")
    url = f"/deployments/{ids.deployment}"

    status, page = await measure(web_app, web_client.get(url))
    assert status == 200 and len(page) <= 18
    assert fake.probes() == 0 and fake.calls() == []

    status, first = await measure(web_app, web_client.get(f"{url}/runtime", headers=HTMX))
    assert status == 200 and settings_reads(first) == 1 and structure_loads(first) <= 1
    assert len(first) <= RUNTIME_LIMIT
    reads = len(fake.calls())
    assert fake.probes() == 1 and reads > 0

    status, second = await measure(web_app, web_client.get(f"{url}/runtime", headers=HTMX))
    assert status == 200 and len(second) <= RUNTIME_LIMIT
    assert len(fake.calls()) == reads and fake.probes() == 1


async def test_the_container_dialog_and_post_run_few_statements_and_open_values_to_scrub_only(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, tmp_path: Path
) -> None:
    """The confirmation reads the deployment, the settings and the containers once and runs no
    command. A post that works decrypts no file; one whose command fails opens each version once,
    to scrub the output, and lists the containers anew."""
    fake = FakeDocker(tmp_path / "fake-containers")
    serve(
        fake,
        ps=[ps_entry("mysql", 1)],
        inspect=[inspect_entry(1)],
        network_ls=[],
        network_inspect=[],
    )
    web_app.state.docker_cli = fake.cli()
    counting = CountingCrypto(web_app.state.crypto)
    web_app.state.crypto = counting
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, ids.deployment)
        assert deployment is not None
        deployment.compose_project = "fancy"
        await db.commit()
    await sign_in(web_client, "root")
    url = f"/deployments/{ids.deployment}/containers/mysql/restart"

    status, dialog = await measure(web_app, web_client.get(url, headers=HTMX))
    assert status == 200 and len(dialog) <= CONTAINER_DIALOG_LIMIT, len(dialog)
    assert settings_reads(dialog) == 1 and structure_loads(dialog) <= 1
    assert all(" restart " not in " ".join(call.arguments) for call in fake.calls())
    assert counting.opened == []

    await web_client.get("/healthz")
    token = {**HTMX, "X-CSRFToken": web_client.cookies[CSRF_COOKIE_NAME]}
    status, done = await measure(web_app, web_client.post(url, headers=token))
    assert status == 200 and len(done) <= CONTAINER_POST_LIMIT, len(done)
    assert settings_reads(done) <= 1 and structure_loads(done) <= 1
    assert counting.opened == []

    fake.behavior(BEHAVIOR + 'case "$*" in\n  *" restart "*) echo failed; exit 1 ;;\nesac\n')
    status, failed = await measure(web_app, web_client.post(url, headers=token))
    assert status == 200 and len(failed) <= CONTAINER_POST_FAILED_LIMIT, len(failed)
    assert counting.opened and counting.most_often_opened() == 1


async def test_the_logs_tab_and_its_lines_run_few_statements_and_open_each_value_once_a_read(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, tmp_path: Path
) -> None:
    """The page reads the containers once and decrypts nothing. A read of lines opens the two
    newest versions of every file of the deployment once, to mask them (never more than two per
    file), and a read that finds no line opens none; polls never write the audit log."""
    fake = FakeDocker(tmp_path / "fake-logs")
    serve_logs(fake, log_row("mysql-1", "2026-10-27T10:00:01.000000100Z", "hello"))
    web_app.state.docker_cli = fake.cli()
    counting = CountingCrypto(web_app.state.crypto)
    web_app.state.crypto = counting
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, ids.deployment)
        assert deployment is not None
        deployment.compose_project = "fancy"
        await db.commit()
    await sign_in(web_client, "root")
    url = f"/deployments/{ids.deployment}/logs"

    status, page = await measure(web_app, web_client.get(url))
    assert status == 200 and len(page) <= LOGS_PAGE_LIMIT, len(page)
    assert settings_reads(page) == 1 and structure_loads(page) <= 1
    assert counting.opened == []

    status, first = await measure(web_app, web_client.get(f"{url}/lines", headers=HTMX))
    assert status == 200 and len(first) <= LOGS_LINES_LIMIT, len(first)
    assert settings_reads(first) <= 1 and structure_loads(first) <= 1
    assert counting.opened and counting.most_often_opened() == 1
    async with web_app.state.session_factory() as db:
        environments = await db.scalar(
            select(func.count(Environment.id))
            .join(Service, Service.id == Environment.service_id)
            .where(Service.deployment_id == ids.deployment)
        )
    opened = len(counting.opened)
    assert environments and environments < opened <= 2 * environments

    status, poll = await measure(
        web_app, web_client.get(f"{url}/lines?cursor=2026-10-27T10:00:00Z", headers=HTMX)
    )
    assert status == 200 and len(poll) <= LOGS_POLL_LIMIT, len(poll)
    assert len(counting.opened) == 2 * opened and counting.most_often_opened() == 2
    assert not any("INSERT INTO audit_entries" in statement for statement in poll)

    status, quiet = await measure(
        web_app,
        web_client.get(f"{url}/lines?cursor=2026-10-27T10:00:01.000000100Z", headers=HTMX),
    )
    assert status == 200 and len(quiet) <= LOGS_POLL_LIMIT, len(quiet)
    assert len(counting.opened) == 2 * opened
