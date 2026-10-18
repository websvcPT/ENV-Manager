"""The read models of the Users screens: list rows, token statuses and the detail."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.models import ApiToken, AuditActorType, AuditResult, User, UserGroup
from app.models.types import utc_now
from app.seeds import SeedData
from app.services.audit import record_audit
from app.services.tokens import TokenStatus
from app.services.user_detail import ACTIVITY_LIMIT, list_user_rows, load_user_detail, token_row


@pytest.fixture
async def db(seeded_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the seeded temporary database."""
    async with create_session_factory(seeded_database)() as session:
        yield session


async def test_rows_are_ordered_by_name_and_count_the_projects_the_grants_reach(
    db: AsyncSession, seed_data: SeedData
) -> None:
    rows = await list_user_rows(db)
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
    row = next(row for row in await list_user_rows(db) if row.name == "viewer")
    assert not row.is_active and row.projects == 1


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
