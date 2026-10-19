"""Deploys with an owner and group, run as root in a throwaway container of the demo stack.

The production image runs as root so a deploy can chown what it writes. These scenarios need that
privilege, so ``make e2e-demo`` runs this file alone in its own container started with
``--user 0`` and leaves it out of the run on the development stack (whose container user is the
host user and which keeps the "owner denied" scenario in ``test_demo_deploy.py``).
"""

import grp
import os
import pwd
import stat

from sqlalchemy import select

from app.models import DeployResult, DeployResultStatus, Environment
from app.real_scenarios.conftest import DemoStack, World
from app.real_scenarios.helpers import MANAGED, cli_runs, output

OWNER = "nobody"
GROUP = "nogroup"


async def set_owner_and_group(world: World, owner: str, group: str) -> None:
    """Give the demo api file an owner and a group."""
    async with world.session() as db:
        environment = await db.get(Environment, world.demo_seed.env_api.id)
        assert environment is not None
        environment.file_owner = owner
        environment.file_group = group
        await db.commit()


async def test_a_deploy_applies_the_owner_and_group_and_keeps_the_mode(
    demo: DemoStack, world: World
) -> None:
    assert os.geteuid() == 0, "this scenario runs in the root container of make e2e-demo"
    (demo.targets / ".env.api").unlink()
    await set_owner_and_group(world, OWNER, GROUP)

    result = world.deploy("api", "--env", ".env.api")
    assert result.returncode == 0, output(result)

    target = demo.targets / ".env.api"
    info = target.stat()
    assert info.st_uid == pwd.getpwnam(OWNER).pw_uid
    assert info.st_gid == grp.getgrnam(GROUP).gr_gid
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == MANAGED[".env.api"]

    async with world.session() as db:
        (run,) = await cli_runs(db)
        rows = await db.execute(select(DeployResult).where(DeployResult.run_id == run.id))
        (written,) = rows.scalars()
        assert written.status is DeployResultStatus.WRITTEN


async def test_a_root_owned_target_is_replaced_and_handed_to_the_configured_account(
    demo: DemoStack, world: World
) -> None:
    target = demo.targets / ".env.api"
    target.unlink()
    target.write_text(MANAGED[".env.api"].replace("DEMO", "OLD", 1), encoding="utf-8")
    os.chown(target, 0, 0)
    target.chmod(0o600)
    await set_owner_and_group(world, OWNER, GROUP)

    result = world.deploy("api", "--env", ".env.api", "--force")
    assert result.returncode == 0, output(result)

    info = target.stat()
    assert (info.st_uid, info.st_gid) == (
        pwd.getpwnam(OWNER).pw_uid,
        grp.getgrnam(GROUP).gr_gid,
    )
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == MANAGED[".env.api"]
