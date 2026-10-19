"""The read models of the Users screens: list rows, token statuses and the detail."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import ApiToken, AuditActorType, AuditResult, User, UserGroup
from app.models.types import utc_now
from app.seeds import SeedData
from app.services.audit import record_audit
from app.services.tokens import TokenStatus
from app.services.user_detail import (
    ACTIVITY_LIMIT,
    PAGE_SIZE,
    UserFilters,
    UserRow,
    list_user_rows,
    load_user_detail,
    token_row,
)
from app.tests.grant_helpers import count_statements, grow


async def whole_list(
    db: AsyncSession, filters: UserFilters | None = None, limit: int = 1
) -> list[UserRow]:
    """Every row of the list read page by page, so the cursor is what joins the pages."""
    rows: list[UserRow] = []
    after: int | None = None
    while True:
        page = await list_user_rows(db, filters, after=after, limit=limit)
        rows.extend(page.rows)
        if page.next_after is None:
            return rows
        after = page.next_after


async def test_rows_are_ordered_by_name_and_count_the_projects_the_grants_reach(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rows = (await list_user_rows(db)).rows
    names = [row.name for row in rows]
    assert names == sorted(names, key=str.lower)
    by_name = {row.name: row for row in rows}
    assert by_name["root"].projects is None
    assert by_name["root"].group is UserGroup.SUPERADMIN
    assert by_name["admin"].projects == 2
    assert by_name["viewer"].projects == 1
    assert by_name["project_admin"].projects == 1
    assert by_name["deployment_admin"].projects == 1
    assert by_name["env_scoped_admin"].projects == 1
    assert by_name["env_scoped_viewer"].projects == 2
    assert by_name["ungranted_admin"].projects == 0
    assert by_name["admin"].email == "admin@example.test" and by_name["admin"].is_active


async def test_a_disabled_user_keeps_their_row_and_their_count(
    db: AsyncSession, seed_data: SeedData
) -> None:
    viewer = await db.get(User, seed_data.users.viewer.id)
    assert viewer is not None
    viewer.is_active = False
    await db.commit()
    row = next(row for row in (await list_user_rows(db)).rows if row.name == "viewer")
    assert not row.is_active and row.projects == 1


async def test_active_accounts_come_first_and_disabled_ones_follow_each_by_name(
    db: AsyncSession, seed_data: SeedData
) -> None:
    await db.execute(
        update(User)
        .where(User.id.in_([seed_data.users.admin.id, seed_data.users.viewer.id]))
        .values(is_active=False)
    )
    await db.commit()
    rows = (await list_user_rows(db)).rows
    states = [row.is_active for row in rows]
    assert states == sorted(states, reverse=True) and states.count(False) == 2
    assert [row.name for row in rows if not row.is_active] == ["admin", "viewer"]
    active = [row.name for row in rows if row.is_active]
    assert active == sorted(active, key=str.lower)


async def test_pages_join_without_repeating_or_skipping_a_user(
    db: AsyncSession, seed_data: SeedData
) -> None:
    await grow(db, projects=0, users=PAGE_SIZE * 2 + 7)
    await db.execute(
        update(User).where(User.name.in_(["bulk-3", "bulk-60", "bulk-100"])).values(is_active=False)
    )
    await db.commit()
    everything = list((await list_user_rows(db, limit=10_000)).rows)
    assert len(everything) > PAGE_SIZE * 2
    first = await list_user_rows(db)
    assert len(first.rows) == PAGE_SIZE and first.next_after == first.rows[-1].id
    second = await list_user_rows(db, after=first.next_after)
    assert second.rows[0] == everything[PAGE_SIZE]
    assert await whole_list(db, limit=PAGE_SIZE) == everything
    assert await whole_list(db, limit=1) == everything
    last = await list_user_rows(db, after=everything[-2].id)
    assert last.rows == (everything[-1],) and last.next_after is None


async def test_the_cursor_follows_the_order_of_names_in_any_script(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """SQLite lowers ASCII letters only, so a name such as Émile sorts by what the list sorts by."""
    names = ("Émile", "émile", "Zoë", "zoë", "Ärger", "日本語", "mixed Ünder", "Mixed ünder")
    for index, name in enumerate(names):
        db.add(
            User(
                name=name,
                email=f"script-{index}@example.test",
                password_hash="x",
                group=UserGroup.VIEWER,
            )
        )
    await db.commit()
    everything = list((await list_user_rows(db, limit=10_000)).rows)
    assert await whole_list(db, limit=1) == everything
    assert await whole_list(db, limit=3) == everything
    assert len({row.id for row in everything}) == len(everything)


async def test_a_cursor_for_a_user_that_is_gone_starts_the_list_again(
    db: AsyncSession, seed_data: SeedData
) -> None:
    first = await list_user_rows(db)
    assert (await list_user_rows(db, after=424242)).rows == first.rows


async def test_a_search_matches_part_of_the_name_or_the_email_without_wildcards(
    db: AsyncSession, seed_data: SeedData
) -> None:
    def names(page_rows: tuple[UserRow, ...]) -> set[str]:
        return {row.name for row in page_rows}

    by_name = (await list_user_rows(db, UserFilters(query="ADMIN"))).rows
    assert {"admin", "ungranted_admin", "project_admin"} <= names(by_name)
    assert "viewer" not in names(by_name) and "root" not in names(by_name)
    by_email = (await list_user_rows(db, UserFilters(query="@example.test"))).rows
    assert len(by_email) == len((await list_user_rows(db)).rows)
    underscore = (await list_user_rows(db, UserFilters(query="_"))).rows
    assert underscore and all("_" in row.name or "_" in row.email for row in underscore)
    assert "root" not in names(underscore)
    assert (await list_user_rows(db, UserFilters(query="%"))).rows == ()
    assert (await list_user_rows(db, UserFilters(query="no such person"))).rows == ()


async def test_the_status_filter_keeps_active_or_disabled_accounts_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    viewer = seed_data.users.viewer.id
    await db.execute(update(User).where(User.id == viewer).values(is_active=False))
    await db.commit()
    active = (await list_user_rows(db, UserFilters(status="active"))).rows
    disabled = (await list_user_rows(db, UserFilters(status="disabled"))).rows
    assert [row.name for row in disabled] == ["viewer"]
    assert active and all(row.is_active for row in active)
    total = (await list_user_rows(db)).rows
    assert len(active) + len(disabled) == len(total)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, UserFilters()),
        ({"q": "  John   Doe "}, UserFilters("John Doe")),
        ({"q": "x" * 300}, UserFilters("x" * 100)),
        ({"status": "active"}, UserFilters(status="active")),
        ({"status": "disabled", "q": "a"}, UserFilters("a", "disabled")),
        ({"status": "everything"}, UserFilters()),
        ({"status": "ACTIVE"}, UserFilters()),
    ],
)
def test_filters_clean_the_search_and_ignore_a_status_that_does_not_parse(
    params: dict[str, str], expected: UserFilters
) -> None:
    assert UserFilters.parse(params) == expected


def test_filters_round_trip_through_a_query_string_with_the_cursor() -> None:
    assert UserFilters().query_string() == "" and not UserFilters().active
    filters = UserFilters("a b&c", "disabled")
    assert filters.active
    assert filters.query_string() == "q=a+b%26c&status=disabled"
    assert filters.query_string(after=7) == "q=a+b%26c&status=disabled&after=7"
    assert UserFilters(status="active").query_string() == "status=active"


async def test_a_page_reads_the_users_and_their_reach_in_a_fixed_number_of_statements(
    seeded_database: AsyncEngine,
) -> None:
    async with create_session_factory(seeded_database)() as session:
        with count_statements(seeded_database) as small:
            first = await list_user_rows(session)
        await grow(session, projects=3, users=PAGE_SIZE * 2)
        with count_statements(seeded_database) as large:
            page = await list_user_rows(session)
        with count_statements(seeded_database) as next_page:
            await list_user_rows(session, after=page.next_after)
    assert first.next_after is None and page.next_after is not None
    assert len(small) == len(large) == 4
    assert len(next_page) == 5


async def test_a_token_is_active_revoked_expired_or_stopped_by_its_owner(
    db: AsyncSession, seed_data: SeedData
) -> None:
    token = (await db.execute(select(ApiToken))).scalars().first()
    assert token is not None
    owner = await db.get(User, seed_data.users.admin.id)
    assert owner is not None
    now = utc_now()
    assert token_row(token, owner, now=now).status is TokenStatus.ACTIVE

    owner.is_active = False
    stopped = token_row(token, owner, now=now)
    assert (stopped.status, stopped.reason) == (TokenStatus.STOPPED, "User disabled")
    owner.is_active = True
    owner.group = UserGroup.VIEWER
    stopped = token_row(token, owner, now=now)
    assert (stopped.status, stopped.reason) == (TokenStatus.STOPPED, "Viewers cannot use tokens")
    owner.group = UserGroup.ADMIN

    token.expires_at = now - timedelta(minutes=1)
    assert token_row(token, owner, now=now).status is TokenStatus.EXPIRED
    token.expires_at = now + timedelta(days=1)
    assert token_row(token, owner, now=now).status is TokenStatus.ACTIVE
    token.revoked_at = now
    owner.is_active = False
    revoked = token_row(token, owner, now=now)
    assert (revoked.status, revoked.reason) == (TokenStatus.REVOKED, None)
    assert not revoked.revocable


async def test_only_a_token_that_still_changes_something_can_be_revoked(
    db: AsyncSession, seed_data: SeedData
) -> None:
    token = (await db.execute(select(ApiToken))).scalars().first()
    assert token is not None
    owner = await db.get(User, seed_data.users.admin.id)
    assert owner is not None
    now = utc_now()
    assert token_row(token, owner, now=now).revocable
    owner.is_active = False
    assert token_row(token, owner, now=now).revocable
    owner.is_active = True
    token.expires_at = now - timedelta(minutes=1)
    assert not token_row(token, owner, now=now).revocable


async def test_the_detail_holds_grants_tokens_and_the_users_own_recent_entries(
    db: AsyncSession, seed_data: SeedData
) -> None:
    admin = seed_data.users.admin
    for index in range(ACTIVITY_LIMIT + 3):
        await record_audit(
            db,
            action=f"thing.{index:02d}",
            result=AuditResult.SUCCESS,
            actor_type=AuditActorType.USER,
            actor_user=admin,
            target_name=f"object-{index}",
        )
    await record_audit(
        db,
        action="user.disable",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=seed_data.users.superadmin,
        target_type="user",
        target_id=admin.id,
        target_name=admin.name,
    )
    await db.commit()

    detail = await load_user_detail(db, admin.id)
    assert detail is not None
    assert (detail.name, detail.group, detail.is_active) == ("admin", UserGroup.ADMIN, True)
    assert len(detail.grants) == 2
    assert [(token.name, token.status) for token in detail.tokens] == [
        ("billing-ci", TokenStatus.ACTIVE)
    ]
    assert len(detail.activity) == ACTIVITY_LIMIT
    assert [row.action for row in detail.activity][:2] == [
        f"thing.{ACTIVITY_LIMIT + 2:02d}",
        f"thing.{ACTIVITY_LIMIT + 1:02d}",
    ]
    assert "user.disable" not in {row.action for row in detail.activity}
    assert detail.activity[0].object_name == f"object-{ACTIVITY_LIMIT + 2}"


async def test_a_user_without_tokens_or_entries_has_empty_lists_and_an_unknown_id_is_none(
    db: AsyncSession, seed_data: SeedData
) -> None:
    detail = await load_user_detail(db, seed_data.users.ungranted_admin.id)
    assert detail is not None
    assert detail.tokens == () and detail.grants == frozenset()
    assert await load_user_detail(db, 424242) is None
