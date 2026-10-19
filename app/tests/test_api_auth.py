"""Bearer authentication of the API: one answer for every bad token, rate limiting, last use."""

import asyncio
import hmac
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.sessions import SESSION_COOKIE_NAME
from app.main import create_app
from app.models import ApiToken, AuditResult, LoginCounter, LoginCounterKind, User, UserGroup
from app.models.types import utc_now
from app.services.api_auth import authenticate_token, parse_bearer
from app.services.api_errors import ApiRefusal, RefusalKind
from app.services.app_settings import load_stored_settings, save_section
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy
from app.services.tokens import find_token_by_secret, hash_secret, revoke_token
from app.tests.api_helpers import (
    CLIENT_ADDRESS,
    ApiWorld,
    audit_entries,
    bearer,
    deploy,
    fingerprint,
    last,
    make_token,
)
from app.tests.conftest import sign_in

UNKNOWN_SECRET = "envm_" + "0" * 48
ADDRESS = CLIENT_ADDRESS[0]


async def counter(world: ApiWorld, subject: str = ADDRESS) -> LoginCounter | None:
    """The API failure counter of an address."""
    async with world.session() as db:
        return await db.scalar(
            select(LoginCounter).where(
                LoginCounter.kind == LoginCounterKind.API_IP, LoginCounter.subject == subject
            )
        )


async def allow_failures(world: ApiWorld, limit: int = 1000) -> None:
    """Raise the stored failure limit so a test can send many bad tokens from one address."""
    async with world.session() as db:
        security = (await load_stored_settings(db)).security
        await save_section(
            db, "security", replace(security, api_ip_max_failures=limit), actor=AuditActor.cli()
        )
        await db.commit()


async def prod_mysql_dry_run(world: ApiWorld, secret: str) -> httpx.Response:
    """A dry run of the production MySQL service, which a granted admin token may make."""
    return await deploy(world, secret, world.services["prod/mysql"], {"dry_run": True})


def test_the_header_is_read_strictly() -> None:
    """Only the Bearer scheme with one printable token without spaces gives a secret."""
    assert parse_bearer("Bearer abc") == "abc"
    assert parse_bearer("bearer abc") == "abc"
    assert parse_bearer("BEARER abc") == "abc"
    for header in (
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer  abc",
        "Bearer abc def",
        "Basic abc",
        "Token abc",
        "abc",
        "Bearer abc\t",
        "Bearer café",
        "Bearer " + "a" * 201,
    ):
        assert parse_bearer(header) is None, header


async def test_every_bad_token_gets_the_same_answer(api_world: ApiWorld) -> None:
    """Missing, malformed, unknown, revoked, expired, stopped and cookie-only calls read alike."""
    world = api_world
    await allow_failures(world)
    revoked = await make_token(world, "admin", name="revoked")
    expired = await make_token(world, "admin", name="expired")
    owner_disabled = await make_token(world, "project_admin", name="disabled")
    owner_viewer = await make_token(world, "deployment_admin", name="viewer")
    for secret in (revoked, expired, owner_disabled, owner_viewer):
        assert (await prod_mysql_dry_run(world, secret)).status_code == 200

    async with world.session() as db:
        token = await find_token_by_secret(db, revoked)
        assert token is not None
        await revoke_token(db, token, AuditActor.cli())
        await db.execute(
            update(ApiToken)
            .where(ApiToken.name == "expired")
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )
        await db.execute(update(User).where(User.name == "project_admin").values(is_active=False))
        await db.execute(
            update(User).where(User.name == "deployment_admin").values(group=UserGroup.VIEWER)
        )
        await db.commit()

    url = f"/api/v1/services/{world.services['prod/mysql']}/deployments/last"
    headers: list[dict[str, str]] = [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": f"Bearer {UNKNOWN_SECRET} extra"},
        {"Authorization": "Bearer " + "a" * 300},
        {"Authorization": f"Bearer {UNKNOWN_SECRET}"},
        {"Authorization": "Bearer seed-token-billing-cI"},
        bearer(revoked),
        bearer(expired),
        bearer(owner_disabled),
        bearer(owner_viewer),
    ]
    answers = {fingerprint(await world.http.get(url, headers=sent)) for sent in headers}
    assert len(answers) == 1
    status, body, headers_seen = next(iter(answers))
    sent_headers = dict(headers_seen)
    assert status == 401
    assert sent_headers["www-authenticate"] == "Bearer"
    assert sent_headers["cache-control"] == "no-store"
    assert sent_headers["content-type"] == "application/json"
    assert "set-cookie" not in sent_headers
    assert body == (
        b'{"error":{"code":"unauthorized","message":"Invalid or missing API token.",'
        b'"fields":[],"request_id":null}}'
    )


async def test_a_session_cookie_is_not_a_token(api_world: ApiWorld) -> None:
    """The API ignores cookies: a signed-in browser session gets the same 401."""
    world = api_world
    anonymous = fingerprint(await world.http.get("/api/v1/services/1/deployments/last"))
    await sign_in(world.http, "admin")
    assert SESSION_COOKIE_NAME in world.http.cookies
    with_cookie = await world.http.get("/api/v1/services/1/deployments/last")
    assert fingerprint(with_cookie) == anonymous
    bad_token = await world.http.get(
        "/api/v1/services/1/deployments/last", headers=bearer(UNKNOWN_SECRET)
    )
    assert fingerprint(bad_token) == anonymous
    secret = await make_token(world, "admin")
    assert (await prod_mysql_dry_run(world, secret)).status_code == 200


async def test_api_answers_set_no_cookie(api_world: ApiWorld) -> None:
    """Neither a refusal nor a success sets the CSRF cookie or any other cookie."""
    world = api_world
    secret = await make_token(world, "admin")
    refused = await world.http.get("/api/v1/services/1/deployments/last")
    answered = await prod_mysql_dry_run(world, secret)
    unknown = await world.http.get("/api/v1/nothing")
    for response in (refused, answered, unknown):
        assert "set-cookie" not in response.headers
    assert dict(world.http.cookies) == {}


async def test_the_stored_hash_is_compared_in_constant_time(
    api_world: ApiWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A found token and a missing one both go through ``hmac.compare_digest``."""
    world = api_world
    secret = await make_token(world, "admin")
    real = hmac.compare_digest
    compared: list[tuple[bytes, bytes]] = []

    def spy(first: bytes, second: bytes) -> bool:
        compared.append((first, second))
        return real(first, second)

    monkeypatch.setattr("app.services.api_auth.hmac.compare_digest", spy)
    await prod_mysql_dry_run(world, secret)
    stored = hash_secret(secret).encode()
    assert compared[0] == (stored, stored)
    compared.clear()
    await prod_mysql_dry_run(world, UNKNOWN_SECRET)
    assert len(compared) == 1 and compared[0][0] != compared[0][1]


async def test_failed_attempts_lock_the_address_and_answer_429_with_retry_after(
    api_world: ApiWorld,
) -> None:
    """Ten bad tokens lock the address for 15 minutes, a valid token included."""
    world = api_world
    secret = await make_token(world, "admin")
    url = f"/api/v1/services/{world.services['prod/mysql']}/deployments/last"
    for _ in range(9):
        assert (await world.http.get(url, headers=bearer(UNKNOWN_SECRET))).status_code == 401
    tenth = await world.http.get(url, headers=bearer(UNKNOWN_SECRET))
    assert tenth.status_code == 429
    assert tenth.headers["retry-after"] == "900"
    assert tenth.json()["error"]["code"] == "rate_limited"
    assert "www-authenticate" not in tenth.headers

    valid = await prod_mysql_dry_run(world, secret)
    assert valid.status_code == 429
    assert 0 < int(valid.headers["retry-after"]) <= 900
    assert valid.json() == tenth.json()

    stored = await counter(world)
    assert stored is not None and stored.failures == 11, "the refused valid call is counted too"
    assert stored.locked_until is not None
    async with world.session() as db:
        assert (
            await db.scalar(select(LoginCounter).where(LoginCounter.kind == LoginCounterKind.IP))
            is None
        )

    elsewhere = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app, client=("198.51.100.8", 1)),
        base_url="http://test",
    )
    async with elsewhere:
        free = await elsewhere.post(
            f"/api/v1/services/{world.services['prod/mysql']}/deploy",
            json={"dry_run": True},
            headers=bearer(secret),
        )
    assert free.status_code == 200


async def test_every_kind_of_failure_counts_and_the_lock_ends(api_world: ApiWorld) -> None:
    """Unknown, revoked and malformed attempts share one counter; the lock ends on its own."""
    world = api_world
    policy = AuthPolicy()
    secret = await make_token(world, "admin", name="doomed")
    async with world.session() as db:
        token = await find_token_by_secret(db, secret)
        assert token is not None
        await revoke_token(db, token, AuditActor.cli())
    base = utc_now()
    sent = [f"Bearer {secret}", f"Bearer {UNKNOWN_SECRET}", "Basic x", None, "Bearer"] * 2
    async with world.session() as db:
        for number, header in enumerate(sent, start=1):
            with pytest.raises(ApiRefusal) as refusal:
                await authenticate_token(db, header, ADDRESS, policy, now=base)
            expected = RefusalKind.RATE_LIMITED if number == 10 else RefusalKind.UNAUTHORIZED
            assert refusal.value.kind is expected
        with pytest.raises(ApiRefusal) as locked:
            await authenticate_token(db, None, ADDRESS, policy, now=base + timedelta(minutes=14))
        assert locked.value.kind is RefusalKind.RATE_LIMITED
        assert locked.value.retry_after == 60

    fresh = await make_token(world, "admin", name="fresh")
    after = base + policy.lockout_duration + timedelta(seconds=1)
    async with world.session() as db:
        caller = await authenticate_token(db, f"Bearer {fresh}", ADDRESS, policy, now=after)
        assert caller.owner.name == "admin"
        with pytest.raises(ApiRefusal) as again:
            await authenticate_token(db, None, ADDRESS, policy, now=after)
        assert again.value.kind is RefusalKind.UNAUTHORIZED
    restarted = await counter(world)
    assert restarted is not None and restarted.failures == 1 and restarted.locked_until is None


async def test_refusals_after_a_valid_token_do_not_count(api_world: ApiWorld) -> None:
    """A 403, 404 or 422 from a token that authenticated never counts toward the lock."""
    world = api_world
    read_only = await make_token(world, "admin", operations=("read_last_deployment",), name="ro")
    full = await make_token(world, "admin", name="full")
    mysql = world.services["prod/mysql"]
    for _ in range(12):
        assert (await deploy(world, read_only, mysql)).status_code == 403
        assert (await deploy(world, full, 987654)).status_code == 404
        assert (await deploy(world, full, mysql, {"force": True})).status_code == 422
    assert await counter(world) is None
    assert (await prod_mysql_dry_run(world, full)).status_code == 200


async def test_the_lock_survives_a_restart(api_world: ApiWorld) -> None:
    """The counters are stored, so a second application on the same data starts locked."""
    world = api_world
    for _ in range(10):
        await world.http.get("/api/v1/services/1/deployments/last", headers=bearer(UNKNOWN_SECRET))
    world.app.state.server_lock.release()  # the first process ends before the next starts
    restarted = create_app()
    async with restarted.router.lifespan_context(restarted):
        transport = httpx.ASGITransport(app=restarted, client=CLIENT_ADDRESS)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/api/v1/services/1/deployments/last")
    assert response.status_code == 429


@pytest.fixture
def trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust the address the test client connects from as a proxy; runs before the application."""
    monkeypatch.setenv("ENVMANAGER_TRUSTED_PROXIES", ADDRESS)


async def test_the_address_comes_from_a_trusted_proxy(
    trusted_proxy: None, api_world: ApiWorld
) -> None:
    """Behind a trusted proxy the forwarded address is counted, recorded and audited."""
    world = api_world
    forwarded = {"X-Forwarded-For": "203.0.113.9"}
    await world.http.get(
        "/api/v1/services/1/deployments/last", headers={**forwarded, **bearer(UNKNOWN_SECRET)}
    )
    assert await counter(world, "203.0.113.9") is not None
    assert await counter(world, ADDRESS) is None
    secret = await make_token(world, "admin")
    response = await world.http.post(
        f"/api/v1/services/{world.services['prod/mysql']}/deploy",
        json={"dry_run": True},
        headers={**forwarded, **bearer(secret)},
    )
    assert response.status_code == 200
    async with world.session() as db:
        token = await find_token_by_secret(db, secret)
    assert token is not None and token.last_used_ip == "203.0.113.9"
    assert {entry.ip_address for entry in await audit_entries(world)} >= {"203.0.113.9"}


async def test_junk_a_client_prepends_to_the_forwarded_list_still_counts_under_its_address(
    trusted_proxy: None, api_world: ApiWorld
) -> None:
    """The proxy appends the real address after whatever the client sent, so junk before it
    cannot make a request lose its address and escape the counter or the audit entry.
    """
    world = api_world
    junk = {"X-Forwarded-For": "unknown, <script>, 203.0.113.9"}
    await world.http.get(
        "/api/v1/services/1/deployments/last", headers={**junk, **bearer(UNKNOWN_SECRET)}
    )
    stored = await counter(world, "203.0.113.9")
    assert stored is not None and stored.failures == 1
    assert await counter(world, ADDRESS) is None
    failures = [e for e in await audit_entries(world) if e.action == "token.auth_failure"]
    assert {entry.ip_address for entry in failures} == {"203.0.113.9"}


async def test_forwarded_headers_are_ignored_without_a_trusted_proxy(api_world: ApiWorld) -> None:
    """Without a configured proxy a client cannot choose the address that is counted."""
    world = api_world
    await world.http.get(
        "/api/v1/services/1/deployments/last",
        headers={"X-Forwarded-For": "203.0.113.9", **bearer(UNKNOWN_SECRET)},
    )
    assert await counter(world, "203.0.113.9") is None
    assert await counter(world) is not None


async def test_a_call_records_the_last_use_even_when_it_is_refused(api_world: ApiWorld) -> None:
    """Last used time and address update on every call that authenticated, never on a bad token."""
    world = api_world
    secret = await make_token(world, "admin", operations=("read_last_deployment",))
    async with world.session() as db:
        token = await find_token_by_secret(db, secret)
        assert token is not None
        assert token.last_used_at is None and token.last_used_ip is None
        token_id = token.id

    async def used() -> tuple[datetime | None, str | None]:
        async with world.session() as db:
            found = await db.get(ApiToken, token_id)
            assert found is not None
            return found.last_used_at, found.last_used_ip

    old = utc_now() - timedelta(days=3)
    async with world.session() as db:
        await db.execute(update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=old))
        await db.commit()

    forbidden = await deploy(world, secret, world.services["prod/mysql"])
    assert forbidden.status_code == 403
    stamped, address = await used()
    assert stamped is not None and stamped > old + timedelta(days=2)
    assert address == ADDRESS

    async with world.session() as db:
        await db.execute(update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=old))
        await db.commit()
    missing = await last(world, secret, 987654)
    assert missing.status_code == 404
    assert (await used())[0] != old

    async with world.session() as db:
        token = await db.get(ApiToken, token_id)
        assert token is not None
        await revoke_token(db, token, AuditActor.cli())
        await db.execute(update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=old))
        await db.commit()
    assert (await last(world, secret, 987654)).status_code == 401
    assert (await used())[0] == old


async def test_disabling_or_demoting_the_owner_stops_the_token_at_once(
    api_world: ApiWorld,
) -> None:
    """The scope is loaded for every call: no cached grant or group outlives a change."""
    world = api_world
    secret = await make_token(world, "deployment_admin")
    assert (await prod_mysql_dry_run(world, secret)).status_code == 200
    async with world.session() as db:
        await db.execute(
            update(User).where(User.name == "deployment_admin").values(is_active=False)
        )
        await db.commit()
    assert (await prod_mysql_dry_run(world, secret)).status_code == 401
    async with world.session() as db:
        await db.execute(update(User).where(User.name == "deployment_admin").values(is_active=True))
        await db.commit()
    assert (await prod_mysql_dry_run(world, secret)).status_code == 200


async def test_a_flood_from_a_locked_address_writes_one_blocked_entry_per_window(
    api_world: ApiWorld,
) -> None:
    """Only the first refusal of a lockout window is audited; the rest are counted and answered."""
    world = api_world
    policy = AuthPolicy()
    base = utc_now()
    async with world.session() as db:
        for _ in range(10):
            with pytest.raises(ApiRefusal):
                await authenticate_token(db, None, ADDRESS, policy, now=base)
    before = len(await audit_entries(world))
    lock_seconds = int(policy.lockout_duration.total_seconds())
    async with world.session() as db:
        for number in range(1, 26):
            moment = base + timedelta(seconds=number)
            with pytest.raises(ApiRefusal) as refusal:
                await authenticate_token(db, None, ADDRESS, policy, now=moment)
            assert refusal.value.kind is RefusalKind.RATE_LIMITED
            assert refusal.value.retry_after is not None
            assert refusal.value.retry_after == lock_seconds - number

    blocked = (await audit_entries(world))[before:]
    assert [(entry.action, entry.result) for entry in blocked] == [
        ("token.auth_failure", AuditResult.BLOCKED)
    ]
    assert blocked[0].details == {"reason": "locked"}
    stored = await counter(world)
    assert stored is not None and stored.failures == 10 + 25

    after = base + policy.lockout_duration + timedelta(seconds=1)
    async with world.session() as db:
        for _ in range(10):
            with pytest.raises(ApiRefusal):
                await authenticate_token(db, None, ADDRESS, policy, now=after)
        with pytest.raises(ApiRefusal) as again:
            await authenticate_token(db, None, ADDRESS, policy, now=after)
        assert again.value.kind is RefusalKind.RATE_LIMITED
    later = (await audit_entries(world))[before + 1 :]
    reasons = [
        entry.details.get("reason") for entry in later if entry.result is AuditResult.BLOCKED
    ]
    assert reasons == ["too_many_failures", "locked"]


async def test_failed_attempts_are_audited_without_the_secret(api_world: ApiWorld) -> None:
    """Every refused attempt has an entry with the token and owner when known, never the secret."""
    world = api_world
    secret = await make_token(world, "admin", name="audited")
    async with world.session() as db:
        token = await find_token_by_secret(db, secret)
        assert token is not None
        await revoke_token(db, token, AuditActor.cli())
    before = len(await audit_entries(world))
    await last(world, UNKNOWN_SECRET, 1)
    await last(world, secret, 1)
    await world.http.get("/api/v1/services/1/deployments/last")
    entries = (await audit_entries(world))[before:]
    assert [entry.action for entry in entries] == ["token.auth_failure"] * 3
    unknown, revoked, missing = entries
    assert (unknown.actor_token_id, unknown.actor_user_id, unknown.actor_name) == (None, None, None)
    assert unknown.details == {"reason": "unknown"} and unknown.ip_address == ADDRESS
    assert revoked.actor_name == "token audited" and revoked.actor_token_id is not None
    assert revoked.actor_user_id is not None and revoked.details == {"reason": "revoked"}
    assert missing.details == {"reason": "missing"}
    assert all(entry.result is AuditResult.FAILURE for entry in entries)
    dump = repr([(entry.details, entry.target_name, entry.actor_name) for entry in entries])
    assert secret not in dump and hash_secret(secret) not in dump and UNKNOWN_SECRET not in dump


async def test_bad_tokens_that_arrive_together_are_all_answered_and_counted(
    api_world: ApiWorld,
) -> None:
    """The first failures of an address race on its counter; each still answers 401."""
    world = api_world
    url = "/api/v1/services/1/deployments/last"
    answers = await asyncio.gather(
        *(world.http.get(url, headers=bearer(UNKNOWN_SECRET)) for _ in range(8))
    )
    assert {answer.status_code for answer in answers} == {401}
    stored = await counter(world)
    assert stored is not None and stored.failures == 8 and stored.locked_until is None
