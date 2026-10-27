"""Start, stop, restart and recreate a demo container from the Overview routes, on the real daemon.

The web application runs in this process on the throwaway database of the ``world`` fixture and
the host Docker daemon; the superadmin posts what the confirmation dialog posts. Every check is on
the demo containers: their id (a recreate replaces it), their state and when they last started.
The first scenarios run on the project alone, as Compose resolves it from the labels of the
containers (a deployment with no linked Compose files), the others on the linked file.
"""

from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.models import (
    AuditEntry,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
)
from app.models.types import utc_now
from app.real_scenarios.conftest import COMPOSE_PROJECT, DemoStack, Workloads, World
from app.services.docker_cli import DockerCli
from app.services.stack_runtime import load_stack_runtime
from app.tests.conftest import sign_in
from app.tests.web_helpers import events_of, post

COMPOSE_FILE = "compose.yaml"


@dataclass(frozen=True)
class Container:
    """A demo container as ``docker inspect`` reports it."""

    identifier: str
    status: str
    started_at: str


def container(workloads: Workloads, service: str) -> Container:
    """The one container of a service of the demo project."""
    (found,) = workloads.container_ids(service)
    result = workloads.docker(
        "inspect", "--format", "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}", found
    )
    assert result.returncode == 0, result.stderr
    identifier, status, started = result.stdout.strip().split("|")
    return Container(identifier, status, started)


def start_stack(workloads: Workloads, demo: DemoStack) -> None:
    """Create and start the demo containers and wait until they run."""
    result = workloads.docker(
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "--project-directory",
        str(demo.root),
        "-f",
        str(demo.root / COMPOSE_FILE),
        "up",
        "-d",
        "--wait",
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def link_compose_file(world: World) -> None:
    """Link the demo Compose file to the demo deployment."""
    async with world.session() as db:
        deployment = await db.get(Deployment, world.demo_seed.deployment.id)
        assert deployment is not None
        deployment.compose_files = [COMPOSE_FILE]
        await db.commit()


async def act(web: httpx.AsyncClient, world: World, service: str, action: str) -> httpx.Response:
    """Post an action of the confirmation dialog and check that it answered with a toast."""
    url = f"/deployments/{world.demo_seed.deployment.id}/containers/{service}/{action}"
    answered = await post(web, url)
    assert answered.status_code == 200, answered.text
    assert 'role="status"' in answered.text and "was " in answered.text
    assert events_of(answered).keys() >= {"close-dialogs", "containers-changed"}
    return answered


async def audited(world: World) -> list[tuple[str, str, object]]:
    """The container audit entries: action, result and the details."""
    async with world.session() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action.like("container.%")).order_by(AuditEntry.id)
        )
        return [(entry.action, entry.result.value, entry.details) for entry in rows.scalars()]


async def test_without_linked_files_compose_runs_start_stop_and_restart_on_the_project_alone(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    """The project name is enough: Compose finds the containers by their labels, from a folder
    with no Compose file of its own, and every container keeps its id."""
    start_stack(workloads, demo)
    await sign_in(web, "root")
    before = {name: container(workloads, name) for name in ("api", "db")}

    await act(web, world, "api", "restart")
    restarted = container(workloads, "api")
    assert restarted.identifier == before["api"].identifier and restarted.status == "running"
    assert restarted.started_at != before["api"].started_at
    assert container(workloads, "db") == before["db"]

    await act(web, world, "db", "stop")
    stopped = container(workloads, "db")
    assert stopped.identifier == before["db"].identifier and stopped.status == "exited"
    assert container(workloads, "api").status == "running"

    await act(web, world, "db", "start")
    started = container(workloads, "db")
    assert started.identifier == before["db"].identifier and started.status == "running"
    assert started.started_at != before["db"].started_at

    entries = await audited(world)
    assert [(action, result) for action, result, _ in entries] == [
        ("container.restart", "success"),
        ("container.stop", "success"),
        ("container.start", "success"),
    ]
    assert [details for _, _, details in entries] == [
        {"service": service, "project": COMPOSE_PROJECT, "exit_code": 0}
        for service in ("api", "db", "db")
    ]


async def test_recreate_without_linked_files_is_refused_and_touches_nothing(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    start_stack(workloads, demo)
    await sign_in(web, "root")
    before = container(workloads, "api")
    url = f"/deployments/{world.demo_seed.deployment.id}/containers/api/recreate"
    refused = await post(web, url)
    assert refused.status_code == 409 and "none are linked" in refused.text
    assert container(workloads, "api") == before
    assert [(action, result) for action, result, _ in await audited(world)] == [
        ("container.recreate", "blocked")
    ]


async def test_with_the_compose_file_linked_the_actions_run_on_it(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    start_stack(workloads, demo)
    await link_compose_file(world)
    await sign_in(web, "root")
    before = container(workloads, "db")
    await act(web, world, "db", "restart")
    after = container(workloads, "db")
    assert after.identifier == before.identifier and after.started_at != before.started_at
    await act(web, world, "api", "stop")
    assert container(workloads, "api").status == "exited"
    await act(web, world, "api", "start")
    assert container(workloads, "api").status == "running"


async def test_recreate_replaces_the_container_though_its_definition_did_not_change(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    """Without ``--force-recreate`` Compose leaves a container alone when its configuration is
    unchanged, which is what a deploy of a file the definition does not name leaves behind."""
    start_stack(workloads, demo)
    await link_compose_file(world)
    await sign_in(web, "root")
    before = {name: container(workloads, name) for name in ("api", "db")}
    await act(web, world, "api", "recreate")
    api = container(workloads, "api")
    assert api.identifier != before["api"].identifier and api.status == "running"
    assert container(workloads, "db") == before["db"]
    assert len(workloads.container_ids("api")) == 1


async def test_a_recreate_loads_the_files_a_deploy_wrote_and_clears_the_mark(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    start_stack(workloads, demo)
    await link_compose_file(world)
    api = world.demo_seed.api
    async with world.session() as db:
        run = DeployRun(
            service_id=api.id,
            service_name=api.name,
            deployment_name=world.demo_seed.deployment.name,
            trigger=DeployTrigger.UI,
            status=DeployStatus.SUCCESS,
            started_at=utc_now() - timedelta(seconds=1),
        )
        db.add(run)
        await db.flush()
        db.add(
            DeployResult(
                run_id=run.id,
                environment_name=".env.api",
                target_path="environments/.env.api",
                status=DeployResultStatus.WRITTEN,
            )
        )
        await db.commit()

    async def marked() -> dict[str, bool]:
        async with world.session() as db:
            deployment = await db.get(Deployment, world.demo_seed.deployment.id)
            assert deployment is not None
            runtime = await load_stack_runtime(db, DockerCli(), deployment)
            return {item.name: item.recreate_to_apply for item in runtime.services}

    assert await marked() == {"api": True, "db": False}
    await sign_in(web, "root")
    await act(web, world, "api", "restart")
    assert await marked() == {"api": True, "db": False}
    await act(web, world, "api", "recreate")
    assert await marked() == {"api": False, "db": False}


async def test_a_service_the_project_does_not_have_is_not_found_and_nothing_runs(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    start_stack(workloads, demo)
    await sign_in(web, "root")
    before = container(workloads, "api")
    base = f"/deployments/{world.demo_seed.deployment.id}/containers"
    for name in ("nope", "api;db", "-a"):
        assert (await post(web, f"{base}/{name}/restart")).status_code == 404
    assert container(workloads, "api") == before and await audited(world) == []
