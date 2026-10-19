"""The signed-in user's own sessions: the list with a readable device, and revoking them.

A user only ever sees and ends their own sessions. A session id of someone else answers like a
missing one, and the session making the request cannot be ended here (Log out does that).
"""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditResult, User, UserSession
from app.models.types import utc_now
from app.services.audit import AuditActor, record_actor_audit
from app.services.auth_policy import AuthPolicy
from app.services.sessions import revoke_user_sessions

UNKNOWN_DEVICE = "Unknown device"

# Checked in order: the first pattern found wins, so Edge and Opera (which also say Chrome) and
# Chrome (which also says Safari) come before the browsers they imitate.
_BROWSERS = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Firefox", re.compile(r"Firefox/|FxiOS/")),
    ("Chrome", re.compile(r"Chrome/|CriOS/")),
    ("Safari", re.compile(r"Safari/")),
)
_SYSTEMS = (
    ("iPhone", re.compile(r"iPhone")),
    ("iPad", re.compile(r"iPad")),
    ("Android", re.compile(r"Android")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("ChromeOS", re.compile(r"CrOS")),
    ("Linux", re.compile(r"Linux|X11")),
)


class SessionNotFoundError(Exception):
    """The session does not exist or belongs to someone else."""


class CurrentSessionError(Exception):
    """The session is the one making the request, which Log out ends."""


@dataclass(frozen=True)
class SessionRow:
    """One live session as the account page lists it."""

    id: int
    device: str
    ip_address: str | None
    created_at: datetime
    last_seen_at: datetime
    current: bool


def describe_device(user_agent: str | None) -> str:
    """A short "Browser on System" name from a User-Agent header, best effort.

    Only what is recognised is named; anything else reads as an unknown device, and the raw
    header is never shown.
    """
    if not user_agent:
        return UNKNOWN_DEVICE
    browser = next((name for name, pattern in _BROWSERS if pattern.search(user_agent)), None)
    system = next((name for name, pattern in _SYSTEMS if pattern.search(user_agent)), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or UNKNOWN_DEVICE


def to_row(session: UserSession, current_id: int) -> SessionRow:
    """The listed form of a stored session."""
    return SessionRow(
        id=session.id,
        device=describe_device(session.user_agent),
        ip_address=session.ip_address,
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        current=session.id == current_id,
    )


async def list_sessions(
    db: AsyncSession,
    user: User,
    current_id: int,
    policy: AuthPolicy,
    *,
    now: datetime | None = None,
) -> list[SessionRow]:
    """The user's live sessions, the current one first, then the most recently active.

    A session past its absolute expiry or idle timeout is dead even when nothing has deleted its
    row yet, so it is left out.
    """
    moment = now or utc_now()
    stored = (
        await db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.expires_at > moment,
                UserSession.last_seen_at > moment - policy.idle_timeout,
            )
        )
    ).scalars()
    rows = [to_row(session, current_id) for session in stored]
    return sorted(rows, key=lambda row: (not row.current, -row.last_seen_at.timestamp(), row.id))


async def find_other_session(
    db: AsyncSession, user: User, current_id: int, session_id: int
) -> UserSession:
    """The user's session with this id, refusing the current one and anyone else's."""
    session = await db.get(UserSession, session_id)
    if session is None or session.user_id != user.id:
        raise SessionNotFoundError
    if session.id == current_id:
        raise CurrentSessionError
    return session


async def revoke_other_session(
    db: AsyncSession,
    user: User,
    current_id: int,
    session_id: int,
    *,
    ip_address: str | None = None,
) -> SessionRow:
    """End one of the user's other sessions, audited, and return what was listed for it."""
    session = await find_other_session(db, user, current_id, session_id)
    row = to_row(session, current_id)
    await db.delete(session)
    await record_actor_audit(
        db,
        AuditActor.for_user(user, ip_address),
        action="account.session_revoke",
        result=AuditResult.SUCCESS,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"device": row.device, "session_ip": row.ip_address},
    )
    await db.commit()
    return row


async def revoke_all_other_sessions(
    db: AsyncSession, user: User, current_id: int, *, ip_address: str | None = None
) -> int:
    """End every session of the user but the current one, audited. Returns how many ended."""
    ended = await revoke_user_sessions(db, user.id, keep_session_id=current_id)
    await record_actor_audit(
        db,
        AuditActor.for_user(user, ip_address),
        action="account.sessions_revoke_others",
        result=AuditResult.SUCCESS,
        target_type="user",
        target_id=user.id,
        target_name=user.name,
        details={"sessions_ended": ended},
    )
    await db.commit()
    return ended
