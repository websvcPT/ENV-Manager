"""The last deployment endpoint: which run it answers, and what the token's scope lets it show."""

import httpx
from sqlalchemy import delete, select, update

from app.models import DeployResult, DeployRun, Environment, PostDeployStatus
from app.seeds import EDITOR_ENVIRONMENT_NAME
from app.services.tokens import OPERATION_DEPLOY
from app.tests.api_helpers import ApiWorld, deploy, fingerprint, last, make_token

DB = ".env.db"
SECRETS = ".env.db.secrets"
APP = EDITOR_ENVIRONMENT_NAME


async def clear_runs(world: ApiWorld) -> None:
    """Forget the runs of the seed, so a test reads only the runs it makes."""
    async with world.session() as db:
        await db.execute(delete(DeployRun))
        await db.commit()


def names(response: httpx.Response) -> list[str]:
    """The environment names of a run answer."""
    return [item["name"] for item in response.json()["environments"]]


async def test_a_service_never_deployed_reads_like_one_the_token_cannot_reach(
    api_world: ApiWorld,
) -> None:
    """No run, no such service and a service out of scope give one and the same 404."""
    world = api_world
    await clear_runs(world)
    secret = await make_token(world, "project_admin")
    never = await last(world, secret, world.services["prod/mysql"])
    missing = await last(world, secret, 987654)
    outside = await last(world, secret, world.services["main/api"])
    assert never.status_code == 404
    assert fingerprint(never) == fingerprint(missing) == fingerprint(outside)
    assert never.json()["error"]["code"] == "not_found"


async def test_the_answer_is_the_run_the_deploy_returned(api_world: ApiWorld) -> None:
    """The deploy and the last result have one shape, and the same content for the same run."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    deployed = await deploy(world, secret, mysql)
    read = await last(world, secret, mysql)
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
    assert read.json() == deployed.json()
    assert read.json()["status"] == "success"


async def test_dry_runs_are_not_the_last_deployment(api_world: ApiWorld) -> None:
    """A dry run made after a real deploy does not replace it, and alone it is no deployment."""
    world = api_world
    await clear_runs(world)
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    await deploy(world, secret, mysql, {"dry_run": True})
    assert (await last(world, secret, mysql)).status_code == 404
    real = await deploy(world, secret, mysql)
    await deploy(world, secret, mysql, {"dry_run": True})
    read = await last(world, secret, mysql)
    assert read.json()["run_id"] == real.json()["run_id"] and read.json()["dry_run"] is False


async def test_only_the_files_in_scope_are_listed(api_world: ApiWorld) -> None:
    """A token whose owner reaches one file sees that file's result and nothing of the others."""
    world = api_world
    await clear_runs(world)
    admin = await make_token(world, "admin")
    scoped = await make_token(world, "env_scoped_admin")
    mysql = world.services["prod/mysql"]
    everything = await deploy(world, admin, mysql)
    assert sorted(names(everything)) == sorted([DB, APP, SECRETS])
    seen = await last(world, scoped, mysql)
    assert seen.status_code == 200
    assert names(seen) == [DB]
    assert seen.json()["run_id"] == everything.json()["run_id"]
    assert SECRETS not in seen.text and APP not in seen.text


async def test_a_run_that_touched_only_hidden_files_does_not_exist_for_the_token(
    api_world: ApiWorld,
) -> None:
    """The newest run with a result in scope answers; a newer run of hidden files is skipped."""
    world = api_world
    await clear_runs(world)
    admin = await make_token(world, "admin")
    scoped = await make_token(world, "env_scoped_admin")
    mysql = world.services["prod/mysql"]
    first = await deploy(world, admin, mysql)
    hidden_only = await deploy(
        world, admin, mysql, {"environments": [SECRETS], "versions": {SECRETS: 1}}
    )
    assert hidden_only.json()["run_id"] > first.json()["run_id"]
    read = await last(world, scoped, mysql)
    assert read.json()["run_id"] == first.json()["run_id"]
    assert (await last(world, admin, mysql)).json()["run_id"] == hidden_only.json()["run_id"]
    async with world.session() as db:
        await db.execute(delete(DeployRun).where(DeployRun.id == first.json()["run_id"]))
        await db.commit()
    assert (await last(world, scoped, mysql)).status_code == 404


async def test_a_restricted_token_reads_only_inside_its_restriction(api_world: ApiWorld) -> None:
    """The restriction limits the read like it limits the deploy, with the same 404."""
    world = api_world
    web = world.services["prod/web"]
    secret = await make_token(world, "admin", restriction=f"service:{web}")
    read = await last(world, secret, web)
    assert read.status_code == 200 and read.json()["service"]["name"] == "web"
    assert (await last(world, secret, world.services["prod/mysql"])).status_code == 404
    assert (await last(world, secret, world.services["main/api"])).status_code == 404


async def test_the_seeded_run_is_answered_without_its_output(api_world: ApiWorld) -> None:
    """A run recorded long before keeps its stored output out of the answer."""
    world = api_world
    secret = await make_token(world, "admin", restriction=f"service:{world.services['prod/web']}")
    async with world.session() as db:
        stored = await db.scalar(
            select(DeployRun.post_deploy_output).where(
                DeployRun.service_id == world.services["prod/web"]
            )
        )
    assert stored
    read = await last(world, secret, world.services["prod/web"])
    assert stored not in read.text and "output" not in read.text


async def test_a_running_post_deploy_command_reads_running(api_world: ApiWorld) -> None:
    """While the command has not ended the run has no finish time and says running."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    answer = (await deploy(world, secret, mysql)).json()
    async with world.session() as db:
        await db.execute(
            update(DeployRun)
            .where(DeployRun.id == answer["run_id"])
            .values(post_deploy_status=PostDeployStatus.RUNNING, finished_at=None)
        )
        await db.commit()
    read = (await last(world, secret, mysql)).json()
    assert read["post_deploy"]["status"] == "running" and read["finished_at"] is None


async def test_a_file_deleted_since_the_run_is_not_listed(api_world: ApiWorld) -> None:
    """A result whose file no longer exists belongs to nobody's scope and is left out."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    await deploy(world, secret, mysql)
    async with world.session() as db:
        await db.execute(
            delete(Environment).where(Environment.id == world.environments[f"mysql/{SECRETS}"])
        )
        await db.commit()
        orphaned = await db.scalar(
            select(DeployResult.id).where(DeployResult.environment_name == SECRETS)
        )
        assert orphaned is not None
    read = await last(world, secret, mysql)
    assert sorted(names(read)) == sorted([DB, APP])


async def test_a_token_that_cannot_read_gets_403(api_world: ApiWorld) -> None:
    """A deploy-only token is refused the read, whatever the service."""
    world = api_world
    secret = await make_token(world, "admin", operations=(OPERATION_DEPLOY,))
    for service in (world.services["prod/mysql"], 987654):
        assert (await last(world, secret, service)).status_code == 403
