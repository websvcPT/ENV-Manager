"""The runtime of a deployment's Compose project: containers, ports, networks, matching with the
stored services, "recreate to apply", the cache and every state that is not a container list.

Docker is the fake executable (a real script); the database is a real temporary SQLite file.
"""

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import (
    DEPLOYMENT_FILES_NAME,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Project,
    Service,
)
from app.services.docker_cli import (
    CLI_MISSING_REASON,
    DAEMON_UNREACHABLE_REASON,
    NOT_MOUNTED_REASON,
    DockerCli,
)
from app.services.docker_query import FAILED_MESSAGE
from app.services.stack_runtime import RuntimeCache, RuntimeState, StackRuntime, load_stack_runtime
from app.services.stack_runtime_reads import (
    CONTAINER_TEMPLATE,
    NETWORK_TEMPLATE,
    UNREADABLE_REASON,
    PortBind,
    parse_time,
)
from app.tests.fake_docker import FakeDocker
from app.tests.runtime_helpers import (
    CREATED,
    ZERO_TIME,
    attached,
    container_id,
    inspect_entry,
    make_fake,
    network_entry,
    ps_entry,
    publisher,
    recorded,
    serve,
)

PROJECT = "demo"
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 10, 27, 10, 0, 0, 123456, tzinfo=UTC)
FORBIDDEN_PATHS = (
    ".Config",
    ".Env",
    "Health.Log",
    ".Log",
    ".HostConfig",
    ".Mounts",
    ".Containers",
    ".Options",
    ".Labels",
)


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker that knows an empty project."""
    return make_fake(tmp_path / "fake")


async def add_deployment(
    engine: AsyncEngine, services: tuple[str, ...] = (), compose_project: str | None = PROJECT
) -> tuple[int, dict[str, int]]:
    """A deployment with the named services, and the ids of both."""
    async with create_session_factory(engine)() as db:
        owner = Project(name=f"project {uuid4().hex[:8]}")
        db.add(owner)
        await db.flush()
        deployment = Deployment(
            project_id=owner.id,
            name="stack",
            root_path="/srv/stack",
            compose_project=compose_project,
        )
        db.add(deployment)
        await db.flush()
        rows = {name: Service(deployment_id=deployment.id, name=name) for name in services}
        db.add_all(rows.values())
        await db.commit()
        return deployment.id, {name: row.id for name, row in rows.items()}


async def add_run(
    engine: AsyncEngine,
    deployment_id: int,
    service_id: int,
    written_at: datetime,
    *,
    status: DeployStatus = DeployStatus.SUCCESS,
    result: DeployResultStatus = DeployResultStatus.WRITTEN,
    dry_run: bool = False,
    trigger: DeployTrigger = DeployTrigger.UI,
) -> None:
    """One recorded deploy of a service with a single result recorded at ``written_at``."""
    async with create_session_factory(engine)() as db:
        run = DeployRun(
            service_id=service_id,
            service_name="service",
            deployment_name="stack",
            trigger=trigger,
            dry_run=dry_run,
            status=status,
            started_at=written_at - timedelta(seconds=1),
        )
        db.add(run)
        await db.flush()
        db.add(
            DeployResult(
                run_id=run.id,
                environment_name=".env",
                target_path=".env",
                status=result,
                created_at=written_at,
            )
        )
        await db.commit()


async def load(
    engine: AsyncEngine,
    cli: DockerCli,
    deployment_id: int,
    *,
    now: datetime = NOW,
    cache: RuntimeCache | None = None,
) -> StackRuntime:
    """Load the runtime of a deployment."""
    async with create_session_factory(engine)() as db:
        deployment = await db.get(Deployment, deployment_id)
        assert deployment is not None
        return await load_stack_runtime(db, cli, deployment, now=now, cache=cache)


def running_stack(fake: FakeDocker, *services: str) -> None:
    """The fake answers one running container per service on the project's default network."""
    entries = [ps_entry(name, number) for number, name in enumerate(services, start=1)]
    details = [
        inspect_entry(number, networks=(attached("demo_default", f"172.20.0.{number + 1}"),))
        for number in range(1, len(services) + 1)
    ]
    serve(
        fake,
        ps=entries,
        inspect=details,
        network_ls=["demo_default"],
        network_inspect=[network_entry("demo_default", subnets=(("172.20.0.0/16", "172.20.0.1"),))],
    )


async def test_a_deployment_without_a_project_says_so_and_asks_docker_nothing(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    for project in (None, "", "   "):
        deployment_id, _ = await add_deployment(engine, compose_project=project)
        runtime = await load(engine, fake.cli(), deployment_id)
        assert runtime.state is RuntimeState.NO_PROJECT
        assert runtime.project is None and runtime.services == () and runtime.networks == ()
    assert fake.probes() == 0 and fake.calls() == []


async def test_docker_that_is_not_usable_gives_its_fixed_reason(
    engine: AsyncEngine, fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    runtime = await load(engine, fake.cli(mounted=False), deployment_id)
    assert (runtime.state, runtime.reason) == (RuntimeState.UNAVAILABLE, NOT_MOUNTED_REASON)
    assert runtime.project == PROJECT and runtime.services == ()

    fake.switch("no-daemon")
    runtime = await load(engine, fake.cli(), deployment_id)
    assert (runtime.state, runtime.reason) == (RuntimeState.UNAVAILABLE, DAEMON_UNREACHABLE_REASON)

    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    cli = DockerCli(socket_path=fake.cli().socket_path, scratch=fake.folder / "scratch")
    runtime = await load(engine, cli, deployment_id)
    assert (runtime.state, runtime.reason) == (RuntimeState.UNAVAILABLE, CLI_MISSING_REASON)
    assert fake.calls() == []


async def test_a_project_without_containers_is_empty_and_its_services_have_no_container(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, ids = await add_deployment(engine, ("api", "db"))
    for printed in ("", "[]", "\n"):
        serve(fake, ps=printed)
        runtime = await load(engine, fake.cli(), deployment_id)
        assert runtime.state is RuntimeState.EMPTY and runtime.reason is None
        assert [(item.name, item.service_id, item.no_container) for item in runtime.services] == [
            ("api", ids["api"], True),
            ("db", ids["db"], True),
        ]
    assert {call[0] for call in recorded(fake)} == {"compose"}


async def test_both_shapes_of_the_container_list_are_read(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api", "db"))
    entries = [ps_entry("db", 2, image="postgres:17"), ps_entry("api", 1)]
    found = []
    for printed in (entries, ("array", entries)):
        serve(fake, ps=printed, inspect=[inspect_entry(1), inspect_entry(2)])
        runtime = await load(engine, fake.cli(), deployment_id)
        assert runtime.state is RuntimeState.READY
        found.append([(c.service, c.name, c.container_id, c.image) for c in runtime.containers])
    assert (
        found[0]
        == found[1]
        == [
            ("api", "demo-api-1", container_id(1), "alpine:3.24.2"),
            ("db", "demo-db-2", container_id(2), "postgres:17"),
        ]
    )


async def test_the_reads_use_only_field_templates_and_the_project(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api", "db"))
    running_stack(fake, "api", "db")
    await load(engine, fake.cli(), deployment_id)
    ids = [container_id(1), container_id(2)]
    assert recorded(fake) == [
        ("compose", "-p", PROJECT, "ps", "--all", "--format", "json"),
        ("inspect", "--type", "container", "--format", CONTAINER_TEMPLATE, *ids),
        (
            "network",
            "ls",
            "--filter",
            "label=com.docker.compose.project=demo",
            "--format",
            "json",
        ),
        ("network", "inspect", "--format", NETWORK_TEMPLATE, "demo_default"),
    ]
    environment = fake.calls()[0].environment
    assert environment["COMPOSE_EXPERIMENTAL_GIT_REMOTE"] == "false"


def test_the_templates_select_single_fields_and_never_the_environment_or_probe_output() -> None:
    for template in (CONTAINER_TEMPLATE, NETWORK_TEMPLATE):
        for forbidden in FORBIDDEN_PATHS:
            # the address ranges of a network are the one field named Config that is read
            assert forbidden not in template.replace(".IPAM.Config", ".IPAM.Ranges")
        assert template.count("{{") == template.count("}}")
    selected = {
        ".Id",
        ".State.Status",
        ".State.Health.Status",
        ".State.StartedAt",
        ".State.FinishedAt",
        ".State.ExitCode",
        ".RestartCount",
        ".Created",
        ".NetworkSettings.Networks",
        "$network.IPAddress",
        "$network.GlobalIPv6Address",
        "$network.Aliases",
    }
    for path in selected:
        assert path in CONTAINER_TEMPLATE
    for path in (".Name", ".Driver", ".Internal", ".IPAM.Config", ".Subnet", ".Gateway"):
        assert path in NETWORK_TEMPLATE
    assert CONTAINER_TEMPLATE == (
        '{"id":{{json .Id}},"status":{{json .State.Status}},'
        '"health":{{if index .State "Health"}}{{json .State.Health.Status}}{{else}}null{{end}},'
        '"started":{{json .State.StartedAt}},"finished":{{json .State.FinishedAt}},'
        '"exit":{{json .State.ExitCode}},"restarts":{{json .RestartCount}},'
        '"created":{{json .Created}},'
        '"networks":[{{range $name, $network := .NetworkSettings.Networks}}'
        '{"name":{{json $name}},"ip":{{json $network.IPAddress}},'
        '"ipv6":{{json $network.GlobalIPv6Address}},"aliases":{{json $network.Aliases}}},'
        "{{end}}null]}"
    )


async def test_state_health_uptime_restarts_and_addresses_come_from_inspect(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api", "db", "job"))
    serve(
        fake,
        ps=[
            ps_entry("api", 1, health="healthy"),
            ps_entry("db", 2, "exited", exit_code=137),
            ps_entry("job", 3, "created"),
        ],
        inspect=[
            inspect_entry(
                1,
                health="unhealthy",
                started="2026-10-27T11:00:00.999999999Z",
                restarts=3,
                networks=(
                    attached("b-net", "10.0.0.5", ("api", "abc"), ipv6="fd00::5"),
                    attached("a-net", "172.20.0.2"),
                ),
            ),
            inspect_entry(
                2,
                status="exited",
                started="2026-10-27T09:00:00Z",
                finished="2026-10-27T11:30:00.25Z",
                exit_code=137,
                restarts=1,
            ),
            inspect_entry(3, status="created", started=ZERO_TIME),
        ],
        network_inspect=[network_entry("a-net"), network_entry("b-net")],
    )
    runtime = await load(engine, fake.cli(), deployment_id)
    api, db, job = runtime.containers
    assert (api.state, api.health, api.restart_count) == ("running", "unhealthy", 3)
    assert api.started_at == datetime(2026, 10, 27, 11, 0, 0, 999999, tzinfo=UTC)
    assert api.uptime == NOW - api.started_at and api.exit_code == 0
    assert api.created_at == CREATED_AT
    assert [(n.name, n.ip_address, n.ipv6_address, n.aliases) for n in api.networks] == [
        ("a-net", "172.20.0.2", "", ("api",)),
        ("b-net", "10.0.0.5", "fd00::5", ("api", "abc")),
    ]
    assert (db.state, db.exit_code, db.restart_count) == ("exited", 137, 1)
    assert db.uptime is None
    assert db.finished_at == datetime(2026, 10, 27, 11, 30, 0, 250000, tzinfo=UTC)
    assert (job.state, job.started_at, job.health) == ("created", None, None)
    assert runtime.running_count == 1


async def test_published_ports_flag_binds_on_every_interface(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    serve(
        fake,
        ps=[
            ps_entry(
                "api",
                1,
                publishers=(
                    publisher("0.0.0.0", 8080, 80),  # noqa: S104
                    publisher("::", 8080, 80),
                    publisher("", 9000, 9000, "udp"),
                    publisher("127.0.0.1", 5432, 5432),
                    publisher("::1", 5432, 5432),
                    publisher("192.168.10.4", 3000, 3000),
                    publisher("", 0, 443),
                ),
            )
        ],
        inspect=[inspect_entry(1)],
    )
    (container,) = (await load(engine, fake.cli(), deployment_id)).containers
    ports = container.ports
    shown = [(p.host_ip, p.host_port, p.container_port, p.protocol, p.bind) for p in ports]
    assert shown == [
        ("0.0.0.0", 8080, 80, "tcp", PortBind.ALL_INTERFACES),  # noqa: S104
        ("::", 8080, 80, "tcp", PortBind.ALL_INTERFACES),
        ("", 9000, 9000, "udp", PortBind.ALL_INTERFACES),
        ("127.0.0.1", 5432, 5432, "tcp", PortBind.THIS_HOST),
        ("::1", 5432, 5432, "tcp", PortBind.THIS_HOST),
        ("192.168.10.4", 3000, 3000, "tcp", PortBind.ADDRESS),
    ]


async def test_networks_of_the_project_and_the_external_ones_the_containers_join(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api", "db"))
    serve(
        fake,
        ps=[ps_entry("api", 1), ps_entry("db", 2)],
        inspect=[
            inspect_entry(1, networks=(attached("demo_default"), attached("shared-proxy"))),
            inspect_entry(2, networks=(attached("demo_default"),)),
        ],
        network_ls=["demo_default", "demo_backend"],
        network_inspect=[
            network_entry("shared-proxy", subnets=(("10.9.0.0/24", "10.9.0.1"),)),
            network_entry(
                "demo_backend", internal=True, subnets=(("172.21.0.0/16", "172.21.0.1"),)
            ),
            network_entry(
                "demo_default",
                subnets=(("172.20.0.0/16", "172.20.0.1"), ("fd00:1::/64", "fd00:1::1")),
            ),
        ],
    )
    runtime = await load(engine, fake.cli(), deployment_id)
    assert recorded(fake)[-1][4:] == ("demo_backend", "demo_default", "shared-proxy")
    assert [(n.name, n.driver, n.internal, n.external) for n in runtime.networks] == [
        ("demo_backend", "bridge", True, False),
        ("demo_default", "bridge", False, False),
        ("shared-proxy", "bridge", False, True),
    ]
    default = runtime.networks[1]
    assert [(s.subnet, s.gateway) for s in default.subnets] == [
        ("172.20.0.0/16", "172.20.0.1"),
        ("fd00:1::/64", "fd00:1::1"),
    ]


async def test_compose_services_and_stored_services_are_matched_both_ways(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, ids = await add_deployment(engine, ("api", "worker"))
    async with create_session_factory(engine)() as db:
        db.add(Service(deployment_id=deployment_id, name=DEPLOYMENT_FILES_NAME))
        await db.commit()
    running_stack(fake, "api", "cache")
    runtime = await load(engine, fake.cli(), deployment_id)
    assert [
        (s.name, s.service_id, s.has_files, s.no_container, len(s.containers))
        for s in runtime.services
    ] == [
        ("api", ids["api"], True, False, 1),
        ("cache", None, False, False, 1),
        ("worker", ids["worker"], True, True, 0),
    ]

    serve(fake, ps=[])
    runtime = await load(engine, fake.cli(), deployment_id)
    assert [s.name for s in runtime.services] == ["api", "worker"]


async def test_a_container_created_before_the_last_write_is_marked_to_recreate(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, ids = await add_deployment(engine, ("api", "db", "cache", "idle"))
    serve(
        fake,
        ps=[ps_entry("api", 1), ps_entry("db", 2), ps_entry("cache", 3), ps_entry("web", 4)],
        inspect=[
            inspect_entry(1, created="2026-10-27T10:00:00.5Z", started="2026-10-27T11:59:00Z"),
            inspect_entry(2, created="2026-10-27T10:00:00.5Z"),
            inspect_entry(3, created="2026-10-27T10:00:00.5Z"),
            inspect_entry(4, created="2026-10-27T09:00:00Z"),
        ],
    )
    written = datetime(2026, 10, 27, 10, 0, 1, tzinfo=UTC)
    # api: written after its container was created, restarted since (StartedAt is not the test)
    await add_run(engine, deployment_id, ids["api"], written - timedelta(hours=2))
    await add_run(engine, deployment_id, ids["api"], written)
    # db: the newest write is older than its container
    await add_run(engine, deployment_id, ids["db"], written - timedelta(seconds=5))
    # cache: only deploys that wrote nothing
    await add_run(engine, deployment_id, ids["cache"], written, dry_run=True)
    await add_run(engine, deployment_id, ids["cache"], written, trigger=DeployTrigger.COMPOSE)
    await add_run(engine, deployment_id, ids["cache"], written, result=DeployResultStatus.UNCHANGED)
    runtime = await load(engine, fake.cli(), deployment_id)
    by_service = {item.name: item for item in runtime.services}
    assert by_service["api"].recreate_to_apply is True
    assert by_service["api"].last_written_at == written
    assert by_service["db"].recreate_to_apply is False
    assert by_service["cache"].recreate_to_apply is False
    assert by_service["cache"].last_written_at is None
    assert by_service["web"].recreate_to_apply is False and by_service["web"].service_id is None
    assert by_service["idle"].no_container and by_service["idle"].recreate_to_apply is False
    assert runtime.recreate_count == 1


@pytest.mark.parametrize(
    "status", [DeployStatus.POST_DEPLOY_FAILED, DeployStatus.FAILED, DeployStatus.HALTED]
)
async def test_a_run_that_ended_badly_after_writing_a_file_still_marks_the_container(
    engine: AsyncEngine, fake: FakeDocker, status: DeployStatus
) -> None:
    deployment_id, ids = await add_deployment(engine, ("api",))
    serve(fake, ps=[ps_entry("api", 1)], inspect=[inspect_entry(1)])
    await add_run(
        engine,
        deployment_id,
        ids["api"],
        CREATED_AT + timedelta(minutes=1),
        status=status,
    )
    (container,) = (await load(engine, fake.cli(), deployment_id)).containers
    assert container.recreate_to_apply is True


async def test_a_container_without_a_creation_time_is_not_marked(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, ids = await add_deployment(engine, ("api",))
    serve(fake, ps=[ps_entry("api", 1)], inspect=[inspect_entry(1, created="")])
    await add_run(engine, deployment_id, ids["api"], NOW)
    (container,) = (await load(engine, fake.cli(), deployment_id)).containers
    assert container.created_at is None and container.recreate_to_apply is False


async def test_docker_answers_are_kept_for_five_seconds_per_deployment_and_project(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    other_id, _ = await add_deployment(engine, ("api",))
    running_stack(fake, "api")
    cli = fake.cli()
    clock = [100.0]
    cache = RuntimeCache(clock=lambda: clock[0])

    def asked() -> int:
        return sum(1 for call in recorded(fake) if call[0] == "compose")

    first = await load(engine, cli, deployment_id, now=NOW, cache=cache)
    clock[0] += 4.9
    second = await load(engine, cli, deployment_id, now=NOW + timedelta(seconds=4), cache=cache)
    assert asked() == 1
    first_uptime = first.containers[0].uptime
    assert first_uptime is not None
    assert second.containers[0].uptime == first_uptime + timedelta(seconds=4)
    await load(engine, cli, other_id, cache=cache)
    assert asked() == 2
    clock[0] += 0.2
    await load(engine, cli, deployment_id, cache=cache)
    assert asked() == 3

    async with create_session_factory(engine)() as db:
        deployment = await db.get(Deployment, deployment_id)
        assert deployment is not None
        deployment.compose_project = "other"
        await db.commit()
    await load(engine, cli, deployment_id, cache=cache)
    assert asked() == 4


async def test_viewers_at_the_same_moment_share_one_set_of_docker_commands(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    running_stack(fake, "api")
    cli = fake.cli()
    results = await asyncio.gather(*(load(engine, cli, deployment_id) for _ in range(5)))
    assert all(result.state is RuntimeState.READY for result in results)
    assert len(fake.calls()) == 4
    await load(engine, cli, deployment_id)
    assert len(fake.calls()) == 4


async def test_a_failure_is_kept_for_the_same_five_seconds(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    serve(fake, fail="ps")
    cli = fake.cli()
    clock = [0.0]
    cache = RuntimeCache(clock=lambda: clock[0])
    assert (await load(engine, cli, deployment_id, cache=cache)).state is RuntimeState.UNAVAILABLE
    running_stack(fake, "api")
    assert (await load(engine, cli, deployment_id, cache=cache)).state is RuntimeState.UNAVAILABLE
    clock[0] = 5.0
    assert (await load(engine, cli, deployment_id, cache=cache)).state is RuntimeState.READY


@pytest.mark.parametrize("failing", ["ps", "inspect", "network_ls", "network_inspect"])
async def test_a_failing_command_becomes_a_state_with_the_fixed_sentence(
    engine: AsyncEngine, fake: FakeDocker, failing: str
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    serve(
        fake,
        ps=[ps_entry("api", 1)],
        inspect=[inspect_entry(1, networks=(attached("demo_default"),))],
        network_ls=["demo_default"],
        network_inspect=[network_entry("demo_default")],
        fail=failing,
    )
    runtime = await load(engine, fake.cli(), deployment_id)
    assert (runtime.state, runtime.reason) == (RuntimeState.UNAVAILABLE, FAILED_MESSAGE)
    assert runtime.services == () and runtime.networks == ()
    assert "secret" not in repr(runtime)


async def test_lines_that_are_not_json_or_not_containers_are_skipped(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    good = json.dumps(ps_entry("api", 1))
    noise = "\n".join(
        [
            "time=2026 level=warning msg=something",
            good,
            "{broken",
            json.dumps({"ID": "short", "Service": "x"}),
            json.dumps({"ID": container_id(9)}),
            json.dumps([1, 2]),
            "",
        ]
    )
    serve(fake, ps=noise, inspect=[inspect_entry(1)])
    runtime = await load(engine, fake.cli(), deployment_id)
    assert runtime.state is RuntimeState.READY
    assert [container.service for container in runtime.containers] == ["api"]


@pytest.mark.parametrize(
    "printed", ["not json", "{broken", "[1, 2", "[1]", '{"foo": 1}', '"text"', '{"ID": "x"}']
)
async def test_an_answer_with_no_container_in_it_is_reported_as_unreadable(
    engine: AsyncEngine, fake: FakeDocker, printed: str
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    serve(fake, ps=printed)
    runtime = await load(engine, fake.cli(), deployment_id)
    assert (runtime.state, runtime.reason) == (RuntimeState.UNAVAILABLE, UNREADABLE_REASON)
    assert {call[0] for call in recorded(fake)} == {"compose"}


async def test_a_container_without_readable_details_keeps_what_the_list_said(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, ids = await add_deployment(engine, ("api",))
    await add_run(engine, deployment_id, ids["api"], NOW)
    serve(
        fake,
        ps=[ps_entry("api", 1, health="healthy", exit_code=0)],
        inspect="garbage\n" + json.dumps({"id": container_id(7)}) + "\n",
    )
    (container,) = (await load(engine, fake.cli(), deployment_id)).containers
    assert (container.state, container.health, container.exit_code) == ("running", "healthy", 0)
    assert container.created_at is None and container.restart_count is None
    assert container.uptime is None and container.recreate_to_apply is False
    assert container.networks == ()


def test_the_times_of_docker_are_read_with_nanoseconds_and_the_zero_time_is_none() -> None:
    assert parse_time(CREATED) == CREATED_AT
    assert parse_time("2026-10-27T10:00:00Z") == datetime(2026, 10, 27, 10, tzinfo=UTC)
    assert parse_time("2026-10-27T12:00:00.5+02:00") == datetime(
        2026, 10, 27, 10, 0, 0, 500000, tzinfo=UTC
    )
    for value in (ZERO_TIME, "", "yesterday", None, 5):
        assert parse_time(value) is None


async def test_short_container_ids_of_the_list_find_their_inspected_details(
    engine: AsyncEngine, fake: FakeDocker
) -> None:
    deployment_id, _ = await add_deployment(engine, ("api",))
    entry = ps_entry("api", 1)
    entry["ID"] = container_id(1)[:12]
    serve(fake, ps=[entry], inspect=[inspect_entry(1, restarts=2)])
    (container,) = (await load(engine, fake.cli(), deployment_id)).containers
    assert container.restart_count == 2 and container.created_at == CREATED_AT
    assert recorded(fake)[1][-1] == container_id(1)[:12]
