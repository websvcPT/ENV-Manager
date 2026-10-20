"""The drift view against the demo stack: real files, real containers.

The web application runs in this process on the throwaway database of the ``world`` fixture and
the host Docker daemon; a signed-in user does what the drift page does. Every check is on the real
files (content, mode, backups, what was not touched) and on the demo containers.
"""

import re
import stat
import time

import httpx
from sqlalchemy import func, select

from app.models import AuditEntry, EnvironmentVersion
from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.helpers import MANAGED, snapshot
from app.tests.conftest import sign_in
from app.tests.web_helpers import hx_vals, post, without_location

EDITED_KEY = "DEMO_DB_EDITED_ON_DISK=1\n"
REAL_VALUES = ("demo-sqlite", "5433", "demo-token-not-a-secret", "demo-password-not-a-secret")


def chosen(*environments: int) -> dict[str, object]:
    """The fields the deploy dialog posts for a choice of files, at their newest version."""
    return {"selection": "explicit", "environment": list(environments)}


def badges(page: str) -> dict[str, str]:
    """The outcome badge of each pre-flight row by file name."""
    rows = re.findall(
        r'<label data-fragment="preflight-row" data-env="([^"]+)"(.*?)</label>', page, re.DOTALL
    )
    return {name: re.findall(r'data-status="(\w+)"', body)[-1] for name, body in rows}


async def version_count(world: World, environment: int) -> int:
    """How many versions an environment has."""
    async with world.session() as db:
        return (
            await db.scalar(
                select(func.count())
                .select_from(EnvironmentVersion)
                .where(EnvironmentVersion.environment_id == environment)
            )
            or 0
        )


async def drifted_db_file(demo: DemoStack, world: World, web: httpx.AsyncClient) -> str:
    """Deploy ``.env.db`` once so the app has written it, then edit it on disk.

    Returns the edited content. The deploy only finds the identical file, so no container is
    started; the next deploy of the file halts on drift.
    """
    await sign_in(web, "root")
    service = world.demo_seed.db.id
    first = await post(web, f"/services/{service}/deploy", chosen(world.demo_seed.env_db.id))
    assert 'data-status="unchanged"' in first.text
    edited = MANAGED[".env.db"] + EDITED_KEY
    (demo.targets / ".env.db").write_text(edited, encoding="utf-8")
    halted = await post(web, f"/services/{service}/deploy", chosen(world.demo_seed.env_db.id))
    assert f'href="/environments/{world.demo_seed.env_db.id}/drift"' in halted.text
    return edited


async def test_the_view_masks_the_edited_file_and_reveal_audits_all_three_columns(
    demo: DemoStack, world: World, web: httpx.AsyncClient
) -> None:
    env_db = world.demo_seed.env_db.id
    edited = await drifted_db_file(demo, world, web)

    page = (await web.get(f"/environments/{env_db}/drift")).text
    assert 'data-status="drift"' in page and "DEMO_DB_EDITED_ON_DISK" in page
    assert "Added on disk" in page and "3 unchanged keys hidden" in page
    assert not any(value in page for value in REAL_VALUES)
    assert str(demo.base) not in without_location(page)

    values = hx_vals(page, f"/environments/{env_db}/drift/reveal")
    answer = await post(web, f"/environments/{env_db}/drift/reveal", values)
    assert answer.status_code == 200 and answer.headers["Cache-Control"] == "no-store"
    assert "<span hx-ignore data-reveal-value>1</span>" in answer.text
    assert "DEMO_DB_EDITED_ON_DISK" not in answer.text
    async with world.session() as db:
        entries = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.reveal")))
            .scalars()
            .all()
        )
    assert {entry.details["column"] for entry in entries} == {"target"}
    assert not any(value in str(entry.details) for entry in entries for value in REAL_VALUES)
    assert (demo.targets / ".env.db").read_text(encoding="utf-8") == edited


async def test_importing_the_edited_file_makes_the_next_deploy_clean(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    env_db = world.demo_seed.env_db.id
    service = world.demo_seed.db.id
    edited = await drifted_db_file(demo, world, web)
    target = demo.targets / ".env.db"
    before = await version_count(world, env_db)
    names_before = {entry.name for entry in demo.targets.iterdir()}

    page = (await web.get(f"/environments/{env_db}/drift")).text
    imported = await post(
        web, f"/environments/{env_db}/import", hx_vals(page, f"/environments/{env_db}/import")
    )
    assert imported.headers["HX-Redirect"] == f"/environments/{env_db}/drift?imported={before + 1}"
    assert await version_count(world, env_db) == before + 1
    assert target.read_text(encoding="utf-8") == edited
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert {entry.name for entry in demo.targets.iterdir()} == names_before
    after = (await web.get(imported.headers["HX-Redirect"])).text
    assert ">No drift<" in after and "Imported the target as version" in after

    preflight = (await post(web, f"/services/{service}/deploy/preflight", chosen(env_db))).text
    assert badges(preflight)[".env.db"] == "unchanged" and "Drift: None" in preflight
    done = await post(web, f"/services/{service}/deploy", chosen(env_db))
    assert 'data-status="unchanged"' in done.text and "Halted" not in done.text
    assert target.read_text(encoding="utf-8") == edited
    assert {entry.name for entry in demo.targets.iterdir()} == names_before
    assert workloads.container_ids("db") == []
    async with world.session() as db:
        (entry,) = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.import")))
            .scalars()
            .all()
        )
    assert entry.details == {"number": before + 1, "source": "target"}


async def test_overwriting_the_edited_file_asks_first_and_keeps_a_0600_backup(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    env_db = world.demo_seed.env_db.id
    edited = await drifted_db_file(demo, world, web)
    target = demo.targets / ".env.db"
    page = (await web.get(f"/environments/{env_db}/drift")).text
    managed = int(str(hx_vals(page, f"/environments/{env_db}/import")["base_version"]))

    before = snapshot(demo.targets)
    asked = await post(web, f"/environments/{env_db}/overwrite", {"base_version": managed})
    assert asked.status_code == 422 and "Confirm the overwrite to continue." in asked.text
    assert snapshot(demo.targets) == before and workloads.container_ids("db") == []

    since = time.time()
    done = await post(
        web,
        f"/environments/{env_db}/overwrite",
        {"base_version": managed, "confirm_overwrite": "1"},
    )
    assert done.headers["HX-Redirect"] == (
        f"/environments/{env_db}/drift?overwritten={managed}&backup=1&same=0&post=ok"
    )
    assert target.read_text(encoding="utf-8") == MANAGED[".env.db"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    (backup,) = [entry for entry in demo.targets.iterdir() if entry.name.startswith(".env.db.bak-")]
    assert backup.read_text(encoding="utf-8") == edited
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    untouched = {name: info for name, info in before.items() if name != ".env.db"}
    assert {name: info for name, info in snapshot(demo.targets).items() if name in untouched} == (
        untouched
    )
    assert workloads.created_since("db", since) == 1
    after = (await web.get(done.headers["HX-Redirect"])).text
    assert ">No drift<" in after and "Overwrote the target with version" in after
    assert "The previous file was kept as a backup next to it." in after


async def test_the_api_file_that_was_never_deployed_is_imported_and_then_deploys_unchanged(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    await sign_in(web, "root")
    env_api = world.demo_seed.env_api.id
    api = world.demo_seed.api.id
    target = demo.targets / ".env.api"
    drifted = target.read_text(encoding="utf-8")
    page = (await web.get(f"/environments/{env_api}/drift")).text
    assert 'data-status="never"' in page and "was never deployed by the app" in page
    assert not any(value in page for value in REAL_VALUES)
    assert "DEMO_API_MODE" in page

    imported = await post(
        web, f"/environments/{env_api}/import", hx_vals(page, f"/environments/{env_api}/import")
    )
    assert "imported=" in imported.headers["HX-Redirect"]
    assert target.read_text(encoding="utf-8") == drifted
    done = await post(web, f"/services/{api}/deploy", chosen(env_api))
    assert 'data-status="unchanged"' in done.text
    assert target.read_text(encoding="utf-8") == drifted
    assert not [entry for entry in demo.targets.iterdir() if ".bak-" in entry.name]
    assert workloads.container_ids("api") == []


async def test_a_scoped_admin_sees_the_comparison_of_its_file_without_the_root_path(
    demo: DemoStack, world: World, web: httpx.AsyncClient
) -> None:
    env_db = world.demo_seed.env_db.id
    (demo.targets / ".env.db").write_text(MANAGED[".env.db"] + EDITED_KEY, encoding="utf-8")
    await sign_in(web, "env_scoped_admin")
    page = (await web.get(f"/environments/{env_db}/drift")).text
    assert str(demo.base) not in page and not any(value in page for value in REAL_VALUES)
    assert "DEMO_DB_EDITED_ON_DISK" in page and "Import target as new version" in page
    other = world.demo_seed.env_db_secrets.id
    assert (await web.get(f"/environments/{other}/drift")).status_code == 404
