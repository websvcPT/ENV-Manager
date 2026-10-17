"""Environment versions: the only place that inserts saved content.

Content is stored as ciphertext with its keyed content hash; saved versions are never updated.
The caller owns the transaction and writes the audit entry.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import EnvironmentVersion, User


async def create_version(
    db: AsyncSession,
    crypto: CryptoService,
    environment_id: int,
    content: str,
    *,
    author: User | None,
    comment: str | None = None,
) -> EnvironmentVersion:
    """Save ``content`` as the next version of an environment and return the flushed row."""
    latest = await db.scalar(
        select(func.max(EnvironmentVersion.number)).where(
            EnvironmentVersion.environment_id == environment_id
        )
    )
    version = EnvironmentVersion(
        environment_id=environment_id,
        number=(latest or 0) + 1,
        content_encrypted=crypto.encrypt(content),
        content_hash=crypto.content_hash(content),
        comment=comment,
        author_id=author.id if author else None,
    )
    db.add(version)
    await db.flush()
    return version
