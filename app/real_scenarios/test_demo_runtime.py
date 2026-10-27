"""The runtime of the demo deployment read from the real daemon.

The demo Compose project is started with the real docker command. The runtime then lists its two
services with their state, uptime, restart count, addresses and the default network of the
project with its subnet and gateway; after the containers are stopped it reads the exited state;
and a container created before a deploy wrote its files is marked to be recreated until it is.
"""

from datetime import timedelta

from sqlalchemy import select

from app.models import (
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
from app.services.stack_runtime import RuntimeState, StackRuntime, load_stack_runtime

NETWORK = f"{COMPOSE_PROJECT}_default"


def compose(workloads: Workloads, demo: DemoStack, *arguments: str) -> None:
    """Run a compose command on the demo project."""
    result = workloads.docker(
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "--project-directory",
        str(demo.root),
        "-f",
        str(demo.root / "compose.yaml"),
        *arguments,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def read(world: World) -> StackRuntime:
    """The runtime of the seeded demo deployment as Docker reports it now."""
    async with world.session() as db:
        deployment = await db.get(Deployment, world.demo_seed.deployment.id)
        assert deployment is not None
        assert deployment.compose_project == COMPOSE_PROJECT
        return await load_stack_runtime(db, DockerCli(), deployment)


async def test_the_running_demo_services_their_state_and_their_network(
    world: World, demo: DemoStack, workloads: Workloads
) -> None:
    compose(workloads, demo, "up", "-d")
    runtime = await read(world)
    assert runtime.state is RuntimeState.READY and runtime.reason is None
    assert [(item.name, item.has_files, item.no_container) for item in runtime.services] == [
        ("api", True, False),
        ("db", True, False),
    ]
    assert runtime.running_count == 2 and runtime.recreate_count == 0
    for container in runtime.containers:
        assert container.state == "running" and container.exit_code == 0
        assert container.image == "alpine:3.24.2" and container.ports == ()
        assert container.restart_count == 0 and container.health is None
        assert container.uptime is not None and container.uptime >= timedelta()
        assert container.created_at is not None and container.started_at is not None
        (attached,) = container.networks
        assert attached.name == NETWORK and attached.ip_address
        assert container.service in attached.aliases
    (network,) = runtime.networks
    assert (network.name, network.driver, network.internal, network.external) == (
        NETWORK,
        "bridge",
        False,
        False,
    )
    assert network.subnets and all(item.gateway for item in network.subnets)


async def test_stopped_demo_services_read_as_exited_with_their_finish_time(
    world: World, demo: DemoStack, workloads: Workloads
) -> None:
    compose(workloads, demo, "up", "-d")
    compose(workloads, demo, "stop")
    runtime = await read(world)
    assert runtime.state is RuntimeState.READY and runtime.running_count == 0
    for container in runtime.containers:
        assert container.state == "exited"
        assert container.uptime is None and container.finished_at is not None
        assert container.exit_code is not None


async def test_a_container_created_before_a_deploy_is_marked_until_it_is_recreated(
    world: World, demo: DemoStack, workloads: Workloads
) -> None:
    compose(workloads, demo, "up", "-d")
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
        assert await db.scalar(select(DeployRun.id).where(DeployRun.id == run.id)) == run.id
    marked = {item.name: item.recreate_to_apply for item in (await read(world)).services}
    assert marked == {"api": True, "db": False}

    compose(workloads, demo, "up", "-d", "--no-deps", "--force-recreate", "api")
    marked = {item.name: item.recreate_to_apply for item in (await read(world)).services}
    assert marked == {"api": False, "db": False}
