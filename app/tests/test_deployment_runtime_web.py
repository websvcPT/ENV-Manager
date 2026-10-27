"""The runtime section of the Overview: its fragment, the status strip it completes and the
loading state of the page, by state and role, on the seeded application with a fake Docker."""

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.db import create_session_factory
from app.models import (
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Service,
)
from app.services.docker_cli import DAEMON_UNREACHABLE_REASON
from app.tests.conftest import sign_in
from app.tests.fake_docker import FakeDocker
from app.tests.runtime_helpers import (
    attached,
    inspect_entry,
    network_entry,
    ps_entry,
    publisher,
    serve,
)

NOW = datetime(2026, 10, 27, 12, 30, 0, tzinfo=UTC)
PROJECT = "fancy"
FUTURE = "2999-01-01T00:00:00Z"
LONG_AGO = "2000-01-01T00:00:00Z"
EVERY_INTERFACE = "0.0.0.0"  # noqa: S104 - the address of a published port, nothing binds
REFRESH = (
    "every 10s, visibilitychange from:document, deploy-finished from:body, "
    "containers-changed from:body"
)


class Stack:
    """The deployment ids of the seeded Fancy project and the fake Docker that serves its stack."""

    def __init__(self, prod: int, fake: FakeDocker) -> None:
        """Keep the id of ``prod`` and the fake."""
        self.prod = prod
        self.fake = fake

    @property
    def runtime(self) -> str:
        """The address of the runtime fragment of ``prod``."""
        return f"/deployments/{self.prod}/runtime"


def serve_stack(fake: FakeDocker) -> None:
    """Three containers: ``mysql`` running, healthy and published to every interface, ``web``
    exited (killed, restarted four times, created long ago), ``redis`` running with no stored
    service, published to this host only on a network of another stack."""
    serve(
        fake,
        ps=[
            ps_entry(
                "mysql",
                1,
                health="healthy",
                image="mysql:9",
                publishers=(publisher(EVERY_INTERFACE, 3306, 3306),),
            ),
            ps_entry("web", 2, "exited", exit_code=137, image="nginx:1"),
            ps_entry(
                "redis",
                3,
                image="redis:8",
                publishers=(publisher("127.0.0.1", 6379, 6379),),
            ),
        ],
        inspect=[
            inspect_entry(
                1,
                health="healthy",
                started="2026-10-27T10:00:00Z",
                created=FUTURE,
                networks=(attached("fancy_default", "172.20.0.2", ("mysql",)),),
            ),
            inspect_entry(
                2,
                status="exited",
                started="2026-10-27T09:00:00Z",
                finished="2026-10-27T09:30:00Z",
                exit_code=137,
                restarts=4,
                created=LONG_AGO,
                networks=(attached("fancy_default", "", ("web",)),),
            ),
            inspect_entry(
                3,
                started="2026-10-26T12:30:00Z",
                created=FUTURE,
                networks=(attached("shared_net", "172.30.0.5", ("redis",)),),
            ),
        ],
        network_ls=["fancy_default", "fancy_backend"],
        network_inspect=[
            network_entry("fancy_default", subnets=(("172.20.0.0/16", "172.20.0.1"),)),
            network_entry(
                "fancy_backend", internal=True, subnets=(("172.21.0.0/16", "172.21.0.1"),)
            ),
            network_entry("shared_net", subnets=(("172.30.0.0/16", "172.30.0.1"),)),
        ],
    )


async def deploy_web_files(web_app: FastAPI, deployment: int) -> None:
    """A real deploy run that wrote a file of the ``web`` service, at the time of the fake clock."""
    async with create_session_factory(web_app.state.engine)() as db:
        service = await db.scalar(
            select(Service).where(Service.deployment_id == deployment, Service.name == "web")
        )
        assert service is not None
        run = DeployRun(
            service_id=service.id,
            service_name="web",
            deployment_name="prod",
            trigger=DeployTrigger.UI,
            status=DeployStatus.SUCCESS,
            started_at=NOW - timedelta(seconds=1),
        )
        db.add(run)
        await db.flush()
        db.add(
            DeployResult(
                run_id=run.id,
                environment_name=".env.web",
                target_path=".env.web",
                status=DeployResultStatus.WRITTEN,
                created_at=NOW,
            )
        )
        await db.commit()


async def set_project(web_app: FastAPI, deployment: int, project: str | None) -> None:
    """Set the Compose project of a deployment."""
    async with create_session_factory(web_app.state.engine)() as db:
        row = await db.get(Deployment, deployment)
        assert row is not None
        row.compose_project = project
        await db.commit()


@pytest.fixture
async def stack(web_app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stack:
    """The prod deployment with a Compose project, a fake Docker serving its stack and a fixed
    clock for the uptimes."""
    fake = FakeDocker(tmp_path / "fake")
    serve_stack(fake)
    web_app.state.docker_cli = fake.cli()
    monkeypatch.setattr("app.web.deployments.utc_now", lambda: NOW)
    async with web_app.state.session_factory() as db:
        prod = await db.scalar(select(Deployment.id).where(Deployment.name == "prod"))
    assert prod is not None
    await set_project(web_app, prod, PROJECT)
    await deploy_web_files(web_app, prod)
    return Stack(prod, fake)


def text_of(markup: str) -> str:
    """The visible text of a piece of markup, with the white space collapsed."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def rows_of(fragment: str) -> dict[str, str]:
    """The markup of each row of the services table, by service name."""
    return dict(
        re.findall(
            r'<tr data-fragment="service-summary-row" data-service="([^"]+)">(.*?)</tr>',
            fragment,
            re.DOTALL,
        )
    )


def items_of(fragment: str) -> dict[str, str]:
    """The markup of each item of the phone list of the services, by service name."""
    return dict(
        re.findall(
            r'<li data-fragment="service-summary-item" data-service="([^"]+)"[^>]*>(.*?)</li>',
            fragment,
            re.DOTALL,
        )
    )


def strip_of(fragment: str) -> str:
    """The status strip the fragment brings along."""
    pattern = r'<section aria-label="Status" id="status-strip".*?</section>'
    found = re.search(pattern, fragment, re.DOTALL)
    assert found is not None
    return found.group(0)


def strip_cell(fragment: str, label: str) -> str:
    """The text of one cell of the status strip."""
    cell = re.search(rf">{label}</p>(.*?)</div></div>", strip_of(fragment), re.DOTALL)
    assert cell is not None, label
    return text_of(cell.group(1))


async def read(client: httpx.AsyncClient, path: str, status: int = 200) -> str:
    """The body of a GET that must answer with ``status``."""
    response = await client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == status, (path, response.status_code)
    return response.text


async def test_the_page_loads_the_runtime_after_it_is_shown_and_never_asks_docker(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The page carries the loading state, with the one trigger that asks for the fragment, and
    the status strip without the container cells."""
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{stack.prod}")
    page = response.text
    assert response.status_code == 200
    loading = re.search(r'<div id="runtime"[^>]*>', page)
    assert loading is not None
    markup = loading.group(0)
    assert 'aria-busy="true"' in markup
    assert f'hx-get="/deployments/{stack.prod}/runtime"' in markup
    assert f'hx-trigger="load, {REFRESH}"' in markup
    assert "data-background-refresh" in markup
    assert 'data-fragment="runtime-services"' in page and "Loading the containers" in page
    assert ">Containers</p>" not in page and ">Applied</p>" not in page
    assert ">State</p>" in page and ">Last deploy</p>" in page
    assert 'data-fragment="service-summary-row"' not in page
    assert stack.fake.probes() == 0 and stack.fake.calls() == []


async def test_the_fragment_swaps_itself_every_ten_seconds_and_keeps_its_content_on_errors(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The refreshing fragment does not carry the load trigger (it would loop) and ignores any
    error answer, so a failed refresh leaves what is shown."""
    await sign_in(web_client, "root")
    response = await web_client.get(stack.runtime, headers={"HX-Request": "true"})
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"
    fragment = response.text
    root = re.search(r'<div id="runtime"[^>]*>', fragment)
    assert root is not None
    markup = root.group(0)
    assert 'aria-busy="false"' in markup
    assert f'hx-trigger="{REFRESH}"' in markup and 'hx-trigger="load' not in markup
    assert 'hx-swap="outerHTML"' in markup and 'hx-sync="this:drop"' in markup
    assert 'hx-status:4xx="swap:none"' in markup and 'hx-status:5xx="swap:none"' in markup
    assert 'id="status-strip"' in fragment and 'hx-swap-oob="true"' in strip_of(fragment)
    assert "<html" not in fragment


async def test_the_superadmin_reads_the_services_with_their_containers(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """State and health badges, uptime, exit code and restarts, the files and the Recreate to
    apply warning, the compose service with no files and the images under the names."""
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    rows = rows_of(fragment)
    assert list(rows) == ["mysql", "redis", "web"]

    mysql = rows["mysql"]
    assert 'data-status="up"' in mysql and 'data-status="healthy"' in mysql
    assert text_of(mysql).startswith(
        "mysql mysql:9 Running Healthy Up 2 hours since 2026-10-27 10:00"
    )
    assert 'href="/services/' in mysql and "Recreate to apply" not in mysql

    web = rows["web"]
    assert 'data-status="exited"' in web and "Healthy" not in web
    summary = text_of(web)
    assert "nginx:1 Exited" in summary
    assert "Exited 3 hours ago exit code 137 restarted 4 times" in summary
    assert 'data-status="recreate"' in web and "Recreate to apply" in summary
    written = r"Files written \d{4}-\d\d-\d\d \d\d:\d\d ?, container created 2000-01-01 00:00"
    assert re.search(written, summary)

    redis = rows["redis"]
    assert "No files in ENV Manager" in redis and "Up 1 day" in text_of(redis)
    assert f'href="/deployments/{stack.prod}/services/new?name=redis"' in redis
    assert "/services/" not in redis.replace(f"/deployments/{stack.prod}/services/new", "")
    assert text_of(redis).endswith("None")

    assert "No container" not in fragment
    assert "Environments" not in fragment and ">Files<" in fragment


async def test_the_status_strip_counts_the_containers_and_what_needs_a_recreate(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Containers is running of total, Applied the number to recreate, the other cells stay."""
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert strip_cell(fragment, "Containers") == "2 of 3 running"
    assert 'data-status="attention"' in strip_of(fragment)
    assert strip_cell(fragment, "Applied") == "1 to recreate"
    for label in ("State", "Last deploy", "Drift state", "Dist check", "Post-deploy"):
        assert f">{label}</p>" in strip_of(fragment)
    order = [text_of(label) for label in re.findall(r"<p [^>]*>([^<]+)</p>", strip_of(fragment))]
    assert order == [
        "State",
        "Containers",
        "Applied",
        "Last deploy",
        "Drift state",
        "Dist check",
        "Post-deploy",
    ]


async def test_everything_applied_reads_all_applied(
    web_client: httpx.AsyncClient, web_app: FastAPI, stack: Stack
) -> None:
    """With every container newer than the last write, the Applied cell says so."""
    async with web_app.state.session_factory() as db:
        await db.execute(update(DeployResult).values(created_at=NOW - timedelta(days=10000)))
        await db.commit()
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert strip_cell(fragment, "Applied") == "All applied"
    assert "Recreate to apply" not in fragment
    assert 'data-status="recreate"' not in fragment


async def test_the_network_panel_lists_networks_addresses_and_published_ports(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Networks with driver, subnet, gateway and notes; addresses per service and the bind
    badges, the one for every interface in the warn tone."""
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    panel = fragment.split('data-fragment="runtime-network"', 1)[1]
    networks = {
        name: text_of(markup)
        for name, markup in re.findall(
            r'data-fragment="network-row" data-network="([^"]+)">(.*?)</tr>', panel, re.DOTALL
        )
    }
    assert networks == {
        "fancy_default": "fancy_default bridge 172.20.0.0/16 172.20.0.1 None",
        "fancy_backend": (
            "fancy_backend bridge 172.21.0.0/16 172.21.0.1 Internal, no outside access"
        ),
        "shared_net": (
            "shared_net bridge 172.30.0.0/16 172.30.0.1 External, shared with other stacks"
        ),
    }
    services = dict(
        re.findall(
            r'data-fragment="network-service-row" data-service="([^"]+)">(.*?)</tr>',
            panel,
            re.DOTALL,
        )
    )
    assert list(services) == ["mysql", "redis", "web"]
    assert text_of(services["mysql"]) == (
        "mysql fancy_default 172.20.0.2 as mysql 0.0.0.0:3306 \u2192 3306/tcp All interfaces"
    )
    assert re.search(r'bg-warn-bg text-warn-fg[^>]*data-status="all-interfaces"', services["mysql"])
    assert text_of(services["redis"]) == (
        "redis shared_net 172.30.0.5 as redis 127.0.0.1:6379 \u2192 6379/tcp This host only"
    )
    assert 'data-status="this-host"' in services["redis"]
    assert text_of(services["web"]) == "web fancy_default no address as web None"


async def test_a_viewer_sees_the_same_runtime_without_any_action(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Read only for everyone: a viewer gets the status and the network, but no Add service."""
    await sign_in(web_client, "viewer")
    fragment = await read(web_client, stack.runtime)
    assert list(rows_of(fragment)) == ["mysql", "redis", "web"]
    assert strip_cell(fragment, "Containers") == "2 of 3 running"
    assert 'data-fragment="runtime-network"' in fragment
    assert "No files in ENV Manager" in fragment
    for absent in ("/services/new", "Add service", "Settings", "<button"):
        assert absent not in fragment, absent


async def test_a_caller_with_one_environment_sees_only_their_service_and_no_network(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The row, the counts and the strip cover the granted service only; nothing names the
    stack's other containers, its project or its networks."""
    await sign_in(web_client, "env_scoped_admin")
    fragment = await read(web_client, stack.runtime)
    assert list(rows_of(fragment)) == ["mysql"]
    assert list(items_of(fragment)) == ["mysql"]
    assert strip_cell(fragment, "Containers") == "1 of 1 running"
    assert strip_cell(fragment, "Applied") == "All applied"
    assert "Files in your scope" in fragment
    for hidden in ("web", "redis", "nginx", "fancy", "shared_net", "172.", "Network", PROJECT):
        assert hidden not in text_of(fragment), hidden
    assert 'data-fragment="runtime-network"' not in fragment
    assert "Add service" not in fragment and "No files in ENV Manager" not in fragment


async def test_the_phone_list_has_the_state_uptime_files_and_the_recreate_badge(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Below md the rows are a list with the same facts, and the table is for md and up."""
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert 'class="hidden md:block"' in fragment
    assert '<ul class="m-0 list-none md:hidden"' in fragment
    items = items_of(fragment)
    assert list(items) == ["mysql", "redis", "web"]
    web = text_of(items["web"])
    assert "Exited" in web and "Exited 3 hours ago exit code 137 restarted 4 times" in web
    assert "Recreate to apply" in web and "Files written" in web
    assert "Up 2 hours" in text_of(items["mysql"])


async def test_without_a_compose_project_the_note_points_the_superadmin_to_settings(
    web_client: httpx.AsyncClient, web_app: FastAPI, stack: Stack
) -> None:
    """The stored services are still listed, without container columns, and Docker is not asked."""
    await set_project(web_app, stack.prod, None)
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert 'data-runtime-state="no_project"' in fragment
    assert "has no Docker Compose project name" in fragment
    assert f'href="/deployments/{stack.prod}/settings"' in fragment
    assert list(rows_of(fragment)) == ["mysql", "web"]
    assert ">Uptime<" not in fragment
    assert ">Containers</p>" not in fragment and ">Applied</p>" not in fragment
    assert stack.fake.probes() == 0 and stack.fake.calls() == []
    await sign_in(web_client, "admin")
    fragment = await read(web_client, stack.runtime)
    assert "has no Docker Compose project name" in fragment
    assert "/settings" not in fragment


async def test_an_unusable_docker_gives_its_reason_and_the_stored_services(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The reason is the fixed text of the Docker status, as the post-deploy panel shows it."""
    stack.fake.switch("no-daemon")
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert 'data-runtime-state="unavailable"' in fragment
    assert f"The containers could not be read: {DAEMON_UNREACHABLE_REASON}" in text_of(fragment)
    assert "bg-warn-bg text-warn-fg" in fragment
    assert list(rows_of(fragment)) == ["mysql", "web"]
    assert ">Containers</p>" not in fragment
    assert "Exited" not in fragment and "Running" not in fragment


async def test_a_project_without_containers_names_the_project_and_lists_the_stored_services(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The empty state names the project for the caller who sees it; a caller with one
    environment gets the state without the name."""
    serve(stack.fake)
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert 'data-runtime-state="empty"' in fragment
    assert f"No containers in {PROJECT}" in text_of(fragment)
    assert list(rows_of(fragment)) == ["mysql", "web"]
    assert 'data-fragment="runtime-network"' not in fragment
    assert ">Containers</p>" not in fragment
    await sign_in(web_client, "env_scoped_admin")
    fragment = await read(web_client, stack.runtime)
    assert "No containers yet" in text_of(fragment) and PROJECT not in text_of(fragment)
    assert list(rows_of(fragment)) == ["mysql"]


async def test_a_stored_service_without_a_container_says_so(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """With containers for some services only, the others read No container."""
    serve(
        stack.fake,
        ps=[ps_entry("mysql", 1)],
        inspect=[inspect_entry(1, created=FUTURE)],
        network_ls=[],
        network_inspect=[],
    )
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    rows = rows_of(fragment)
    assert list(rows) == ["mysql", "web"]
    assert "No container" in text_of(rows["web"]) and "No container" not in rows["mysql"]
    assert strip_cell(fragment, "Containers") == "1 of 1 running"
    assert 'data-fragment="runtime-network"' not in fragment


async def test_the_network_panel_is_left_out_when_there_is_nothing_to_show(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Containers with no network, no address and no port give no panel."""
    serve(
        stack.fake,
        ps=[ps_entry("mysql", 1)],
        inspect=[inspect_entry(1, created=FUTURE)],
        network_ls=[],
        network_inspect=[],
    )
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert 'data-fragment="runtime-network"' not in fragment
    assert 'data-fragment="runtime-services"' in fragment


async def test_a_failing_docker_read_keeps_the_page_working(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """A command that fails gives the fixed sentence in the unavailable state, never an error."""
    serve(
        stack.fake,
        ps=[ps_entry("mysql", 1)],
        inspect=[inspect_entry(1, created=FUTURE)],
        fail="inspect",
    )
    await sign_in(web_client, "root")
    fragment = await read(web_client, stack.runtime)
    assert 'data-runtime-state="unavailable"' in fragment
    assert "secret detail" not in fragment


async def test_the_fragment_follows_the_access_rules_of_the_page(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Anonymous callers are refused and a caller who cannot see the deployment gets 404."""
    assert (await web_client.get(stack.runtime)).status_code in (303, 401)
    await sign_in(web_client, "root")
    await read(web_client, "/deployments/99999/runtime", 404)


async def test_add_service_form_takes_the_name_of_a_compose_service(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The link of a service with no files opens the form with its name filled in."""
    await sign_in(web_client, "root")
    page = await read(web_client, f"/deployments/{stack.prod}/services/new?name=redis")
    assert 'name="name"' in page and 'value="redis"' in page
    page = await read(web_client, f"/deployments/{stack.prod}/services/new?name=%22%3Cb%3E")
    assert "&#34;&lt;b&gt;" in page and "<b>" not in page
    page = await read(web_client, f"/deployments/{stack.prod}/services/new")
    assert 'value=""' in page
    await sign_in(web_client, "admin")
    await read(web_client, f"/deployments/{stack.prod}/services/new?name=redis", 403)
