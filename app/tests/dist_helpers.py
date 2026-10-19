"""Helpers shared by the dist check tests: seeded deployments moved onto a real host folder.

The seed places deployments under paths that do not exist in the test container. A dist check
reads a real file, so these helpers move the deployments below a temporary folder, make that
folder the only allowed path and write dist files there.
"""

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AllowedPath, Deployment, DistMissingPolicy, Environment, User
from app.seeds import SeedData


async def relocate_deployments(db: AsyncSession, host: Path) -> dict[str, Path]:
    """Move the seeded deployments below ``host`` and allow ``host`` as the only path.

    Returns the real root of each deployment by name, keyed ``prod``, ``staging`` and ``main``;
    every root has an ``environments`` folder. The legacy deployment keeps its outside path.
    """
    real_host = host.resolve()
    roots = {name: real_host / name for name in ("prod", "staging", "main")}
    for root in roots.values():
        (root / "environments").mkdir(parents=True)
    for deployment in (await db.execute(select(Deployment))).scalars():
        if deployment.name in roots:
            deployment.root_path = str(roots[deployment.name])
    owner = (await db.execute(select(User).where(User.name == "root"))).scalar_one()
    await db.execute(delete(AllowedPath))
    db.add(AllowedPath(path=str(real_host), created_by_id=owner.id))
    await db.commit()
    return roots


async def link_dist(
    db: AsyncSession,
    environment: Environment,
    dist_path: str | None,
    *,
    policy: DistMissingPolicy | None = None,
) -> None:
    """Point an environment at a dist file and optionally give it its own missing-keys policy."""
    stored = await db.get(Environment, environment.id)
    assert stored is not None
    stored.dist_path = dist_path
    stored.dist_missing_policy = policy
    await db.commit()


async def set_deployment_policy(
    db: AsyncSession, seed_data: SeedData, name: str, policy: DistMissingPolicy
) -> None:
    """Set the missing-keys policy of the seeded deployment with this name."""
    deployments = {
        "prod": seed_data.deployments.fancy_prod,
        "staging": seed_data.deployments.fancy_staging,
        "main": seed_data.deployments.billing_main,
    }
    stored = await db.get(Deployment, deployments[name].id)
    assert stored is not None
    stored.dist_missing_policy = policy
    await db.commit()


def write_dist(root: Path, relative: str, text: str) -> None:
    """Write a dist file below a deployment root."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
