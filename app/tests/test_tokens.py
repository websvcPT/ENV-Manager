"""The token service: creation and its refusals, secrecy, restrictions, listing and revocation."""

import hashlib
import json
import string
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    AccessGrant,
    ApiToken,
    ApiTokenRestriction,
    AuditActorType,
    AuditEntry,
    AuditResult,
    RestrictionLevel,
    User,
    UserGroup,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.permissions import load_token_scope, load_user_scope
from app.services.tokens import (
    BEARER_PREFIX,
    HIDDEN_ITEM,
    OPERATION_DEPLOY,
    OPERATION_READ_LAST_DEPLOYMENT,
    PREFIX_LENGTH,
    CreatedToken,
    RestrictionChoices,
    RestrictionTarget,
    TokenError,
    TokenStatus,
    create_token,
    find_token_by_secret,
    generate_secret,
    get_manageable_token,
    hash_secret,
    is_live,
    list_tokens,
    restriction_choices,
    revoke_token,
    revoke_user_tokens,
    token_status,
    usage_example,
)

SEED_SECRET = "seed-token-billing-ci"


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def make_token(
    db: AsyncSession,
    owner: User,
    *,
    name: str = "ci",
    operations: tuple[str, ...] = (OPERATION_DEPLOY,),
    restriction: str = "",
    expires: str = "",
) -> CreatedToken:
    """Create a token for a seeded user the way the create form does."""
    scope = await load_user_scope(db, owner)
    return await create_token(
        db,
        owner,
        scope,
        name=name,
        operations=operations,
        restriction=restriction,
        expires=expires,
        actor=AuditActor.for_user(owner, "203.0.113.4"),
    )


async def audit_rows(db: AsyncSession, action: str | None = None) -> list[AuditEntry]:
    """Audit entries, oldest first, optionally of one action."""
    stmt = select(AuditEntry).order_by(AuditEntry.id)
    if action is not None:
        stmt = stmt.where(AuditEntry.action == action)
    return list((await db.execute(stmt)).scalars())


def test_a_secret_has_a_prefix_and_192_bits_of_hex() -> None:
    secrets_seen = {generate_secret() for _ in range(100)}
    assert len(secrets_seen) == 100
    for secret in secrets_seen:
        assert secret.startswith(BEARER_PREFIX)
        body = secret.removeprefix(BEARER_PREFIX)
        assert len(body) == 48 and set(body) <= set(string.hexdigits.lower())


def test_the_hash_is_the_sha256_hex_of_the_secret() -> None:
    assert hash_secret("abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(hash_secret(generate_secret())) == 64


async def test_the_seeded_token_secret_resolves_to_the_seeded_token(
    db: AsyncSession, seed_data: SeedData
) -> None:
    found = await find_token_by_secret(db, SEED_SECRET)
    assert found is not None and found.id == seed_data.tokens[0].id
    assert await find_token_by_secret(db, "seed-token-billing-cI") is None
    assert await find_token_by_secret(db, "") is None
    assert await find_token_by_secret(db, "x" * 500) is None


async def test_creating_a_token_stores_only_the_hash_and_a_short_prefix(
    db: AsyncSession, seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    created = await make_token(
        db, seed_data.users.admin, operations=(OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT)
    )
    assert created.secret.startswith(BEARER_PREFIX)
    async with create_session_factory(seeded_database)() as fresh:
        stored = await fresh.get(ApiToken, created.token.id)
        assert stored is not None
        assert stored.token_hash == hash_secret(created.secret)
        assert stored.prefix == created.secret[:PREFIX_LENGTH] and len(stored.prefix) == 13
        assert stored.operations == [OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT]
        assert stored.owner_id == seed_data.users.admin.id
        assert stored.revoked_at is None and stored.last_used_at is None
        for column in ApiToken.__table__.columns:
            assert created.secret not in str(getattr(stored, column.name))
        found = await find_token_by_secret(fresh, created.secret)
        assert found is not None and found.id == stored.id


async def test_the_secret_and_its_hash_never_reach_an_audit_entry(
    db: AsyncSession, seed_data: SeedData
) -> None:
    created = await make_token(db, seed_data.users.admin, restriction="")
    await revoke_token(db, created.token, AuditActor.for_user(seed_data.users.admin))
    entries = await audit_rows(db)
    text = json.dumps(
        [
            [e.action, e.actor_name, e.target_name, e.ip_address, e.details, e.version_ids]
            for e in entries
        ],
        default=str,
    )
    assert created.secret not in text
    assert created.secret[:PREFIX_LENGTH] not in text
    assert created.token.token_hash not in text
    assert created.secret.removeprefix(BEARER_PREFIX) not in text


async def test_creation_is_audited_with_the_owner_the_choices_and_no_scope_ids(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rentals = seed_data.projects.rentals
    tomorrow = (utc_now() + timedelta(days=1)).date().isoformat()
    created = await make_token(
        db,
        seed_data.users.admin,
        name="deploy-bot",
        operations=(OPERATION_READ_LAST_DEPLOYMENT, OPERATION_DEPLOY),
        restriction=f"project:{rentals.id}",
        expires=tomorrow,
    )
    (entry,) = await audit_rows(db, "token.create")
    assert (entry.actor_type, entry.actor_name, entry.actor_user_id) == (
        AuditActorType.USER,
        "admin",
        seed_data.users.admin.id,
    )
    assert entry.actor_token_id is None
    assert (entry.target_type, entry.target_id, entry.target_name) == (
        "token",
        created.token.id,
        "deploy-bot",
    )
    assert entry.result is AuditResult.SUCCESS and entry.ip_address == "203.0.113.4"
    assert entry.details == {
        "operations": [OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT],
        "restriction": "rentals",
        "expires": tomorrow,
    }
    assert (entry.project_id, entry.deployment_id, entry.service_id) == (None, None, None)


async def test_a_restriction_row_is_written_for_each_level_a_token_can_be_restricted_to(
    db: AsyncSession, seed_data: SeedData
) -> None:
    targets = {
        RestrictionLevel.PROJECT: seed_data.projects.rentals.id,
        RestrictionLevel.DEPLOYMENT: seed_data.deployments.rentals_prod.id,
        RestrictionLevel.SERVICE: seed_data.services.mysql.id,
    }
    for level, item_id in targets.items():
        created = await make_token(
            db,
            seed_data.users.admin,
            name=f"by-{level.value}",
            restriction=f"{level.value}:{item_id}",
        )
        (row,) = (
            await db.execute(
                select(ApiTokenRestriction).where(ApiTokenRestriction.token_id == created.token.id)
            )
        ).scalars()
        assert row.level is level
        assert {
            RestrictionLevel.PROJECT: row.project_id,
            RestrictionLevel.DEPLOYMENT: row.deployment_id,
            RestrictionLevel.SERVICE: row.service_id,
        }[level] == item_id
    unrestricted = await make_token(db, seed_data.users.admin, name="open")
    assert (
        await db.execute(
            select(ApiTokenRestriction).where(ApiTokenRestriction.token_id == unrestricted.token.id)
        )
    ).first() is None


async def test_a_restriction_narrows_the_owner_and_never_widens_them(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    created = await make_token(
        db, admin, restriction=f"service:{seed_data.services.mysql.id}", name="mysql-only"
    )
    scope = await load_token_scope(db, created.token)
    assert scope is not None and scope.user_id == admin.id
    assert scope.narrowing is not None
    assert scope.narrowing.service_ids == {seed_data.services.mysql.id}


@pytest.mark.parametrize(
    ("kwargs", "field", "message"),
    [
        ({"name": ""}, "name", "Enter a name."),
        ({"name": "   "}, "name", "Enter a name."),
        ({"name": "x" * 101}, "name", "Use 1 to 100 printable characters."),
        ({"name": "bad\nname"}, "name", "Use 1 to 100 printable characters."),
        ({"operations": ()}, "operations", "Choose at least one operation."),
        ({"operations": ("deploy", "delete_everything")}, "operations", "Choose from the listed"),
        ({"expires": "tomorrow"}, "expires", "Enter a valid date."),
        ({"expires": "2020-01-01"}, "expires", "The expiry date must be today or later."),
        ({"restriction": "project:999999"}, "restriction", "Choose an item from the list."),
        ({"restriction": "environment:1"}, "restriction", "Choose an item from the list."),
        ({"restriction": "garbage"}, "restriction", "Choose an item from the list."),
        ({"restriction": "project:12x"}, "restriction", "Choose an item from the list."),
    ],
)
async def test_a_refused_token_names_the_field_and_creates_nothing(
    db: AsyncSession,
    seed_data: SeedData,
    kwargs: dict[str, object],
    field: str,
    message: str,
) -> None:
    before = len((await db.execute(select(ApiToken))).scalars().all())
    with pytest.raises(TokenError) as refused:
        await make_token(db, seed_data.users.admin, **kwargs)  # type: ignore[arg-type]
    assert refused.value.field == field and message in str(refused.value)
    assert len((await db.execute(select(ApiToken))).scalars().all()) == before
    assert await audit_rows(db, "token.create") == []


async def test_a_restriction_outside_the_owners_reach_is_refused(
    db: AsyncSession, seed_data: SeedData
) -> None:
    billing = seed_data.projects.billing
    with pytest.raises(TokenError) as refused:
        await make_token(db, seed_data.users.project_admin, restriction=f"project:{billing.id}")
    assert refused.value.field == "restriction"
    with pytest.raises(TokenError):
        await make_token(
            db,
            seed_data.users.env_scoped_admin,
            restriction=f"service:{seed_data.services.web.id}",
        )


async def test_viewers_cannot_create_tokens(db: AsyncSession, seed_data: SeedData) -> None:
    with pytest.raises(TokenError) as refused:
        await make_token(db, seed_data.users.viewer)
    assert refused.value.field is None and "cannot create" in str(refused.value)
    assert await audit_rows(db, "token.create") == []


async def test_the_expiry_lasts_through_the_end_of_the_chosen_day_in_utc(
    db: AsyncSession, seed_data: SeedData, monkeypatch: pytest.MonkeyPatch
) -> None:
    noon = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("app.services.tokens.utc_now", lambda: noon)
    today = noon.date()
    created = await make_token(db, seed_data.users.admin, name="today", expires=today.isoformat())
    assert created.token.expires_at == datetime.combine(today, time(23, 59, 59), tzinfo=UTC)
    owner = seed_data.users.admin
    assert (
        token_status(created.token, owner, now=created.token.expires_at - timedelta(seconds=1))[0]
        is TokenStatus.ACTIVE
    )
    assert token_status(created.token, owner, now=created.token.expires_at)[0] is (
        TokenStatus.EXPIRED
    )
    forever = await make_token(db, owner, name="forever", expires="")
    assert forever.token.expires_at is None


async def test_names_are_unique_per_owner_among_tokens_that_are_not_revoked(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    first = await make_token(db, admin, name="Ci-Deploy")
    with pytest.raises(TokenError) as refused:
        await make_token(db, admin, name="ci-deploy")
    assert refused.value.field == "name" and "already have an active token" in str(refused.value)
    other = await make_token(db, seed_data.users.project_admin, name="ci-deploy")
    assert other.token.id != first.token.id
    await revoke_token(db, first.token, AuditActor.for_user(admin))
    again = await make_token(db, admin, name="ci-deploy")
    assert again.token.id not in (first.token.id, other.token.id)


async def test_the_restriction_choices_are_items_with_a_file_the_owner_may_deploy(
    db: AsyncSession, seed_data: SeedData
) -> None:
    def labels(choices: RestrictionChoices) -> dict[str, list[str]]:
        return {
            "projects": [choice.label for choice in choices.projects],
            "deployments": [choice.label for choice in choices.deployments],
            "services": [choice.label for choice in choices.services],
        }

    admin = await restriction_choices(db, await load_user_scope(db, seed_data.users.admin))
    assert labels(admin) == {
        "projects": ["billing", "rentals"],
        "deployments": ["billing / main", "rentals / prod", "rentals / staging"],
        "services": [
            "billing / main / api",
            "billing / main / worker",
            "rentals / prod / mysql",
            "rentals / prod / web",
            "rentals / staging / web",
        ],
    }
    assert admin.keys >= {f"project:{seed_data.projects.rentals.id}"}

    single_file = await restriction_choices(
        db, await load_user_scope(db, seed_data.users.env_scoped_admin)
    )
    assert labels(single_file) == {
        "projects": ["rentals"],
        "deployments": ["rentals / prod"],
        "services": ["rentals / prod / mysql"],
    }

    root = await restriction_choices(db, await load_user_scope(db, seed_data.users.superadmin))
    assert labels(root)["projects"] == ["billing", "rentals"]
    assert "billing / legacy" not in labels(root)["deployments"]

    none = await restriction_choices(db, await load_user_scope(db, seed_data.users.ungranted_admin))
    assert none.keys == frozenset()


def test_a_restriction_target_parses_only_the_levels_the_form_offers() -> None:
    assert RestrictionTarget.parse("project:3") == RestrictionTarget(RestrictionLevel.PROJECT, 3)
    assert RestrictionTarget.parse(" service:12 ") == RestrictionTarget(
        RestrictionLevel.SERVICE, 12
    )
    refused = ("environment:3", "project:", "project:-1", "project:1.5", "x:1", "")
    for value in (*refused, "project:" + "9" * 20):
        assert RestrictionTarget.parse(value) is None
    assert RestrictionTarget(RestrictionLevel.DEPLOYMENT, 4).key == "deployment:4"


def test_the_usage_example_reads_the_secret_from_the_environment() -> None:
    both = usage_example(
        "https://env.example.com/", (OPERATION_DEPLOY, OPERATION_READ_LAST_DEPLOYMENT), 7
    )
    assert "https://env.example.com/api/v1/services/7/deploy" in both
    assert "https://env.example.com/api/v1/services/7/deployments/last" in both
    assert both.count("Authorization: Bearer $ENVM_TOKEN") == 2
    assert '"dry_run": true' in both
    deploy_only = usage_example("http://localhost:8080", (OPERATION_DEPLOY,), None)
    assert "/services/SERVICE_ID/deploy" in deploy_only and "deployments/last" not in deploy_only
    read_only = usage_example("http://localhost:8080", (OPERATION_READ_LAST_DEPLOYMENT,), None)
    assert "-X POST" not in read_only and "deployments/last" in read_only


async def test_an_admin_lists_their_own_tokens_and_the_superadmin_everyones(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin, root = seed_data.users.admin, seed_data.users.superadmin
    await make_token(db, admin, name="newest")
    await make_token(db, root, name="roots")
    own = await list_tokens(db, admin, await load_user_scope(db, admin))
    assert [row.name for row in own] == ["newest", "billing-ci"]
    assert {row.owner_name for row in own} == {"admin"}
    everyone = await list_tokens(db, root, await load_user_scope(db, root))
    assert [(row.name, row.owner_name) for row in everyone] == [
        ("roots", "root"),
        ("newest", "admin"),
        ("billing-ci", "admin"),
    ]
    other = await list_tokens(
        db, seed_data.users.project_admin, await load_user_scope(db, seed_data.users.project_admin)
    )
    assert other == []


async def test_a_row_shows_what_the_token_may_do_where_and_when(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    created = await make_token(
        db,
        admin,
        name="both",
        operations=(OPERATION_READ_LAST_DEPLOYMENT, OPERATION_DEPLOY),
        restriction=f"deployment:{seed_data.deployments.rentals_prod.id}",
    )
    created.token.last_used_at = utc_now()
    created.token.last_used_ip = "198.51.100.7"
    await db.commit()
    rows = {row.name: row for row in await list_tokens(db, admin, await load_user_scope(db, admin))}
    both = rows["both"]
    assert both.operations == ("Deploy", "Read last deployment")
    assert both.restriction == "rentals / prod"
    assert (both.prefix, both.last_used_ip) == (created.secret[:PREFIX_LENGTH], "198.51.100.7")
    assert both.status is TokenStatus.ACTIVE and both.revocable
    assert rows["billing-ci"].restriction == "billing / main / api / .env.api"
    assert rows["billing-ci"].operations == ("Deploy",)
    assert rows["billing-ci"].expires_at is None and rows["billing-ci"].last_used_at is None


async def test_the_restriction_column_never_names_an_item_the_viewer_lost_access_to(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin, root = seed_data.users.admin, seed_data.users.superadmin
    rentals = seed_data.projects.rentals
    await make_token(db, admin, name="rentals-only", restriction=f"project:{rentals.id}")
    await db.execute(
        delete(AccessGrant).where(
            AccessGrant.user_id == admin.id, AccessGrant.project_id == seed_data.projects.rentals.id
        )
    )
    await db.commit()
    rows = {row.name: row for row in await list_tokens(db, admin, await load_user_scope(db, admin))}
    assert rows["rentals-only"].restriction == HIDDEN_ITEM
    assert "rentals" not in rows["rentals-only"].restriction
    assert rows["billing-ci"].restriction.startswith("billing")
    as_root = {
        row.name: row for row in await list_tokens(db, root, await load_user_scope(db, root))
    }
    assert as_root["rentals-only"].restriction == "rentals"


async def test_an_item_the_viewer_only_sees_as_context_keeps_its_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    single_file = seed_data.users.env_scoped_admin
    scope = await load_user_scope(db, single_file)
    created = await make_token(
        db, single_file, name="mysql", restriction=f"service:{seed_data.services.mysql.id}"
    )
    rows = await list_tokens(db, single_file, scope)
    assert rows[0].name == created.token.name and rows[0].restriction == "rentals / prod / mysql"


async def test_a_row_is_revocable_only_while_revoking_changes_something(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    expiring = await make_token(
        db, admin, name="short", expires=(utc_now().date() + timedelta(days=1)).isoformat()
    )
    scope = await load_user_scope(db, admin)
    later = utc_now() + timedelta(days=3)
    rows = {row.name: row for row in await list_tokens(db, admin, scope, now=later)}
    assert rows["short"].status is TokenStatus.EXPIRED and not rows["short"].revocable
    assert rows["billing-ci"].revocable
    await revoke_token(db, expiring.token, AuditActor.for_user(admin))
    rows = {row.name: row for row in await list_tokens(db, admin, scope)}
    assert rows["short"].status is TokenStatus.REVOKED and not rows["short"].revocable


async def test_revoking_stops_the_token_at_once_and_is_audited(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    created = await make_token(db, admin, name="to-revoke")
    assert await load_token_scope(db, created.token) is not None
    assert is_live(created.token)
    revoked = await revoke_token(db, created.token, AuditActor.for_user(admin, "203.0.113.4"))
    assert revoked and created.token.revoked_at is not None
    assert await load_token_scope(db, created.token) is None
    assert not is_live(created.token)
    (entry,) = await audit_rows(db, "token.revoke")
    assert (entry.actor_name, entry.target_name, entry.result) == (
        "admin",
        "to-revoke",
        AuditResult.SUCCESS,
    )
    assert entry.details == {"owner": "admin", "reason": "requested"}
    assert (entry.target_type, entry.target_id) == ("token", created.token.id)
    again = await revoke_token(db, created.token, AuditActor.for_user(admin))
    assert not again
    assert len(await audit_rows(db, "token.revoke")) == 1


async def test_a_superadmin_revoking_names_themselves_and_the_owner(
    db: AsyncSession, seed_data: SeedData
) -> None:
    created = await make_token(db, seed_data.users.admin, name="theirs")
    await revoke_token(db, created.token, AuditActor.for_user(seed_data.users.superadmin))
    (entry,) = await audit_rows(db, "token.revoke")
    assert entry.actor_name == "root" and entry.details == {"owner": "admin", "reason": "requested"}


async def test_only_the_owner_or_a_superadmin_can_manage_a_token(
    db: AsyncSession, seed_data: SeedData
) -> None:
    created = await make_token(db, seed_data.users.admin, name="mine")
    token_id = created.token.id
    owner = await get_manageable_token(db, seed_data.users.admin, token_id)
    assert owner is not None and owner.id == token_id
    assert await get_manageable_token(db, seed_data.users.superadmin, token_id) is not None
    assert await get_manageable_token(db, seed_data.users.project_admin, token_id) is None
    assert await get_manageable_token(db, seed_data.users.admin, 999999) is None


async def test_revoking_all_of_a_users_tokens_skips_those_already_revoked(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    first = await make_token(db, admin, name="first")
    second = await make_token(db, admin, name="second")
    other = await make_token(db, seed_data.users.project_admin, name="not-theirs")
    await revoke_token(db, first.token, AuditActor.for_user(admin))
    stamp = first.token.revoked_at
    count = await revoke_user_tokens(
        db, admin, AuditActor(AuditActorType.CLI), reason="user_disabled"
    )
    await db.commit()
    assert count == 2  # billing-ci and second
    assert first.token.revoked_at == stamp
    assert second.token.revoked_at is not None and other.token.revoked_at is None
    entries = await audit_rows(db, "token.revoke")
    assert [(e.actor_name, e.target_name, e.details["reason"]) for e in entries[1:]] == [
        ("cli", "billing-ci", "user_disabled"),
        ("cli", "second", "user_disabled"),
    ]


async def test_a_status_comes_from_one_place_for_every_state(
    db: AsyncSession, seed_data: SeedData
) -> None:
    owner = seed_data.users.admin
    token = (await db.execute(select(ApiToken))).scalars().first()
    assert token is not None
    now = utc_now()
    assert token_status(token, owner, now=now) == (TokenStatus.ACTIVE, None)
    owner.is_active = False
    assert token_status(token, owner, now=now) == (TokenStatus.STOPPED, "User disabled")
    owner.is_active = True
    owner.group = UserGroup.VIEWER
    assert token_status(token, owner, now=now) == (
        TokenStatus.STOPPED,
        "Viewers cannot use tokens",
    )
    owner.group = UserGroup.ADMIN
    token.expires_at = now - timedelta(seconds=1)
    assert token_status(token, owner, now=now) == (TokenStatus.EXPIRED, None)
    token.revoked_at = now
    assert token_status(token, owner, now=now) == (TokenStatus.REVOKED, None)
