"""A changed target path against the demo stack: real files, real containers.

The web application runs in this process on the throwaway database of the ``world`` fixture and
the host Docker daemon; a signed-in user does what the environment settings page does. Every
check is on the real files: the file at the old path is left alone, the new path counts as never
deployed, and the next deploy creates the file or halts on what is already there.
"""

import stat

import httpx

from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.helpers import MANAGED, snapshot
from app.tests.conftest import sign_in
from app.tests.web_helpers import post

NEW_PATH = "environments/.env.db.relocated"
NEW_NAME = ".env.db.relocated"


def chosen(*environments: int) -> dict[str, object]:
    """The fields the deploy dialog posts for a choice of files, at their newest version."""
    return {"selection": "explicit", "environment": list(environments)}


def settings(**overrides: str) -> dict[str, str]:
    """The settings form of ``.env.db`` as posted, with its target moved to the new path."""
    base = {
        "target_path": NEW_PATH,
        "dist_path": "environments/.env.db-dist",
        "file_mode": "",
        "file_owner": "",
        "file_group": "",
        "dist_missing_policy": "",
        "drift_policy": "",
        "missing_target_policy": "",
    }
    return {**base, **overrides}


async def deploy_once_then_ask_to_move(
    demo: DemoStack, world: World, web: httpx.AsyncClient
) -> bytes:
    """Deploy ``.env.db`` so the app has written it, then post the move without the confirmation.

    Returns the content of the file at the old path, which nothing may change afterwards.
    """
    await sign_in(web, "root")
    env_db = world.demo_seed.env_db.id
    first = await post(web, f"/services/{world.demo_seed.db.id}/deploy", chosen(env_db))
    assert 'data-status="unchanged"' in first.text
    asked = await post(web, f"/environments/{env_db}/settings", settings())
    assert asked.status_code == 422
    assert "Confirm that the file counts as not deployed at the new path" in asked.text
    return (demo.targets / ".env.db").read_bytes()


async def test_the_move_leaves_the_old_file_alone_and_the_next_deploy_creates_the_new_one(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    env_db = world.demo_seed.env_db.id
    service = world.demo_seed.db.id
    old_content = await deploy_once_then_ask_to_move(demo, world, web)
    assert not (demo.targets / NEW_NAME).exists()
    before = snapshot(demo.targets)

    moved = await post(web, f"/environments/{env_db}/settings", settings(acknowledge_move="1"))
    assert moved.headers["HX-Redirect"] == f"/environments/{env_db}?updated=true"
    assert snapshot(demo.targets) == before
    page = (await web.get(f"/environments/{env_db}")).text
    assert "never deployed" in page and NEW_PATH in page

    done = await post(web, f"/services/{service}/deploy", chosen(env_db))
    assert 'data-status="success"' in done.text or 'data-status="written"' in done.text
    created = demo.targets / NEW_NAME
    assert created.read_text(encoding="utf-8") == MANAGED[".env.db"]
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert (demo.targets / ".env.db").read_bytes() == old_content
    assert not [entry for entry in demo.targets.iterdir() if ".bak-" in entry.name]


async def test_a_file_already_at_the_new_path_halts_the_deploy_as_never_deployed(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    env_db = world.demo_seed.env_db.id
    service = world.demo_seed.db.id
    await deploy_once_then_ask_to_move(demo, world, web)
    stranger = "SOMEONE_ELSE=wrote-this\n"
    (demo.targets / NEW_NAME).write_text(stranger, encoding="utf-8")
    moved = await post(web, f"/environments/{env_db}/settings", settings(acknowledge_move="1"))
    assert moved.status_code == 200
    before = snapshot(demo.targets)

    halted = await post(web, f"/services/{service}/deploy", chosen(env_db))
    assert f'href="/environments/{env_db}/drift"' in halted.text
    assert (demo.targets / NEW_NAME).read_text(encoding="utf-8") == stranger
    assert snapshot(demo.targets) == before
    drift = (await web.get(f"/environments/{env_db}/drift")).text
    assert 'data-status="never"' in drift and "was never deployed by the app" in drift
    assert workloads.container_ids("db") == []
