"""Importing the demo stack's Compose file into a scratch deployment, then deploying it.

The web application runs in this process on the throwaway database of the ``world`` fixture; the
superadmin goes through the import screens as the browser does, on the real
``demo/stack/compose.yaml`` and the real files below it. A scratch deployment with no services
stands on the same folder as the seeded demo deployment. Every check is on the real files (what
the import left alone, what the deploy wrote, mode, backups, what was not touched) and on the
runs and results recorded.
"""

import html
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.models import (
    DEPLOYMENT_FILES_NAME,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    PostDeployStatus,
    Service,
)
from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.helpers import BASELINE_NAMES, deploy_as, snapshot
from app.services.deploy_trigger import COMPOSE_IMPORT_LABEL
from app.services.services import find_deployment_files
from app.tests.conftest import sign_in
from app.tests.web_helpers import post

SERVICES = ["api", "db"]
OWNERS = {
    "environments/.env.api": "api",
    "environments/.env.db": "db",
    "environments/.env.db.secrets": "db",
}
SECOND_VERSION = "DEMO_SCRATCH_VERSION=2\n"


def hidden_fields(page: str) -> dict[str, list[str]]:
    """The hidden inputs of a fragment by name."""
    fields: dict[str, list[str]] = {}
    for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page):
        fields.setdefault(name, []).append(value)
    return fields


async def scratch_deployment(world: World) -> int:
    """A deployment with no services on the folder of the demo stack; returns its id."""
    async with world.session() as db:
        scratch = Deployment(
            project_id=world.demo_seed.project.id,
            name="scratch-import",
            root_path=world.demo_seed.deployment.root_path,
        )
        db.add(scratch)
        await db.commit()
        return scratch.id


async def import_demo(web: httpx.AsyncClient, deployment: int) -> str:
    """Go through the three steps with the demo's own choices; returns the address it ends on."""
    base = f"/deployments/{deployment}/import-compose"
    assert "compose.yaml" in (await web.get(base)).text
    preview = await post(web, f"{base}/preview", {"compose_file": ["compose.yaml"]})
    page = preview.text
    services = re.findall(r'data-fragment="import-service-row" data-service="([^"]+)"', page)
    files = re.findall(r'data-fragment="import-file-row" data-file="([^"]+)"', page)
    assert sorted(services) == SERVICES and sorted(files) == sorted(OWNERS)
    form: dict[str, object] = {"compose_file": ["compose.yaml"], "service": SERVICES}
    form["file"] = list(OWNERS)
    for key, owner in OWNERS.items():
        form[f"owner:{key}"] = owner
        form[f"type:{key}"] = "env"
    confirm = await post(web, f"{base}/confirm", form)
    assert confirm.status_code == 200
    done = await post(web, base, dict(hidden_fields(confirm.text)))
    assert done.status_code == 200
    return done.headers["hx-redirect"]


async def runs_of(world: World, service: int) -> list[DeployRun]:
    """The runs of a service, oldest first."""
    async with world.session() as db:
        rows = await db.execute(
            select(DeployRun).where(DeployRun.service_id == service).order_by(DeployRun.id)
        )
        return list(rows.scalars())


async def results_by_path(world: World, run: int) -> dict[str, DeployResult]:
    """The results of a run by target path."""
    async with world.session() as db:
        rows = await db.execute(select(DeployResult).where(DeployResult.run_id == run))
        return {result.target_path: result for result in rows.scalars()}


async def test_an_imported_file_edited_to_version_two_deploys_without_a_halt(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    on_disk = {name: (demo.targets / name).read_bytes() for name in BASELINE_NAMES}
    before = snapshot(demo.targets)
    mode_before = stat.S_IMODE((demo.targets / ".env.api").stat().st_mode)
    scratch = await scratch_deployment(world)
    await sign_in(web, "root")

    address = await import_demo(web, scratch)

    assert address.startswith(f"/deployments/{scratch}?imported=2,3,3,0,")
    assert snapshot(demo.targets) == before
    page = (await web.get(f"/deployments/{scratch}")).text
    assert page.count(COMPOSE_IMPORT_LABEL) >= 3
    async with world.session() as db:
        imported = await db.get(Deployment, scratch)
        assert imported is not None and imported.compose_files == ["compose.yaml"]
        services = {
            service.name: service.id
            for service in await db.scalars(select(Service).where(Service.deployment_id == scratch))
        }
        environments = {
            environment.target_path: environment
            for environment in await db.scalars(
                select(Environment).where(Environment.service_id.in_(services.values()))
            )
        }
    assert sorted(services) == SERVICES and sorted(environments) == sorted(OWNERS)

    for service, targets in (
        ("api", ["environments/.env.api"]),
        ("db", ["environments/.env.db", "environments/.env.db.secrets"]),
    ):
        (run,) = await runs_of(world, services[service])
        assert (run.trigger, run.status, run.dry_run) == (
            DeployTrigger.COMPOSE,
            DeployStatus.SUCCESS,
            False,
        )
        results = await results_by_path(world, run.id)
        assert sorted(results) == targets
        for target, result in results.items():
            assert result.status is DeployResultStatus.UNCHANGED
            assert result.written_hash == world.crypto.content_hash(
                on_disk[target.rsplit("/", 1)[1]]
            )

    api_file = environments["environments/.env.api"]
    await world.save_version(api_file.id, on_disk[".env.api"].decode() + SECOND_VERSION)

    deployed = await deploy_as(world, "root", services["api"])

    assert deployed.status is DeployStatus.SUCCESS and deployed.run_id is not None
    written = await results_by_path(world, deployed.run_id)
    assert {path: result.status for path, result in written.items()} == {
        "environments/.env.api": DeployResultStatus.WRITTEN
    }
    assert written["environments/.env.api"].backup_path is None
    assert (demo.targets / ".env.api").read_text() == (
        on_disk[".env.api"].decode() + SECOND_VERSION
    )
    assert stat.S_IMODE((demo.targets / ".env.api").stat().st_mode) == mode_before
    assert set(snapshot(demo.targets)) == BASELINE_NAMES
    for name in BASELINE_NAMES - {".env.api"}:
        assert snapshot(demo.targets)[name] == before[name]
        assert (demo.targets / name).read_bytes() == on_disk[name]

    second = await deploy_as(world, "root", services["db"])

    assert second.status is DeployStatus.SUCCESS and second.run_id is not None
    unchanged = await results_by_path(world, second.run_id)
    assert {result.status for result in unchanged.values()} == {DeployResultStatus.UNCHANGED}
    assert snapshot(demo.targets)[".env.db"] == before[".env.db"]
    assert snapshot(demo.targets)[".env.db.secrets"] == before[".env.db.secrets"]

    api_runs = await runs_of(world, services["api"])
    assert [run.trigger for run in api_runs] == [DeployTrigger.COMPOSE, DeployTrigger.UI]
    async with world.session() as db:
        numbers = select(EnvironmentVersion.number).where(
            EnvironmentVersion.environment_id == api_file.id
        )
        assert sorted(await db.scalars(numbers)) == [1, 2]
    history = (await web.get(f"/deployments/{scratch}/history")).text
    assert history.count("data-history-trigger") == 2
    assert workloads.container_ids() == []


STACK_UP = "docker compose -f compose.yaml up -d"
COMMANDS = {
    "api": f"{STACK_UP} --no-deps api",
    "db": f"{STACK_UP} --no-deps db",
    DEPLOYMENT_FILES_NAME: STACK_UP,
}
API_NAMES = {"DEMO_API_MODE", "DEMO_API_PORT", "DEMO_API_TOKEN"}
STACK_ENV_TEXT = "DEMO_STACK_NOTE=from-the-scenario\n"


@pytest.fixture
def stack_env(demo: DemoStack) -> Iterator[Path]:
    """A deployment .env beside the demo Compose file for the time of a scenario: the demo stack
    has none, and Compose reads it without the stack using any variable from it."""
    path = demo.root / ".env"
    path.write_text(STACK_ENV_TEXT, encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


async def test_an_import_with_commands_brings_the_stack_up_and_recreates_one_service(
    demo: DemoStack,
    world: World,
    web: httpx.AsyncClient,
    workloads: Workloads,
    stack_env: Path,
) -> None:
    compose_before = (demo.root / "compose.yaml").read_bytes()
    scratch = await scratch_deployment(world)
    await sign_in(web, "root")
    base = f"/deployments/{scratch}/import-compose"
    page = (await post(web, f"{base}/preview", {"compose_file": ["compose.yaml"]})).text
    own = re.findall(r'data-fragment="import-deployment-file-row" data-file="([^"]+)"', page)
    assert own == [".env"]
    offered = re.findall(r'<input type="text" name="command:([^"]+)" value="([^"]*)"', page)
    assert {owner: html.unescape(text) for owner, text in offered} == COMMANDS
    form: dict[str, object] = {"compose_file": ["compose.yaml"], "service": SERVICES}
    form["file"] = list(OWNERS)
    for key, owner in OWNERS.items():
        form[f"owner:{key}"] = owner
        form[f"type:{key}"] = "env"
    form["deployment_file"] = [".env"]
    form["type:.env"] = "env"
    for owner in ("api", DEPLOYMENT_FILES_NAME):
        form[f"run:{owner}"] = "1"
        form[f"command:{owner}"] = COMMANDS[owner]
    confirm = await post(web, f"{base}/confirm", form)
    assert confirm.status_code == 200 and 'data-import-count="commands">2<' in confirm.text
    done = await post(web, base, dict(hidden_fields(confirm.text)))
    assert done.status_code == 200 and done.headers["hx-redirect"].endswith(",2")
    assert (demo.root / "compose.yaml").read_bytes() == compose_before
    assert workloads.container_ids() == []
    page = (await web.get(f"/deployments/{scratch}")).text
    assert 'data-fragment="compose-file-row" data-file="compose.yaml"' in page
    async with world.session() as db:
        files = await find_deployment_files(db, scratch)
        assert files is not None
        services = {
            service.name: service.id
            for service in await db.scalars(select(Service).where(Service.deployment_id == scratch))
        }

    # A forced deploy of the deployment files writes nothing and brings the whole stack up.
    stack = await deploy_as(world, "root", files.id, force=True)

    assert stack.status is DeployStatus.SUCCESS, stack.message
    assert stack.post_deploy.status is PostDeployStatus.RAN and stack.post_deploy.exit_code == 0
    assert (demo.root / "compose.yaml").read_bytes() == compose_before
    assert len(workloads.container_ids("api")) == 1 and len(workloads.container_ids("db")) == 1
    (db_container,) = workloads.container_ids("db")
    workloads.wait_for_names("api", API_NAMES)

    # A changed file of one service recreates its container alone.
    async with world.session() as db:
        api_file = await db.scalar(
            select(Environment).where(
                Environment.service_id == services["api"],
                Environment.target_path == "environments/.env.api",
            )
        )
        assert api_file is not None
    current = (demo.targets / ".env.api").read_text()
    await world.save_version(api_file.id, current + SECOND_VERSION)
    before = time.time()

    deployed = await deploy_as(world, "root", services["api"])

    assert deployed.status is DeployStatus.SUCCESS
    assert deployed.post_deploy.status is PostDeployStatus.RAN
    assert workloads.created_since("api", before) == 1
    assert workloads.container_ids("db") == [db_container]
    workloads.wait_for_names("api", {*API_NAMES, "DEMO_SCRATCH_VERSION"})
