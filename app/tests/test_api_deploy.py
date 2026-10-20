"""The deploy endpoint: what it writes, what it answers and every rule it enforces."""

import json
import re
import stat
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.core.crypto import CryptoService
from app.models import (
    AccessGrant,
    ApiTokenRestriction,
    DeployRun,
    DeployTrigger,
    Environment,
    EnvironmentVersion,
    FileType,
    RestrictionLevel,
    User,
)
from app.seeds import EDITOR_ENVIRONMENT_NAME, EDITOR_SECRET_VALUES
from app.services.deploy_lock import hold_deploy_lock
from app.services.tokens import (
    OPERATION_DEPLOY,
    OPERATION_READ_LAST_DEPLOYMENT,
    find_token_by_secret,
    usage_example,
)
from app.tests.api_helpers import (
    ApiWorld,
    audit_entries,
    bearer,
    deploy,
    fingerprint,
    last,
    last_audit_id,
    make_token,
)
from app.tests.deploy_helpers import version_text
from app.tests.dist_helpers import link_dist, write_dist
from app.tests.file_type_helpers import set_file_type

DB = ".env.db"
SECRETS = ".env.db.secrets"
APP = EDITOR_ENVIRONMENT_NAME
FILES = [DB, APP, SECRETS]
RUN_KEYS = {
    "run_id",
    "service",
    "trigger",
    "dry_run",
    "status",
    "started_at",
    "finished_at",
    "environments",
    "post_deploy",
}
ENVIRONMENT_KEYS = {"id", "name", "file_type", "version", "status", "dist", "drift", "message"}


async def run_count(world: ApiWorld) -> int:
    """How many deploy runs are recorded."""
    async with world.session() as db:
        return (await db.execute(select(func.count()).select_from(DeployRun))).scalar_one()


async def newest_run(world: ApiWorld) -> DeployRun:
    """The newest recorded run."""
    async with world.session() as db:
        return (
            await db.execute(select(DeployRun).order_by(DeployRun.id.desc()).limit(1))
        ).scalar_one()


async def managed(
    world: ApiWorld, crypto: CryptoService, name: str, number: int | None = None
) -> str:
    """The managed text of a MySQL file, the newest version by default."""
    async with world.session() as db:
        return await version_text(db, crypto, world.environments[f"mysql/{name}"], number)


async def newest_number(world: ApiWorld, name: str) -> int:
    """The newest version number of a MySQL file."""
    async with world.session() as db:
        number = await db.scalar(
            select(func.max(EnvironmentVersion.number)).where(
                EnvironmentVersion.environment_id == world.environments[f"mysql/{name}"]
            )
        )
    assert number is not None
    return number


def names(response: httpx.Response) -> list[str]:
    """The environment names of a run answer."""
    return [item["name"] for item in response.json()["environments"]]


def problems(response: httpx.Response) -> dict[str, str]:
    """The field problems of a refused request by field name; the status must be 422."""
    assert response.status_code == 422
    return {item["field"]: item["message"] for item in response.json()["error"]["fields"]}


def untouched(world: ApiWorld) -> None:
    """Nothing was written to the production folder and no command ran."""
    assert not any(world.target("prod", name).exists() for name in FILES)
    assert world.docker.calls() == []


async def test_a_deploy_writes_the_files_and_answers_the_run(
    api_world: ApiWorld, crypto: CryptoService
) -> None:
    """Every file of the service is written (mode 0600) and the run is answered in full."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    response = await deploy(world, secret, mysql)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    answer = response.json()
    assert set(answer) == RUN_KEYS
    assert answer["service"] == {"id": mysql, "name": "mysql"}
    assert answer["trigger"] == "api"
    assert answer["dry_run"] is False and answer["status"] == "success"
    assert answer["started_at"] <= answer["finished_at"]
    assert answer["post_deploy"] == {"status": "ran", "exit_code": 0, "reason": None}
    assert names(response) == FILES
    for item in answer["environments"]:
        assert set(item) == ENVIRONMENT_KEYS and item["file_type"] == "env"
        assert (item["status"], item["drift"], item["dist"]) == ("written", "missing", "skipped")
        assert item["id"] == world.environments[f"mysql/{item['name']}"]
        assert item["version"] == await newest_number(world, item["name"])
        assert item["message"] == "Created."
    for name in FILES:
        target = world.target("prod", name)
        assert target.read_text(encoding="utf-8") == await managed(world, crypto, name)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    run = await newest_run(world)
    assert run.id == answer["run_id"] and run.trigger is DeployTrigger.API
    assert run.triggered_by_token_id is not None and run.triggered_by_user_id is None
    assert run.force is False and run.dry_run is False
    (call,) = world.docker.calls()
    assert call.arguments == ("compose", "up", "-d", "--no-deps", "mysql")
    assert call.working_directory == str(world.roots["prod"])


async def test_an_empty_body_deploys_every_file(api_world: ApiWorld) -> None:
    """No body is the same as an empty object."""
    world = api_world
    secret = await make_token(world, "admin")
    response = await deploy(world, secret, world.services["prod/mysql"], raw=b"")
    assert response.status_code == 200 and names(response) == FILES


async def test_a_dry_run_reports_without_writing(api_world: ApiWorld) -> None:
    """``dry_run`` runs the checks only: nothing is written and no command runs."""
    world = api_world
    secret = await make_token(world, "admin")
    response = await deploy(world, secret, world.services["prod/mysql"], {"dry_run": True})
    assert response.status_code == 200
    answer = response.json()
    assert answer["dry_run"] is True and answer["status"] == "success"
    assert {item["status"] for item in answer["environments"]} == {"would_write"}
    assert answer["post_deploy"] == {"status": "none", "exit_code": None, "reason": None}
    untouched(world)
    assert (await newest_run(world)).dry_run is True


async def test_every_answered_file_carries_its_type(api_world: ApiWorld) -> None:
    """The deploy answer and the last result name the type of each file, never its content."""
    world = api_world
    secret = await make_token(world, "admin")
    async with world.session() as db:
        await set_file_type(db, world.environments[f"mysql/{DB}"], FileType.CERT)
    mysql = world.services["prod/mysql"]
    response = await deploy(world, secret, mysql, {})
    assert response.status_code == 200
    types = {item["name"]: item["file_type"] for item in response.json()["environments"]}
    assert types == {DB: "cert", APP: "env", SECRETS: "env"}
    again = await last(world, secret, mysql)
    assert {item["name"]: item["file_type"] for item in again.json()["environments"]} == types


async def test_environments_are_named_by_file_name_or_by_id(api_world: ApiWorld) -> None:
    """A string names a file and a number is an id; only the chosen files are written."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    by_name = await deploy(world, secret, mysql, {"environments": [DB, DB]})
    assert by_name.status_code == 200 and names(by_name) == [DB]
    assert world.target("prod", DB).exists()
    assert not world.target("prod", SECRETS).exists() and not world.target("prod", APP).exists()

    by_id = await deploy(
        world, secret, mysql, {"environments": [world.environments[f"mysql/{SECRETS}"]]}
    )
    assert by_id.status_code == 200 and names(by_id) == [SECRETS]
    ids = world.environments
    mixed = await deploy(
        world,
        secret,
        mysql,
        {"environments": [APP, ids[f"mysql/{DB}"], str(ids[f"mysql/{SECRETS}"])]},
    )
    assert mixed.status_code == 200 and sorted(names(mixed)) == sorted(FILES)


async def test_versions_pick_the_text_to_write(api_world: ApiWorld, crypto: CryptoService) -> None:
    """``versions`` maps a file name, or an id as text, to the version number to write."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    assert await newest_number(world, APP) == 3
    older = await deploy(world, secret, mysql, {"versions": {APP: 1}})
    assert older.status_code == 200
    item = next(item for item in older.json()["environments"] if item["name"] == APP)
    assert item["version"] == 1
    target = world.target("prod", APP)
    assert target.read_text(encoding="utf-8") == await managed(world, crypto, APP, 1)
    by_id = await deploy(
        world,
        secret,
        mysql,
        {"environments": [APP], "versions": {str(world.environments[f"mysql/{APP}"]): 2}},
    )
    item = by_id.json()["environments"][0]
    assert (item["version"], item["status"]) == (2, "written")
    assert target.read_text(encoding="utf-8") == await managed(world, crypto, APP, 2)


async def test_an_unchanged_deploy_skips_the_command(api_world: ApiWorld) -> None:
    """The second deploy finds every file identical and the command does not run again."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    await deploy(world, secret, mysql)
    again = await deploy(world, secret, mysql)
    assert again.status_code == 200
    answer = again.json()
    assert {item["status"] for item in answer["environments"]} == {"unchanged"}
    assert answer["post_deploy"]["status"] == "skipped"
    assert answer["post_deploy"]["reason"] == (
        "No file changed and the command runs only when one did."
    )
    assert len(world.docker.calls()) == 1


async def test_a_halted_file_is_a_result_and_force_is_not_available(api_world: ApiWorld) -> None:
    """Drift halts the file: the answer is a 200 with status halted and the file is untouched."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    outside = world.target("prod", DB)
    outside.write_text("CHANGED_OUTSIDE=1\n", encoding="utf-8")
    response = await deploy(world, secret, mysql, {"environments": [DB]})
    assert response.status_code == 200
    answer = response.json()
    assert answer["status"] == "halted"
    (item,) = answer["environments"]
    assert (item["status"], item["drift"]) == ("halted", "drift")
    assert answer["post_deploy"]["status"] == "skipped"
    assert outside.read_text(encoding="utf-8") == "CHANGED_OUTSIDE=1\n"
    assert world.docker.calls() == []
    forced = await deploy(world, secret, mysql, {"environments": [DB], "force": True})
    assert forced.status_code == 422
    assert outside.read_text(encoding="utf-8") == "CHANGED_OUTSIDE=1\n"


async def test_a_blocked_file_reports_the_dist_outcome(api_world: ApiWorld) -> None:
    """Missing dist keys block the file under the default policy and say so in the answer."""
    world = api_world
    secret = await make_token(world, "admin")
    write_dist(world.roots["prod"], "environments/.env.db-dist", "MISSING_KEY=\n")
    async with world.session() as db:
        environment = await db.get(Environment, world.environments[f"mysql/{DB}"])
        assert environment is not None
        await link_dist(db, environment, "environments/.env.db-dist")
    response = await deploy(world, secret, world.services["prod/mysql"], {"environments": [DB]})
    answer = response.json()
    assert response.status_code == 200 and answer["status"] == "blocked"
    (item,) = answer["environments"]
    assert (item["status"], item["dist"]) == ("blocked", "blocked")
    assert not world.target("prod", DB).exists()


async def test_a_failing_command_is_post_deploy_failed_with_its_exit_code(
    api_world: ApiWorld,
) -> None:
    """The files are written, the run reads post_deploy_failed and carries the exit code."""
    world = api_world
    secret = await make_token(world, "admin")
    world.docker.behavior('echo "workers=4 failed to start"; exit 3')
    response = await deploy(world, secret, world.services["prod/mysql"])
    answer = response.json()
    assert response.status_code == 200 and answer["status"] == "post_deploy_failed"
    assert answer["post_deploy"]["status"] == "failed"
    assert answer["post_deploy"]["exit_code"] == 3
    assert "workers=4" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"force": True},
        {"force": False},
        {"force": None},
        {"force": "yes", "dry_run": True},
        {"environments": [DB], "force": 1},
    ],
)
async def test_force_is_refused_whatever_its_value(
    api_world: ApiWorld, body: dict[str, Any]
) -> None:
    """A body with the key ``force`` is refused with a clear message and nothing happens."""
    world = api_world
    secret = await make_token(world, "admin")
    runs = await run_count(world)
    response = await deploy(world, secret, world.services["prod/mysql"], body)
    found = problems(response)
    assert "force is not accepted by the API" in found["force"]
    assert "person in the web interface" in found["force"]
    assert response.json()["error"]["code"] == "validation_error"
    assert await run_count(world) == runs
    untouched(world)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"environment": [DB]}, "environment"),
        ({"Force": True}, "Force"),
        ({"dry_run": "yes"}, "dry_run"),
        ({"dry_run": 1}, "dry_run"),
        ({"environments": DB}, "environments"),
        ({"environments": [True]}, "environments"),
        ({"environments": [1.5]}, "environments"),
        ({"environments": [""]}, "environments"),
        ({"environments": [[DB]]}, "environments"),
        ({"environments": [10**30]}, "environments"),
        ({"environments": [0]}, "environments"),
        ({"environments": ["x" * 101]}, "environments"),
        ({"versions": [1]}, "versions"),
        ({"versions": {DB: "1"}}, "versions"),
        ({"versions": {DB: True}}, "versions"),
        ({"versions": {DB: 0}}, "versions"),
        ({"versions": {DB: 10**30}}, "versions"),
        ({"versions": {"": 1}}, "versions"),
    ],
)
async def test_unknown_keys_and_wrong_types_are_refused(
    api_world: ApiWorld, body: dict[str, Any], field: str
) -> None:
    """Strict validation: a field that is not accepted or has the wrong type answers 422."""
    world = api_world
    secret = await make_token(world, "admin")
    response = await deploy(world, secret, world.services["prod/mysql"], body)
    assert field in problems(response)
    assert "input" not in response.text
    untouched(world)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b"null",
        b'"text"',
        b"1",
        b"\xff\xfe\x00",
        b"[" * 30000 + b"]" * 30000,
        b" " + b"x" * 70_000,
        b'{"environments": [' + b"9" * 400 + b"]}",
    ],
)
async def test_a_body_that_cannot_be_used_is_refused(api_world: ApiWorld, raw: bytes) -> None:
    """Bad JSON, a body that is not an object, a huge number and an oversize body answer 422."""
    world = api_world
    secret = await make_token(world, "admin")
    response = await deploy(world, secret, world.services["prod/mysql"], raw=raw)
    assert problems(response)
    untouched(world)


async def test_unknown_and_hidden_environments_read_alike(api_world: ApiWorld) -> None:
    """A file the token may not deploy reads exactly like one that does not exist."""
    world = api_world
    secret = await make_token(world, "env_scoped_admin")
    mysql = world.services["prod/mysql"]
    hidden = await deploy(world, secret, mysql, {"environments": [SECRETS]})
    missing = await deploy(world, secret, mysql, {"environments": ["nothing"]})
    by_id = await deploy(
        world, secret, mysql, {"environments": [world.environments[f"mysql/{SECRETS}"]]}
    )
    not_found = "Environment {} was not found in this service."
    assert problems(hidden)["environments"] == not_found.format(repr(SECRETS))
    assert problems(missing)["environments"] == not_found.format("'nothing'")
    assert problems(by_id)["environments"] == not_found.format(
        world.environments[f"mysql/{SECRETS}"]
    )
    assert hidden.json()["error"]["code"] == missing.json()["error"]["code"]
    untouched(world)


async def test_choices_that_cannot_be_deployed_are_refused(api_world: ApiWorld) -> None:
    """An empty list, a version for a file not deployed and a missing version answer 422."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    assert "environments" in problems(await deploy(world, secret, mysql, {"environments": []}))
    outside = await deploy(world, secret, mysql, {"environments": [DB], "versions": {APP: 1}})
    assert "not one of the environments deployed" in problems(outside)["versions"]
    unknown = await deploy(world, secret, mysql, {"versions": {"nothing": 1}})
    assert "versions" in problems(unknown)
    twice = await deploy(
        world,
        secret,
        mysql,
        {"versions": {APP: 1, str(world.environments[f"mysql/{APP}"]): 2}},
    )
    assert "two different versions" in problems(twice)["versions"]
    missing_version = await deploy(world, secret, mysql, {"versions": {DB: 99}})
    assert missing_version.status_code == 422
    assert missing_version.json()["error"]["message"] == "Version not found."
    untouched(world)


async def test_a_service_that_is_missing_or_out_of_scope_reads_the_same(
    api_world: ApiWorld,
) -> None:
    """Out of scope, missing, not a number and out of range all answer the same 404."""
    world = api_world
    secret = await make_token(world, "project_admin")
    others: list[int | str] = [
        world.services["main/api"],
        987654,
        "abc",
        "-1",
        "1.5",
        "0",
        10**30,
        "٣",
    ]
    deploys = {fingerprint(await deploy(world, secret, service)) for service in others}
    reads = {fingerprint(await last(world, secret, service)) for service in others}
    assert len(deploys) == 1 and len(reads) == 1
    deployed = await deploy(world, secret, 987654)
    read = await last(world, secret, 987654)
    assert (deployed.status_code, read.status_code) == (404, 404)
    assert deployed.json()["error"]["message"] == "Service not found."
    assert read.json()["error"]["message"] == "No deployment was found for this service."
    untouched(world)


async def test_a_token_without_the_operation_gets_403_before_anything_else(
    api_world: ApiWorld,
) -> None:
    """The operation is checked first, so the answer says nothing about the service."""
    world = api_world
    read_only = await make_token(world, "admin", operations=(OPERATION_READ_LAST_DEPLOYMENT,))
    deploy_only = await make_token(world, "admin", operations=(OPERATION_DEPLOY,), name="push")
    answers = set()
    for service in (world.services["prod/mysql"], world.services["main/api"], 987654, "abc"):
        refused = await deploy(world, read_only, service, {"force": True})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "forbidden"
        assert refused.json()["error"]["message"] == "This token is not allowed to deploy."
        answers.add(fingerprint(refused))
        read = await last(world, deploy_only, service)
        assert read.status_code == 403
        assert read.json()["error"]["message"] == (
            "This token is not allowed to read the last deployment."
        )
    assert len(answers) == 1
    untouched(world)


async def test_a_busy_deployment_folder_answers_409(api_world: ApiWorld) -> None:
    """While another deploy holds the folder the call is refused and records no run."""
    world = api_world
    secret = await make_token(world, "admin")
    runs = await run_count(world)
    before = await last_audit_id(world)
    with hold_deploy_lock(world.app.state.lock_dir, str(world.roots["prod"])):
        response = await deploy(world, secret, world.services["prod/mysql"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "deploy_locked"
    assert await run_count(world) == runs
    untouched(world)
    (entry,) = await audit_entries(world, before)
    assert entry.action == "service.deploy" and entry.details["refused"] == "locked"
    assert entry.actor_token_id is not None and entry.actor_type.value == "api"
    again = await deploy(world, secret, world.services["prod/mysql"])
    assert again.status_code == 200


async def test_a_dry_run_does_not_need_the_folder_lock(api_world: ApiWorld) -> None:
    """Dry runs take no lock, so a busy folder does not refuse them."""
    world = api_world
    secret = await make_token(world, "admin")
    with hold_deploy_lock(world.app.state.lock_dir, str(world.roots["prod"])):
        response = await deploy(world, secret, world.services["prod/mysql"], {"dry_run": True})
    assert response.status_code == 200


async def test_the_owner_grants_limit_what_a_token_deploys(api_world: ApiWorld) -> None:
    """An owner with one file gets a token that deploys that file and cannot name another."""
    world = api_world
    secret = await make_token(world, "env_scoped_admin")
    mysql = world.services["prod/mysql"]
    response = await deploy(world, secret, mysql)
    assert response.status_code == 200 and names(response) == [DB]
    assert world.target("prod", DB).exists()
    assert not world.target("prod", SECRETS).exists() and not world.target("prod", APP).exists()
    assert (await deploy(world, secret, world.services["staging/web"])).status_code == 404


@pytest.mark.parametrize(
    ("restriction", "reachable", "hidden"),
    [
        ("project:billing", ["main/api", "main/worker"], ["prod/mysql", "prod/web", "staging/web"]),
        ("deployment:staging", ["staging/web"], ["prod/mysql", "prod/web", "main/api"]),
        ("service:prod/web", ["prod/web"], ["prod/mysql", "staging/web", "main/api"]),
    ],
)
async def test_a_restriction_narrows_the_owner(
    api_world: ApiWorld, restriction: str, reachable: list[str], hidden: list[str]
) -> None:
    """The token reaches only the item it is restricted to, inside what its owner reaches."""
    world = api_world
    level, _, item = restriction.partition(":")
    ids = {"project": world.projects, "deployment": world.deployments, "service": world.services}
    secret = await make_token(world, "admin", restriction=f"{level}:{ids[level][item]}")
    for service in reachable:
        response = await deploy(world, secret, world.services[service], {"dry_run": True})
        assert response.status_code == 200, service
    for service in hidden:
        response = await deploy(world, secret, world.services[service], {"dry_run": True})
        assert response.status_code == 404, service


async def test_a_restriction_outside_the_owner_grants_reaches_nothing(api_world: ApiWorld) -> None:
    """The scope is the intersection: a restriction cannot widen what the owner has."""
    world = api_world
    secret = await make_token(world, "project_admin")
    async with world.session() as db:
        token = await find_token_by_secret(db, secret)
        assert token is not None
        db.add(
            ApiTokenRestriction(
                token_id=token.id,
                level=RestrictionLevel.SERVICE,
                service_id=world.services["main/api"],
            )
        )
        await db.commit()
    for service in ("main/api", "prod/mysql"):
        response = await deploy(world, secret, world.services[service], {"dry_run": True})
        assert response.status_code == 404
    untouched(world)


async def test_a_lost_grant_applies_to_the_next_call(api_world: ApiWorld) -> None:
    """Grants are loaded for every call: removing one takes effect at once."""
    world = api_world
    secret = await make_token(world, "project_admin")
    mysql = world.services["prod/mysql"]
    assert (await deploy(world, secret, mysql, {"dry_run": True})).status_code == 200
    async with world.session() as db:
        owner = await db.scalar(select(User).where(User.name == "project_admin"))
        assert owner is not None
        await db.execute(delete(AccessGrant).where(AccessGrant.user_id == owner.id))
        await db.commit()
    assert (await deploy(world, secret, mysql, {"dry_run": True})).status_code == 404
    assert (await last(world, secret, mysql)).status_code == 404


async def test_the_answer_never_holds_values_output_or_command_text(
    api_world: ApiWorld, crypto: CryptoService
) -> None:
    """A command that prints the managed files leaves no value, output or command in any answer."""
    world = api_world
    secret = await make_token(world, "admin")
    values: list[str] = []
    for name in FILES:
        for line in (await managed(world, crypto, name)).splitlines():
            if "=" in line and not line.startswith("#"):
                values.append(line.partition("=")[2].strip().strip("'\""))
    assert any(len(value) >= 4 for value in values)
    world.docker.behavior(
        "echo OUTPUT-MARKER-9137; cat " + " ".join(f"environments/{name}" for name in FILES)
    )
    mysql = world.services["prod/mysql"]
    deployed = await deploy(world, secret, mysql)
    read = await last(world, secret, mysql)
    run = await newest_run(world)
    assert run.post_deploy_output is not None and "OUTPUT-MARKER-9137" in run.post_deploy_output
    for response in (deployed, read):
        assert response.status_code == 200
        text = response.text
        assert "OUTPUT-MARKER" not in text and "output" not in text
        assert "compose" not in text and "docker" not in text
        assert str(world.roots["prod"]) not in text and ".bak" not in text
        assert not [value for value in values if len(value) >= 4 and value in text]
        assert not [value for value in EDITOR_SECRET_VALUES if value in text]
    assert deployed.json() == read.json()


async def test_the_usage_example_of_the_token_dialog_works_as_shown(api_world: ApiWorld) -> None:
    """The example the created-token dialog shows is a request the API accepts, path and body."""
    world = api_world
    mysql = world.services["prod/mysql"]
    secret = await make_token(world, "admin", restriction=f"service:{mysql}")
    example = usage_example(
        "http://test", (OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT), mysql
    )
    post_block, get_block = example.split("\n\n")
    post_path = re.search(r"-X POST http://test(\S+)", post_block)
    body = re.search(r"-d '(.*)'", post_block)
    get_path = re.search(r"curl http://test(\S+)", get_block)
    assert post_path and body and get_path
    assert "Authorization: Bearer $ENVM_TOKEN" in post_block
    assert "Content-Type: application/json" in post_block
    assert "Authorization: Bearer $ENVM_TOKEN" in get_block
    dry = await world.http.post(post_path.group(1), content=body.group(1), headers=bearer(secret))
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert json.loads(body.group(1)) == {"dry_run": True}
    assert (await deploy(world, secret, mysql)).status_code == 200
    read = await world.http.get(get_path.group(1), headers=bearer(secret))
    assert read.status_code == 200 and read.json()["dry_run"] is False
