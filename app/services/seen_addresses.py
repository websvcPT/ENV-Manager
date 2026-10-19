"""The client addresses each user has signed in from, kept as keyed hashes.

A sign-in from an address the user has not used before can be announced. The record holds only
an HMAC of the address under a subkey of the master key, so the table neither shows the addresses
nor lets anyone test a guess without the key, and it keeps at most ``MAX_SEEN_ADDRESSES`` per
user, dropping the one not seen for longest. A user with no record yet is learning: their first
address is stored without being announced, since there is nothing to compare it with (and
databases that already have users have no record for any of them).
"""

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import UserSeenAddress

MAX_SEEN_ADDRESSES = 20
HASH_DOMAIN = "seen-address"


async def note_sign_in_address(
    db: AsyncSession,
    crypto: CryptoService,
    user_id: int,
    address: str | None,
    now: datetime,
) -> bool:
    """Record a sign-in address and say whether it is new for a user who has signed in before.

    The row is inserted with ``ON CONFLICT DO NOTHING``, so two sign-ins that arrive together
    from one new address announce it once. A known address only gets its last seen time moved.
    The caller commits. A sign-in without an address records nothing.
    """
    if not address:
        return False
    digest = crypto.keyed_hash(HASH_DOMAIN, address)
    known = (
        await db.execute(
            select(func.count())
            .select_from(UserSeenAddress)
            .where(UserSeenAddress.user_id == user_id)
        )
    ).scalar_one()
    inserted = await db.execute(
        insert(UserSeenAddress)
        .values(user_id=user_id, address_hash=digest, first_seen_at=now, last_seen_at=now)
        .on_conflict_do_nothing(index_elements=["user_id", "address_hash"])
    )
    if inserted.rowcount == 0:  # type: ignore[attr-defined]
        row = (
            await db.execute(
                select(UserSeenAddress).where(
                    UserSeenAddress.user_id == user_id, UserSeenAddress.address_hash == digest
                )
            )
        ).scalar_one()
        row.last_seen_at = now
        return False
    await _prune(db, user_id)
    return known > 0


async def _prune(db: AsyncSession, user_id: int) -> None:
    """Keep the ``MAX_SEEN_ADDRESSES`` addresses seen most recently and drop the rest."""
    keep = (
        select(UserSeenAddress.id)
        .where(UserSeenAddress.user_id == user_id)
        .order_by(UserSeenAddress.last_seen_at.desc(), UserSeenAddress.id.desc())
        .limit(MAX_SEEN_ADDRESSES)
    )
    await db.execute(
        delete(UserSeenAddress).where(
            UserSeenAddress.user_id == user_id, UserSeenAddress.id.not_in(keep)
        )
    )
