"""The account sessions list: readable devices, live sessions only, revoking one's own others."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import AuditEntry, UserSession
from app.models.types import utc_now
from app.services.account_sessions import (
    CurrentSessionError,
    SessionNotFoundError,
    describe_device,
    list_sessions,
    revoke_all_other_sessions,
    revoke_other_session,
)
from app.services.auth_policy import AuthPolicy
from app.services.sessions import create_session
from app.tests.auth_helpers import make_user

POLICY = AuthPolicy()
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
EDGE_WINDOWS = CHROME_WINDOWS + " Edg/130.0.0.0"
CHROME_IPHONE = SAFARI_IPHONE.replace("Version/17.5", "CriOS/130.0.0.0")
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)


@pytest.mark.parametrize(
    ("agent", "device"),
    [
        (FIREFOX_LINUX, "Firefox on Linux"),
        (SAFARI_IPHONE, "Safari on iPhone"),
        (CHROME_WINDOWS, "Chrome on Windows"),
        (EDGE_WINDOWS, "Edge on Windows"),
        (CHROME_IPHONE, "Chrome on iPhone"),
        (SAFARI_MAC, "Safari on macOS"),
        (CHROME_ANDROID, "Chrome on Android"),
        ("Opera/9.80 (Windows NT 6.1) Presto/2.12 Version/12.16", "Opera on Windows"),
        ("curl/8.5.0", "Unknown device"),
        ("python-httpx/0.28.1", "Unknown device"),
        ("", "Unknown device"),
        (None, "Unknown device"),
        ("Mozilla/5.0 (X11; Linux x86_64)", "Linux"),
    ],
)
def test_the_device_is_named_from_the_user_agent(agent: str | None, device: str) -> None:
    assert describe_device(agent) == device


def test_the_raw_user_agent_is_never_part_of_the_name() -> None:
    assert "Mozilla" not in describe_device(FIREFOX_LINUX)


async def test_the_list_has_the_current_session_first_then_the_newest_and_no_dead_ones(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    now = utc_now()
    async with factory() as db:
        user, other = make_user("alice"), make_user("bob")
        db.add_all([user, other])
        await db.flush()
        ids: dict[str, int] = {}
        for label, agent, seen in (
            ("old", FIREFOX_LINUX, now - timedelta(minutes=20)),
            ("current", CHROME_WINDOWS, now - timedelta(minutes=5)),
            ("new", SAFARI_IPHONE, now - timedelta(minutes=1)),
            ("idle", SAFARI_MAC, now - timedelta(hours=3)),
        ):
            await create_session(
                db, user, POLICY, user_agent=agent, ip_address="10.0.0.1", now=seen
            )
            ids[label] = (await db.execute(select(func.max(UserSession.id)))).scalar_one()
        expired = await create_session(db, user, POLICY, now=now - timedelta(hours=20))
        assert expired
        await create_session(db, other, POLICY, user_agent=CHROME_ANDROID, now=now)
        await db.commit()
        rows = await list_sessions(db, user, ids["current"], POLICY, now=now)
    assert [row.id for row in rows] == [ids["current"], ids["new"], ids["old"]]
    assert [row.current for row in rows] == [True, False, False]
    assert rows[0].device == "Chrome on Windows" and rows[0].ip_address == "10.0.0.1"
    assert rows[0].created_at < rows[0].last_seen_at + timedelta(seconds=1)


async def test_a_session_of_the_user_can_be_revoked_and_the_entry_names_device_and_address(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        for agent, address in ((CHROME_WINDOWS, "10.0.0.1"), (SAFARI_IPHONE, "10.0.0.2")):
            await create_session(db, user, POLICY, user_agent=agent, ip_address=address)
        await db.commit()
        current, other = (
            (await db.execute(select(UserSession.id).order_by(UserSession.id))).scalars().all()
        )
        row = await revoke_other_session(db, user, current, other, ip_address="10.0.0.1")
        assert (row.device, row.ip_address) == ("Safari on iPhone", "10.0.0.2")
        left = (await db.execute(select(UserSession.id))).scalars().all()
        entry = (await db.execute(select(AuditEntry))).scalar_one()
    assert left == [current]
    assert entry.action == "account.session_revoke" and entry.target_id == user.id
    assert entry.details == {"device": "Safari on iPhone", "session_ip": "10.0.0.2"}


async def test_the_current_session_and_other_peoples_sessions_cannot_be_revoked(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        user, other = make_user("alice"), make_user("bob")
        db.add_all([user, other])
        await db.flush()
        await create_session(db, user, POLICY)
        await create_session(db, other, POLICY)
        await db.commit()
        mine, theirs = (
            (await db.execute(select(UserSession.id).order_by(UserSession.id))).scalars().all()
        )
        with pytest.raises(CurrentSessionError):
            await revoke_other_session(db, user, mine, mine)
        with pytest.raises(SessionNotFoundError):
            await revoke_other_session(db, user, mine, theirs)
        with pytest.raises(SessionNotFoundError):
            await revoke_other_session(db, user, mine, 9999)
        assert len((await db.execute(select(UserSession.id))).scalars().all()) == 2
        assert (await db.execute(select(AuditEntry))).scalars().all() == []


async def test_signing_out_the_others_keeps_the_current_session_and_other_users(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        user, other = make_user("alice"), make_user("bob")
        db.add_all([user, other])
        await db.flush()
        for owner in (user, user, user, other):
            await create_session(db, owner, POLICY)
        await db.commit()
        ids = (await db.execute(select(UserSession.id).order_by(UserSession.id))).scalars().all()
        ended = await revoke_all_other_sessions(db, user, ids[0], ip_address="10.0.0.1")
        left = (await db.execute(select(UserSession.id))).scalars().all()
        entry = (await db.execute(select(AuditEntry))).scalar_one()
    assert ended == 2 and left == [ids[0], ids[3]]
    assert entry.action == "account.sessions_revoke_others"
    assert entry.details == {"sessions_ended": 2}
