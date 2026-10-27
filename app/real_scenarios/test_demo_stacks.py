"""Stack discovery against the real daemon and the demo stack.

The demo Compose project is started with the real docker command. The discovery then finds it by
its project name and by its folder, reads the container counts from the real ``compose ls``,
marks it as used by the seeded demo deployment, and after the containers are stopped and removed
it still finds the folder.
"""

from app.real_scenarios.conftest import COMPOSE_PROJECT, DemoStack, Workloads, World
from app.services.docker_cli import DockerCli
from app.services.paths import load_allowed_paths
from app.services.stack_discovery import DetectedStack, StackSource, discover_stacks


async def find_demo(world: World, demo: DemoStack) -> tuple[DetectedStack, str | None]:
    """The stack of the demo folder as discovered now, and why Docker was not used."""
    async with world.session() as db:
        allowed = await load_allowed_paths(db)
        found = await discover_stacks(db, DockerCli(), allowed)
    (stack,) = [stack for stack in found.stacks if stack.folder == str(demo.root.resolve())]
    return stack, found.docker_reason


def start(workloads: Workloads, demo: DemoStack) -> None:
    """Bring the demo containers up through the real compose plugin."""
    result = workloads.docker(
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "--project-directory",
        str(demo.root),
        "-f",
        str(demo.root / "compose.yaml"),
        "up",
        "-d",
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_the_running_demo_project_is_found_by_docker_and_by_its_folder(
    world: World, demo: DemoStack, workloads: Workloads
) -> None:
    start(workloads, demo)
    stack, docker_reason = await find_demo(world, demo)
    assert docker_reason is None
    assert stack.project == COMPOSE_PROJECT and stack.source is StackSource.BOTH
    assert stack.usable and stack.reason is None
    assert stack.compose_files == ("compose.yaml",)
    assert (stack.running, stack.total) == (2, 2)
    assert stack.used_by is not None
    assert stack.used_by.id == world.demo_seed.deployment.id


async def test_stopped_containers_are_counted_and_removed_ones_leave_the_folder(
    world: World, demo: DemoStack, workloads: Workloads
) -> None:
    start(workloads, demo)
    workloads.docker("compose", "-p", COMPOSE_PROJECT, "stop")
    stack, _ = await find_demo(world, demo)
    assert stack.project == COMPOSE_PROJECT and (stack.running, stack.total) == (0, 2)

    workloads.stop()
    stack, docker_reason = await find_demo(world, demo)
    assert docker_reason is None
    assert stack.project is None and stack.source is StackSource.FOLDER
    assert (stack.running, stack.total) == (None, None)
    assert stack.usable and stack.used_by is not None
