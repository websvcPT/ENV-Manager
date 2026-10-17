"""The version service: numbering, ciphertext at rest and the single insert path."""

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import EnvironmentVersion
from app.seeds import SeedData
from app.services.versions import create_version

APP_ROOT = Path(__file__).resolve().parents[1]


async def test_create_version_numbers_encrypts_and_hashes(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    environment = seed_data.envs.db
    async with create_session_factory(seeded_database)() as db:
        first = await create_version(
            db, crypto, environment.id, "NEW=1\n", author=seed_data.users.admin, comment="note"
        )
        second = await create_version(db, crypto, environment.id, "NEW=2\n", author=None)
        await db.commit()
    assert (first.number, second.number) == (3, 4)
    async with create_session_factory(seeded_database)() as db:
        rows = (
            (
                await db.execute(
                    select(EnvironmentVersion)
                    .where(EnvironmentVersion.environment_id == environment.id)
                    .order_by(EnvironmentVersion.number)
                )
            )
            .scalars()
            .all()
        )
    assert [row.number for row in rows] == [1, 2, 3, 4]
    saved = rows[2]
    assert "NEW=1" not in saved.content_encrypted
    assert crypto.decrypt_text(saved.content_encrypted) == "NEW=1\n"
    assert saved.content_hash == crypto.content_hash("NEW=1\n")
    assert saved.author_id == seed_data.users.admin.id and saved.comment == "note"
    assert rows[3].author_id is None


def test_only_the_version_service_inserts_versions() -> None:
    inserting = re.compile(r"^(?!\s*class ).*\bEnvironmentVersion\(", re.MULTILINE)
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in APP_ROOT.rglob("*.py")
        if "tests" not in path.relative_to(APP_ROOT).parts
        and path.name != "versions.py"
        and inserting.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
