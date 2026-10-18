"""The content this app last wrote to a target, which drift is measured against.

The deploy engine and the drift view both read it here, so they cannot disagree about what a
target held after the last deploy. Dry runs and failed attempts never set it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeployResult, DeployResultStatus, DeployRun

_DEPLOYED = (DeployResultStatus.WRITTEN, DeployResultStatus.UNCHANGED)


@dataclass(frozen=True)
class Baseline:
    """The newest real result that wrote a target or found it identical.

    ``version_id`` is the version behind ``written_hash`` (None when the version row is gone)
    and ``written_at`` is when that run started.
    """

    written_hash: str
    version_id: int | None
    written_at: datetime


async def load_baselines(db: AsyncSession, environment_ids: Sequence[int]) -> dict[int, Baseline]:
    """The baseline of each environment that has one, by environment id."""
    rows = await db.execute(
        select(
            DeployResult.environment_id,
            DeployResult.written_hash,
            DeployResult.version_id,
            DeployRun.started_at,
        )
        .join(DeployRun, DeployRun.id == DeployResult.run_id)
        .where(
            DeployResult.environment_id.in_(environment_ids),
            DeployRun.dry_run.is_(False),
            DeployResult.status.in_(_DEPLOYED),
            DeployResult.written_hash.is_not(None),
        )
        .order_by(DeployResult.id)
    )
    return {
        environment_id: Baseline(written_hash, version_id, started_at)
        for environment_id, written_hash, version_id, started_at in rows.all()
        if environment_id is not None and written_hash is not None
    }
