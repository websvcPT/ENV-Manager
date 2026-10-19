"""Tests for the record of addresses a user has signed in from: keyed hashes, bounded in size."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_session_factory
from app.models import User, UserGroup, UserSeenAddress
from app.seeds import SeedData
from app.services.seen_addresses import MAX_SEEN_ADDRESSES, note_sign_in_address

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


async def note(
    engine: AsyncEngine,
    crypto: CryptoService,
    user_id: int,
    address: str | None,
    minutes: int = 0,
) -> bool:
    """Record a sign-in on its own session and commit."""
    async with create_session_factory(engine)() as db:
        found = await note_sign_in_address(
            db, crypto, user_id, address, NOW + timedelta(minutes=minutes)
        )
        await db.commit()
        return found


async def rows(engine: AsyncEngine, user_id: int) -> list[UserSeenAddress]:
    """The stored records of a user, oldest first."""
    async with create_session_factory(engine)() as db:
        return list(
            (
                await db.execute(
                    select(UserSeenAddress)
                    .where(UserSeenAddress.user_id == user_id)
                    .order_by(UserSeenAddress.id)
                )
            ).scalars()
        )


async def test_the_first_address_is_learned_silently_and_the_next_new_one_is_announced(
    seeded_database: AsyncEngine, crypto: CryptoService, seed_data: SeedData
) -> None:
    """With no record yet there is nothing to compare, so only a later new address is new."""
    user = seed_data.users.admin.id
    assert not await note(seeded_database, crypto, user, "203.0.113.1")
    assert not await note(seeded_database, crypto, user, "203.0.113.1", 5)
    assert await note(seeded_database, crypto, user, "203.0.113.2", 10)
    assert not await note(seeded_database, crypto, user, "203.0.113.2", 15)
    assert not await note(seeded_database, crypto, user, "203.0.113.1", 20)
    assert len(await rows(seeded_database, user)) == 2


async def test_the_record_is_kept_per_user(
    seeded_database: AsyncEngine, crypto: CryptoService, seed_data: SeedData
) -> None:
    """An address one user has used is still new for another."""
    first, second = seed_data.users.admin.id, seed_data.users.viewer.id
    await note(seeded_database, crypto, first, "203.0.113.1")
    await note(seeded_database, crypto, second, "198.51.100.9")
    assert await note(seeded_database, crypto, second, "203.0.113.1", 5)


async def test_addresses_are_stored_only_as_keyed_hashes(
    seeded_database: AsyncEngine, crypto: CryptoService, seed_data: SeedData
) -> None:
    """The row holds a hex HMAC, not the address, and another key gives another hash."""
    user = seed_data.users.admin.id
    await note(seeded_database, crypto, user, "203.0.113.77")
    (row,) = await rows(seeded_database, user)
    assert row.address_hash == crypto.keyed_hash("seen-address", "203.0.113.77")
    assert len(row.address_hash) == 64 and "203.0.113.77" not in row.address_hash
    other = CryptoService(parse_master_key(generate_master_key()))
    assert other.keyed_hash("seen-address", "203.0.113.77") != row.address_hash
    assert crypto.content_hash("203.0.113.77") != row.address_hash
    async with create_session_factory(seeded_database)() as db:
        text = repr((await db.execute(select(UserSeenAddress.__table__))).all())
    assert "203.0.113.77" not in text


async def test_the_record_keeps_only_the_most_recent_addresses(
    seeded_database: AsyncEngine, crypto: CryptoService, seed_data: SeedData
) -> None:
    """Past the limit the address not seen for longest is dropped, and a refreshed one stays."""
    user = seed_data.users.admin.id
    await note(seeded_database, crypto, user, "10.0.0.0")
    for number in range(1, MAX_SEEN_ADDRESSES):
        await note(seeded_database, crypto, user, f"10.0.0.{number}", number)
    assert len(await rows(seeded_database, user)) == MAX_SEEN_ADDRESSES
    await note(seeded_database, crypto, user, "10.0.0.0", 100)
    assert await note(seeded_database, crypto, user, "10.0.1.0", 101)
    stored = {row.address_hash for row in await rows(seeded_database, user)}
    assert len(stored) == MAX_SEEN_ADDRESSES
    assert crypto.keyed_hash("seen-address", "10.0.0.0") in stored
    assert crypto.keyed_hash("seen-address", "10.0.0.1") not in stored
    assert crypto.keyed_hash("seen-address", "10.0.1.0") in stored


async def test_a_sign_in_without_an_address_records_and_announces_nothing(
    seeded_database: AsyncEngine, crypto: CryptoService, seed_data: SeedData
) -> None:
    """No client address means nothing to compare or store."""
    user = seed_data.users.admin.id
    assert not await note(seeded_database, crypto, user, None)
    assert not await note(seeded_database, crypto, user, "")
    assert await rows(seeded_database, user) == []


async def test_two_sign_ins_from_one_new_address_announce_it_once(
    seeded_database: AsyncEngine, crypto: CryptoService, seed_data: SeedData
) -> None:
    """The unique row decides, so the second of two together finds the address already known."""
    user = seed_data.users.admin.id
    await note(seeded_database, crypto, user, "203.0.113.1")
    factory = create_session_factory(seeded_database)
    async with factory() as first, factory() as second:
        one = await note_sign_in_address(first, crypto, user, "203.0.113.2", NOW)
        await first.commit()
        two = await note_sign_in_address(second, crypto, user, "203.0.113.2", NOW)
        await second.commit()
    assert (one, two) == (True, False)


async def test_deleting_a_user_deletes_their_record(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """The table follows the user, so no address outlives the account."""
    async with create_session_factory(seeded_database)() as db:
        user = User(
            name="temp",
            email="t@example.test",
            password_hash="x",
            group=UserGroup.ADMIN,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        user_id = user.id
    await note(seeded_database, crypto, user_id, "203.0.113.1")
    async with create_session_factory(seeded_database)() as db:
        gone = await db.get(User, user_id)
        assert gone is not None
        await db.delete(gone)
        await db.commit()
    assert await rows(seeded_database, user_id) == []
