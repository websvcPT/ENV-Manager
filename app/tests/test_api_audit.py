"""Every API call leaves one audit entry with the token and its owner, and never the secret."""

from collections.abc import Awaitable
from typing import Any

import httpx
from sqlalchemy import select

from app.models import ApiToken, AuditActorType, AuditEntry, AuditResult, User
from app.services.deploy_lock import hold_deploy_lock
from app.services.tokens import (
    OPERATION_DEPLOY,
    OPERATION_READ_LAST_DEPLOYMENT,
    hash_secret,
)
from app.tests.api_helpers import (
    CLIENT_ADDRESS,
    ApiWorld,
    audit_entries,
    bearer,
    deploy,
    last,
    last_audit_id,
    make_token,
)

ADDRESS = CLIENT_ADDRESS[0]


async def one_entry(
    world: ApiWorld, call: Awaitable[httpx.Response]
) -> tuple[httpx.Response, AuditEntry]:
    """Make a call and return its answer with the one audit entry that records the call.

    A deploy also leaves the dist check entries of its files; they are the token's too.
    """
    before = await last_audit_id(world)
    response = await call
    entries = await audit_entries(world, before)
    checks = [entry for entry in entries if entry.action == "environment.dist_check"]
    calls = [entry for entry in entries if entry.action != "environment.dist_check"]
    assert len(calls) == 1, [(entry.action, entry.details) for entry in entries]
    assert {entry.actor_token_id for entry in checks} <= {calls[0].actor_token_id}
    assert all(entry.actor_type is AuditActorType.API for entry in checks)
    return response, calls[0]


def assert_token_actor(entry: AuditEntry, owner_id: int, token_id: int, name: str = "ci") -> None:
    """The entry names the token, its owner and the address the call came from."""
    assert entry.actor_type is AuditActorType.API
    assert entry.actor_user_id == owner_id and entry.actor_token_id == token_id
    assert entry.actor_name == f"token {name}" and entry.ip_address == ADDRESS


async def identities(world: ApiWorld, owner: str) -> tuple[int, int]:
    """The id of a seeded user and of that user's newest token."""
    async with world.session() as db:
        user = await db.scalar(select(User).where(User.name == owner))
        token = await db.scalar(select(ApiToken).order_by(ApiToken.id.desc()).limit(1))
    assert user is not None and token is not None
    return user.id, token.id


async def test_each_kind_of_call_leaves_exactly_one_entry(api_world: ApiWorld) -> None:
    """A deploy, a dry run, a read and every refusal: one entry each, with token and owner."""
    world = api_world
    secret = await make_token(world, "admin")
    owner_id, token_id = await identities(world, "admin")
    mysql = world.services["prod/mysql"]
    scope_ids = {
        "project_id": world.projects["rentals"],
        "deployment_id": world.deployments["prod"],
        "service_id": mysql,
    }

    dry, entry = await one_entry(world, deploy(world, secret, mysql, {"dry_run": True}))
    assert dry.status_code == 200
    assert entry.action == "service.deploy_dry_run" and entry.result is AuditResult.SUCCESS
    assert_token_actor(entry, owner_id, token_id)
    assert entry.details["run_id"] == dry.json()["run_id"]

    real, entry = await one_entry(world, deploy(world, secret, mysql))
    assert real.status_code == 200
    assert entry.action == "service.deploy" and entry.result is AuditResult.SUCCESS
    assert_token_actor(entry, owner_id, token_id)
    assert entry.target_name == "mysql" and entry.version_ids
    assert (entry.project_id, entry.deployment_id, entry.service_id) == tuple(scope_ids.values())

    read, entry = await one_entry(world, last(world, secret, mysql))
    assert read.status_code == 200
    assert entry.action == "service.last_deploy_read" and entry.result is AuditResult.SUCCESS
    assert_token_actor(entry, owner_id, token_id)
    assert entry.details == {"run_id": real.json()["run_id"]}
    assert entry.target_name == "mysql" and entry.service_id == mysql

    forced, entry = await one_entry(world, deploy(world, secret, mysql, {"force": True}))
    assert forced.status_code == 422
    assert entry.action == "service.deploy" and entry.result is AuditResult.FAILURE
    assert_token_actor(entry, owner_id, token_id)
    assert entry.details == {"refused": "invalid", "fields": ["force"]}
    assert entry.target_name == "mysql" and entry.service_id == mysql

    with hold_deploy_lock(world.app.state.lock_dir, str(world.roots["prod"])):
        busy, entry = await one_entry(world, deploy(world, secret, mysql))
    assert busy.status_code == 409
    assert entry.action == "service.deploy" and entry.details["refused"] == "locked"
    assert_token_actor(entry, owner_id, token_id)


async def test_a_refusal_names_only_the_id_that_was_asked_for(api_world: ApiWorld) -> None:
    """404 entries for a missing service, one out of scope and a bad id carry no name or scope."""
    world = api_world
    secret = await make_token(world, "project_admin")
    owner_id, token_id = await identities(world, "project_admin")
    api = world.services["main/api"]

    entries = []
    for service in (api, 987654, "abc"):
        for call in (deploy(world, secret, service), last(world, secret, service)):
            response, entry = await one_entry(world, call)
            assert response.status_code == 404
            assert_token_actor(entry, owner_id, token_id)
            assert entry.result is AuditResult.FAILURE
            assert entry.details == {"refused": "not_found"}
            assert entry.target_name is None
            assert (entry.project_id, entry.deployment_id, entry.service_id) == (None,) * 3
            entries.append(entry)
    assert [entry.target_id for entry in entries] == [api, api, 987654, 987654, None, None]
    assert [entry.action for entry in entries[:2]] == ["service.deploy", "service.last_deploy_read"]


async def test_a_token_without_the_operation_leaves_a_forbidden_entry(api_world: ApiWorld) -> None:
    """The 403 is audited with the token and says nothing of the service it named."""
    world = api_world
    secret = await make_token(world, "admin", operations=(OPERATION_READ_LAST_DEPLOYMENT,))
    owner_id, token_id = await identities(world, "admin")
    response, entry = await one_entry(world, deploy(world, secret, world.services["prod/mysql"]))
    assert response.status_code == 403
    assert_token_actor(entry, owner_id, token_id)
    assert entry.details == {"refused": "forbidden"}
    assert entry.target_name is None and entry.service_id is None
    deploy_only = await make_token(world, "admin", operations=(OPERATION_DEPLOY,), name="push")
    _, push_id = await identities(world, "admin")
    response, entry = await one_entry(world, last(world, deploy_only, 987654))
    assert response.status_code == 403
    assert entry.action == "service.last_deploy_read" and entry.details == {"refused": "forbidden"}
    assert_token_actor(entry, owner_id, push_id, "push")


async def test_a_locked_address_is_audited_as_blocked(api_world: ApiWorld) -> None:
    """The failure that locks the address adds a lockout entry; later calls add a blocked one."""
    world = api_world
    secret = await make_token(world, "admin")
    for _ in range(9):
        await world.http.get("/api/v1/services/1/deployments/last", headers=bearer("nope"))
    before = await last_audit_id(world)
    locking = await world.http.get("/api/v1/services/1/deployments/last", headers=bearer("nope"))
    assert locking.status_code == 429
    failure, lockout = await audit_entries(world, before)
    assert (failure.action, failure.result) == ("token.auth_failure", AuditResult.FAILURE)
    assert (lockout.action, lockout.result) == ("token.auth_lockout", AuditResult.BLOCKED)
    assert lockout.details["minutes"] == 15
    response, blocked = await one_entry(world, deploy(world, secret, world.services["prod/mysql"]))
    assert response.status_code == 429
    assert (blocked.action, blocked.result) == ("token.auth_failure", AuditResult.BLOCKED)
    assert blocked.details == {"reason": "locked"} and blocked.actor_token_id is None


async def test_no_entry_holds_the_secret_or_its_hash(api_world: ApiWorld) -> None:
    """Whatever the call, the secret and its stored forms appear in no entry."""
    world = api_world
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    await deploy(world, secret, mysql)
    await last(world, secret, mysql)
    await deploy(world, secret, mysql, {"force": True})
    await deploy(world, secret, 987654)
    await world.http.get("/api/v1/services/1/deployments/last", headers=bearer(secret + "x"))
    entries = await audit_entries(world)
    dump: list[Any] = [
        (
            entry.actor_name,
            entry.target_name,
            entry.details,
            entry.ip_address,
            entry.version_ids,
        )
        for entry in entries
    ]
    text = repr(dump)
    assert secret not in text and hash_secret(secret) not in text and secret[:13] not in text
