"""Audit log service: scope, filters, paging, filter options, rows and entries by role."""

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import (
    ApiToken,
    AuditActorType,
    AuditEntry,
    AuditResult,
    Environment,
    EnvironmentVersion,
    User,
)
from app.seeds import (
    BULK_ACTION,
    BULK_COUNT,
    DELETED_PROJECT_ID,
    SeedData,
    ensure_audit_figures,
)
from app.services.audit import AuditActor, record_actor_audit, record_audit
from app.services.audit_details import detail_lines
from app.services.audit_filters import AuditFilters, Cursor, parse_number
from app.services.audit_log import (
    PAGE_SIZE,
    AuditPage,
    AuditRow,
    FilterOptions,
    count_entries,
    list_page,
    load_entry,
    load_filter_options,
)
from app.services.permissions import Visibility, compute_visibility, load_user_scope

LEGACY_ENTRIES = 4
FIGURE_ENTRIES = 9 + BULK_COUNT
ALL_ENTRIES = LEGACY_ENTRIES + FIGURE_ENTRIES


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database with the audit figures added."""
    async with create_session_factory(seeded_database)() as session:
        await ensure_audit_figures(session)
        await session.commit()
        yield session


async def visibility_of(db: AsyncSession, user: User) -> Visibility:
    """What a user sees, as the audit screen computes it."""
    return await compute_visibility(db, await load_user_scope(db, user))


async def every_row(
    db: AsyncSession, visibility: Visibility, filters: AuditFilters
) -> list[AuditRow]:
    """All rows of a view, followed page by page."""
    rows: list[AuditRow] = []
    cursor: Cursor | None = None
    while True:
        page = await list_page(db, visibility, filters, cursor=cursor)
        rows.extend(page.rows)
        if page.next_cursor is None:
            return rows
        cursor = Cursor.parse(page.next_cursor)


def actions_of(rows: list[AuditRow]) -> list[str]:
    """The actions of rows, in order."""
    return [row.action for row in rows]


async def test_the_sql_scope_matches_the_python_rule_for_every_user_and_every_id_shape(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Whatever ids an entry carries, the clause admits it exactly when the Python rule does."""
    chains = {
        "Fancy Proj. Name db": (
            seed_data.projects.fancy.id,
            seed_data.deployments.fancy_prod.id,
            seed_data.services.mysql.id,
            seed_data.envs.db.id,
        ),
        "Fancy Proj. Name secrets": (
            seed_data.projects.fancy.id,
            seed_data.deployments.fancy_prod.id,
            seed_data.services.mysql.id,
            seed_data.envs.db_secrets.id,
        ),
        "billing api": (
            seed_data.projects.billing.id,
            seed_data.deployments.billing_main.id,
            seed_data.services.api.id,
            seed_data.envs.api.id,
        ),
        "gone": (DELETED_PROJECT_ID, 900002, 900003, 900004),
    }
    users = seed_data.users
    token = (await db.execute(select(ApiToken).where(ApiToken.name == "billing-ci"))).scalar_one()
    actors = [
        AuditActor(AuditActorType.SYSTEM),
        AuditActor.for_user(users.admin),
        AuditActor.for_user(users.env_scoped_admin),
        AuditActor.for_user(users.viewer),
        AuditActor.for_user(users.ungranted_admin),
        AuditActor.for_token(token, users.admin),
    ]
    for ids in chains.values():
        for present in itertools.product((False, True), repeat=4):
            project, deployment, service, environment = (
                value if keep else None for value, keep in zip(ids, present, strict=True)
            )
            for actor in actors:
                await record_actor_audit(
                    db,
                    actor,
                    action="shape.test",
                    result=AuditResult.SUCCESS,
                    project_id=project,
                    deployment_id=deployment,
                    service_id=service,
                    environment_id=environment,
                )
    await db.commit()
    entries = (await db.execute(select(AuditEntry))).scalars().all()
    assert len(entries) > 64 * len(actors)
    for user in vars(users).values():
        visibility = await visibility_of(db, user)
        by_sql = set(
            (await db.execute(select(AuditEntry.id).where(visibility.audit_entry_clause())))
            .scalars()
            .all()
        )
        by_rule = {
            entry.id
            for entry in entries
            if visibility.includes_audit_entry(
                actor_user_id=entry.actor_user_id,
                project_id=entry.project_id,
                deployment_id=entry.deployment_id,
                service_id=entry.service_id,
                environment_id=entry.environment_id,
            )
        }
        assert by_sql == by_rule, user.name


async def test_each_role_sees_only_the_entries_of_its_scope(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The superadmin sees everything, the others the entries of their items, newest first."""
    users = seed_data.users

    async def seen(user: User) -> list[AuditRow]:
        return await every_row(db, await visibility_of(db, user), AuditFilters())

    assert len(await seen(users.superadmin)) == ALL_ENTRIES
    assert [(row.action, row.object_name) for row in await seen(users.admin)] == [
        ("deploy", "mysql"),
        ("environment.save", ".env.db"),
        ("environment.dist_check", ".env.api"),
        ("service.deploy", "api"),
        ("environment.reveal", ".env.db.secrets"),
        ("deploy", "web"),
        ("file_save", ".env.db"),
        ("file_save", ".env.api"),
        ("deployment.update", "prod"),
        ("project.rename", "Fancy Proj. Name"),
    ]
    assert len(await seen(users.project_admin)) == 7
    assert "project.rename" in actions_of(await seen(users.project_admin))
    deployment_rows = await seen(users.deployment_admin)
    assert len(deployment_rows) == 6
    assert "project.rename" not in actions_of(deployment_rows)
    assert [(row.action, row.object_name) for row in await seen(users.env_scoped_admin)] == [
        ("environment.save", ".env.db"),
        ("file_save", ".env.db"),
    ]
    assert await seen(users.ungranted_admin) == []


async def test_entries_without_scope_or_for_deleted_items_are_for_the_superadmin_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """User changes, failed logins, backups and a deleted project never reach an admin."""
    hidden = {"user.create", "login.failure", BULK_ACTION, "project.delete"}
    admin_rows = await every_row(db, await visibility_of(db, seed_data.users.admin), AuditFilters())
    assert not hidden & set(actions_of(admin_rows))
    superadmin_rows = await every_row(
        db, await visibility_of(db, seed_data.users.superadmin), AuditFilters()
    )
    assert hidden <= set(actions_of(superadmin_rows))


async def test_rows_are_newest_first_and_a_cursor_walks_every_page_once(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Pages hold 50 rows, follow created_at then id, and never repeat or skip an entry."""
    visibility = await visibility_of(db, seed_data.users.superadmin)
    first = await list_page(db, visibility, AuditFilters())
    assert len(first.rows) == PAGE_SIZE
    assert first.next_cursor is not None
    rows = await every_row(db, visibility, AuditFilters())
    assert len(rows) == ALL_ENTRIES
    assert len({row.id for row in rows}) == ALL_ENTRIES
    moments = [(row.created_at, row.id) for row in rows]
    assert moments == sorted(moments, reverse=True)
    last = await list_page(db, visibility, AuditFilters(), cursor=Cursor.parse(first.next_cursor))
    assert last.rows[0].id == rows[PAGE_SIZE].id


async def test_the_cursor_orders_entries_with_the_same_time_by_id(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A page boundary inside a group of entries with one timestamp loses none of them."""
    moment = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    for _ in range(5):
        await record_audit(
            db,
            action="tie.test",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.SYSTEM,
            created_at=moment,
        )
    await db.commit()
    visibility = await visibility_of(db, seed_data.users.superadmin)
    filters = AuditFilters(action="tie.test")
    seen: list[int] = []
    cursor: Cursor | None = None
    while True:
        page = await list_page(db, visibility, filters, cursor=cursor, limit=2)
        seen.extend(row.id for row in page.rows)
        if page.next_cursor is None:
            break
        cursor = Cursor.parse(page.next_cursor)
    assert len(seen) == 5 and seen == sorted(seen, reverse=True)


def test_the_cursor_round_trips_and_malformed_tokens_are_ignored() -> None:
    """The token carries the microsecond time and the id; anything else is no cursor."""
    moment = datetime(2026, 10, 17, 9, 12, 5, 123456, tzinfo=UTC)
    token = Cursor(moment, 42).encode()
    assert Cursor.parse(token) == Cursor(moment, 42)
    for bad in (None, "", "abc", "12-", "-5", "1-2-3", "99999999999999999999-1", "1.5-2"):
        assert Cursor.parse(bad) is None


def test_filters_drop_what_does_not_parse_and_round_trip_what_does() -> None:
    """Bad values never raise: they mean no filter, and good ones return as URL parameters."""
    assert AuditFilters.parse({}) == AuditFilters()
    assert (
        AuditFilters.parse(
            {
                "project": "abc",
                "actor": "nobody:x",
                "action": "Not An Action!",
                "result": "great",
                "from": "2026-13-45",
                "to": "20261017",
            }
        )
        == AuditFilters()
    )
    blank = AuditFilters.parse(
        {"project": "", "actor": "", "action": "", "result": "", "from": "", "to": ""}
    )
    assert blank == AuditFilters() and not blank.active
    good = AuditFilters.parse(
        {
            "project": "3",
            "actor": "api:token ci",
            "action": "service.deploy",
            "result": "halted",
            "from": "2026-10-01",
            "to": "2026-10-17",
        }
    )
    assert good.active
    assert good.params() == {
        "project": "3",
        "actor": "api:token ci",
        "action": "service.deploy",
        "result": "halted",
        "from": "2026-10-01",
        "to": "2026-10-17",
    }
    assert AuditFilters.parse(good.params()) == good
    assert good.query_string(before="9-1").endswith("&before=9-1")


@pytest.mark.parametrize("raw", [None, "", "0", "-1", "1.5", "abc", "9" * 16, " 1"])
def test_numbers_that_are_not_positive_whole_numbers_are_ignored(raw: str | None) -> None:
    """Ids from the URL are strictly digits."""
    assert parse_number(raw) is None


async def test_every_filter_narrows_and_they_combine(db: AsyncSession, seed_data: SeedData) -> None:
    """Project, actor, action, result and the date range each filter, and combine with AND."""
    visibility = await visibility_of(db, seed_data.users.superadmin)
    fancy = seed_data.projects.fancy.id

    async def rows(filters: AuditFilters) -> list[AuditRow]:
        return await every_row(db, visibility, filters)

    assert len(await rows(AuditFilters(project_id=fancy))) == 7
    assert actions_of(await rows(AuditFilters(action="environment.save"))) == ["environment.save"]
    assert actions_of(await rows(AuditFilters(result=AuditResult.HALTED))) == ["service.deploy"]
    token = AuditFilters(actor_type=AuditActorType.API, actor_name="token billing-ci")
    assert [row.actor for row in await rows(token)] == ["billing-ci"]
    admin = AuditFilters(actor_type=AuditActorType.USER, actor_user_id=seed_data.users.admin.id)
    assert len(await rows(admin)) == 3
    assert len(await rows(AuditFilters(action=BULK_ACTION))) == BULK_COUNT
    day = date(2026, 10, 17)
    on_the_day = await rows(AuditFilters(date_from=day, date_to=day))
    assert sorted(actions_of(on_the_day)) == ["deploy", "environment.save"]
    assert len(await rows(AuditFilters(date_to=date(2026, 10, 14)))) == ALL_ENTRIES - 8
    combined = AuditFilters(
        project_id=fancy, result=AuditResult.SUCCESS, date_from=date(2026, 10, 16)
    )
    assert actions_of(await rows(combined)) == ["environment.save", "environment.reveal", "deploy"]
    assert await rows(AuditFilters(project_id=fancy, result=AuditResult.HALTED)) == []
    assert await count_entries(db, visibility, AuditFilters(project_id=fancy)) == 7
    assert await count_entries(db, visibility, AuditFilters()) == ALL_ENTRIES


async def test_the_last_day_of_the_range_is_included_whole(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """An entry at 23:59:59 on the end day is inside the range, the next second is not."""
    for moment in (
        datetime(2031, 5, 4, 23, 59, 59, tzinfo=UTC),
        datetime(2031, 5, 5, 0, 0, 0, tzinfo=UTC),
        datetime(2031, 5, 3, 23, 59, 59, tzinfo=UTC),
    ):
        await record_audit(
            db,
            action="range.test",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.SYSTEM,
            created_at=moment,
        )
    await db.commit()
    visibility = await visibility_of(db, seed_data.users.superadmin)
    rows = await every_row(
        db,
        visibility,
        AuditFilters(action="range.test", date_from=date(2031, 5, 4), date_to=date(2031, 5, 4)),
    )
    assert [row.created_at for row in rows] == [datetime(2031, 5, 4, 23, 59, 59, tzinfo=UTC)]


async def test_filter_options_offer_only_values_found_in_the_callers_entries(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Projects, actors and actions come from visible entries, so nothing hidden is named."""

    async def options(user: User) -> FilterOptions:
        return await load_filter_options(db, await visibility_of(db, user))

    superadmin = await options(seed_data.users.superadmin)
    assert [name for _, name in superadmin.projects] == ["billing", "Fancy Proj. Name"]
    assert {option.label for option in superadmin.actors} == {
        "admin (admin@example.test)",
        "root (root@example.test)",
        "cli",
        "system",
        "token billing-ci",
    }
    assert {option.value for option in superadmin.actors if option.value.startswith("user:")} == {
        f"user:{seed_data.users.admin.id}",
        f"user:{seed_data.users.superadmin.id}",
    }
    assert BULK_ACTION in superadmin.actions
    assert [value for value, _ in superadmin.results] == ["success", "halted", "blocked", "failure"]

    admin = await options(seed_data.users.admin)
    assert [name for _, name in admin.projects] == ["billing", "Fancy Proj. Name"]
    assert {option.label for option in admin.actors} == {
        "admin (admin@example.test)",
        "root (root@example.test)",
        "token billing-ci",
    }
    assert not {BULK_ACTION, "user.create", "login.failure", "project.delete"} & set(admin.actions)

    scoped = await options(seed_data.users.env_scoped_admin)
    assert [name for _, name in scoped.projects] == ["Fancy Proj. Name"]
    assert {option.label for option in scoped.actors} == {
        "admin (admin@example.test)",
        "root (root@example.test)",
    }
    assert scoped.actions == ("environment.save", "file_save")

    nobody = await options(seed_data.users.ungranted_admin)
    assert (nobody.projects, nobody.actors, nobody.actions) == ((), (), ())


async def test_a_project_that_was_deleted_is_not_offered(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Entries of a deleted project stay listed for the superadmin but cannot be picked."""
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    options = await load_filter_options(db, superadmin)
    assert DELETED_PROJECT_ID not in {project_id for project_id, _ in options.projects}
    rows = await every_row(db, superadmin, AuditFilters(project_id=DELETED_PROJECT_ID))
    assert actions_of(rows) == ["project.delete"]


async def test_an_entry_outside_the_scope_looks_like_one_that_does_not_exist(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Loading one entry answers None for hidden and missing ids alike."""
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    rows = await every_row(db, superadmin, AuditFilters())
    by_action = {row.action: row for row in rows}
    scoped = await visibility_of(db, seed_data.users.env_scoped_admin)
    shown = by_action["environment.save"]
    assert (await load_entry(db, scoped, shown.id)) == shown
    for hidden in (by_action["service.deploy"], by_action["user.create"], by_action[BULK_ACTION]):
        assert await load_entry(db, scoped, hidden.id) is None
    assert await load_entry(db, scoped, 987654) is None
    assert (await load_entry(db, superadmin, by_action["service.deploy"].id)) is not None


async def test_rows_describe_the_actor_the_object_and_the_versions(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """Tokens lose their label prefix, the CLI and system have a kind, versions have names."""
    visibility = await visibility_of(db, seed_data.users.superadmin)
    rows = await every_row(db, visibility, AuditFilters())
    by_action = {row.action: row for row in rows}
    token = by_action["service.deploy"]
    assert (token.actor, token.actor_kind, token.ip_address) == (
        "billing-ci",
        "token",
        "203.0.113.9",
    )
    assert token.versions == (".env.api v1",)
    cli = by_action["user.create"]
    assert (cli.actor, cli.actor_kind, cli.ip_address) == ("cli", "cli", "127.0.0.1")
    anonymous = by_action["login.failure"]
    assert (anonymous.actor, anonymous.actor_kind) == ("unknown", "user")
    assert by_action[BULK_ACTION].actor_kind == "system"
    saved = by_action["environment.save"]
    assert saved.versions == (".env.db v2",)
    assert [(line.key, line.text) for line in saved.details] == [
        ("version", "2"),
        ("mode", "keys"),
        ("changed keys", "DB_PORT"),
    ]
    assert saved.short_trace is None


async def test_a_version_is_named_only_when_it_lies_within_the_item_of_the_entry(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """A version id that belongs to another file, or to none, is shown as #id and names nothing.

    Version ids are reused once a file is deleted, so an old entry can carry an id that now
    belongs to a file the entry says nothing about.
    """
    versions = dict(
        (
            await db.execute(
                select(Environment.name, EnvironmentVersion.id)
                .join(EnvironmentVersion, EnvironmentVersion.environment_id == Environment.id)
                .where(EnvironmentVersion.number == 1)
            )
        ).all()
    )
    api, secrets, saved = versions[".env.api"], versions[".env.db.secrets"], versions[".env.db"]
    fancy = seed_data.projects.fancy.id
    prod = seed_data.deployments.fancy_prod.id

    async def entry(
        *,
        project_id: int,
        deployment_id: int | None = None,
        service_id: int | None = None,
        environment_id: int | None = None,
    ) -> int:
        created = await record_audit(
            db,
            action="version.test",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.SYSTEM,
            project_id=project_id,
            deployment_id=deployment_id,
            service_id=service_id,
            environment_id=environment_id,
            version_ids=[api, secrets, saved, 987654],
        )
        await db.commit()
        return created.id

    prod_entry = await entry(project_id=fancy, deployment_id=prod)
    file_entry = await entry(
        project_id=fancy,
        deployment_id=prod,
        service_id=seed_data.services.mysql.id,
        environment_id=seed_data.envs.db.id,
    )
    project_entry = await entry(project_id=fancy)
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    for entry_id, expected in (
        (prod_entry, (f"#{api}", ".env.db.secrets v1", ".env.db v1", "#987654")),
        (file_entry, (f"#{api}", f"#{secrets}", ".env.db v1", "#987654")),
        (project_entry, (f"#{api}", ".env.db.secrets v1", ".env.db v1", "#987654")),
    ):
        row = await load_entry(db, superadmin, entry_id)
        assert row is not None and row.versions == expected
        assert row.version_ids == (api, secrets, saved, 987654)


async def test_a_trace_id_is_shortened_when_the_entry_has_one(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The detail line shows the first and last four characters of a trace id."""
    entry = await record_audit(
        db, action="trace.test", result=AuditResult.SUCCESS, actor_type=AuditActorType.SYSTEM
    )
    entry.trace_id = "9c1fabcdef0123456789abcdefe2a000"
    await db.commit()
    visibility = await visibility_of(db, seed_data.users.superadmin)
    row = await load_entry(db, visibility, entry.id)
    assert row is not None and row.short_trace == "9c1f…a000"


async def test_a_page_of_no_rows_has_no_cursor(db: AsyncSession, seed_data: SeedData) -> None:
    """An empty result ends the paging at once."""
    visibility = await visibility_of(db, seed_data.users.ungranted_admin)
    page: AuditPage = await list_page(db, visibility, AuditFilters())
    assert page.rows == () and page.next_cursor is None


@pytest.mark.parametrize(
    ("user", "sees_text"),
    [
        ("superadmin", True),
        ("admin", False),
        ("project_admin", False),
        ("deployment_admin", False),
    ],
)
async def test_the_command_text_of_a_hook_save_is_shown_to_the_superadmin_only(
    db: AsyncSession, seed_data: SeedData, user: str, sees_text: bool
) -> None:
    """Who may read the entry may read that commands changed, but not what they said.

    The text is taken out of the list rows, the single entry and the export column alike.
    """
    saved = await record_actor_audit(
        db,
        AuditActor.for_user(seed_data.users.superadmin),
        action="service.hook_save",
        result=AuditResult.SUCCESS,
        target_type="service",
        target_id=seed_data.services.mysql.id,
        target_name="mysql",
        project_id=seed_data.projects.fancy.id,
        deployment_id=seed_data.deployments.fancy_prod.id,
        service_id=seed_data.services.mysql.id,
        details={
            "hook": "post_deploy",
            "changed": ["commands"],
            "commands": 1,
            "previous_commands": "docker compose up -d --no-deps old-marker-3917",
            "new_commands": "docker compose up -d --no-deps new-marker-3917",
        },
    )
    await db.commit()
    visibility = await visibility_of(db, getattr(seed_data.users, user))
    page = await list_page(db, visibility, AuditFilters(action="service.hook_save"))
    single = await load_entry(db, visibility, saved.id)
    assert single is not None and [row.id for row in page.rows] == [saved.id]
    for row in (page.rows[0], single):
        shown = " ".join(f"{line.key} {line.text}" for line in row.details)
        assert ("new-marker-3917" in shown) is sees_text
        assert ("old-marker-3917" in shown) is sees_text
        assert ("new-marker-3917" in row.details_json) is sees_text
        assert ("previous_commands" in row.details_json) is sees_text
        assert '"changed":["commands"]' in row.details_json


def test_the_most_command_text_the_safety_net_allows_is_shown_whole() -> None:
    """Ten lines of 500 characters fit the display bound that cuts other long text."""
    longest = "\n".join(["docker " + "x" * 493] * 10)
    lines = {line.key: line for line in detail_lines({"new_commands": longest, "output": longest})}
    assert lines["new commands"].text == longest
    assert lines["output"].text == longest[:4000] + "..."
