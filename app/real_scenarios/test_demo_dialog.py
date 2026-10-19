"""The deploy dialog routes against the demo stack: real files, real containers.

The web application runs in this process on the throwaway database of the ``world`` fixture and
the host Docker daemon; a signed-in user posts what the dialog posts. Every check is on the real
files (mode, content, backups, what was not touched) and on the demo containers.
"""

import re
import stat
import time

import httpx
from sqlalchemy import select

from app.models import AuditEntry, DeployRun, PostDeployStatus
from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.helpers import MANAGED, snapshot
from app.tests.conftest import sign_in
from app.tests.web_helpers import events_of, post

DRIFTED_ENVIRONMENT = ".env.api"
CREATED_ENVIRONMENT = "demo-ca.pem"


def chosen(*environments: int, force: bool = False, confirm: bool = False) -> dict[str, object]:
    """The fields the dialog posts for a choice of files, at their newest version."""
    data: dict[str, object] = {"selection": "explicit", "environment": list(environments)}
    if force:
        data["force"] = "on"
    if confirm:
        data["confirm_force"] = "1"
    return data


def badges(page: str) -> dict[str, str]:
    """The outcome badge of each pre-flight row by file name."""
    rows = re.findall(
        r'<label data-fragment="preflight-row" data-env="([^"]+)"(.*?)</label>', page, re.DOTALL
    )
    return {name: re.findall(r'data-status="(\w+)"', body)[-1] for name, body in rows}


async def test_the_dialog_deploys_api_for_real_and_the_command_recreates_the_workload(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    await sign_in(web, "root")
    api = world.demo_seed.api.id
    ca = world.demo_seed.env_ca.id
    drifted = (demo.targets / DRIFTED_ENVIRONMENT).read_text(encoding="utf-8")

    page = (await post(web, f"/services/{api}/deploy/preflight")).text
    assert badges(page) == {DRIFTED_ENVIRONMENT: "halt", CREATED_ENVIRONMENT: "proceed"}
    assert "Not run because an environment would not be written." in page
    page = (await post(web, f"/services/{api}/deploy/preflight", chosen(ca))).text
    assert "docker compose up -d --no-deps --force-recreate api" in page
    assert "will run <strong>once</strong>" in page
    assert list(demo.targets.glob("demo-ca.pem")) == []

    before = time.time()
    done = await post(web, f"/services/{api}/deploy", chosen(ca))
    assert events_of(done) == {"deploy-finished": True}
    assert "1 environment written. The post-deploy command ran once." in done.text
    assert "Ran once · exit code 0" in done.text

    created = demo.targets / CREATED_ENVIRONMENT
    assert created.read_text(encoding="utf-8") == MANAGED[CREATED_ENVIRONMENT]
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert (demo.targets / DRIFTED_ENVIRONMENT).read_text(encoding="utf-8") == drifted
    assert workloads.created_since("api", before) == 1
    workloads.wait_for_names("api", {"DEMO_API_MODE", "DEMO_API_PORT", "DEMO_API_TOKEN"})

    async with world.session() as db:
        run = (
            await db.execute(select(DeployRun).order_by(DeployRun.id.desc()).limit(1))
        ).scalar_one()
        assert run.dry_run is False and run.post_deploy_status is PostDeployStatus.RAN
        entry = await db.get(AuditEntry, run.audit_entry_id)
        assert entry is not None and entry.actor_name == "root"
        assert f'href="/audit?entry={entry.id}"' in done.text
        assert "force-recreate" not in str(entry.details)


async def test_a_file_edited_on_disk_is_drift_and_force_waits_for_its_confirmation(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    await sign_in(web, "root")
    db_service = world.demo_seed.db.id
    env_db = world.demo_seed.env_db.id
    managed = MANAGED[".env.db"]
    page = (await post(web, f"/services/{db_service}/deploy/preflight", chosen(env_db))).text
    assert badges(page)[".env.db"] == "unchanged"

    edited = managed + "DEMO_DB_EDITED_ON_DISK=1\n"
    (demo.targets / ".env.db").write_text(edited, encoding="utf-8")
    page = (await post(web, f"/services/{db_service}/deploy/preflight", chosen(env_db))).text
    assert badges(page)[".env.db"] == "halt"
    assert "Drift: Target changed outside the app" in page

    halted = await post(web, f"/services/{db_service}/deploy", chosen(env_db))
    assert "Halted: drift" in halted.text
    assert (demo.targets / ".env.db").read_text(encoding="utf-8") == edited
    assert workloads.container_ids("db") == []

    before = snapshot(demo.targets)
    asked = await post(web, f"/services/{db_service}/deploy", chosen(env_db, force=True))
    assert "Force overwrite deploys" in asked.text and "HX-Trigger" not in asked.headers
    assert snapshot(demo.targets) == before

    since = time.time()
    forced = await post(
        web, f"/services/{db_service}/deploy", chosen(env_db, force=True, confirm=True)
    )
    assert "previous file backed up, forced past the checks" in forced.text
    assert (demo.targets / ".env.db").read_text(encoding="utf-8") == managed
    assert stat.S_IMODE((demo.targets / ".env.db").stat().st_mode) == 0o600
    (backup,) = [entry for entry in demo.targets.iterdir() if entry.name.startswith(".env.db.bak-")]
    assert backup.read_text(encoding="utf-8") == edited
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert workloads.created_since("db", since) == 1

    async with world.session() as session:
        entries = (
            await session.execute(select(AuditEntry).where(AuditEntry.action == "service.deploy"))
        ).scalars()
        assert [entry.details["force"] for entry in entries] == [False, True]


async def test_a_scoped_admin_deploys_only_their_file_and_the_command_runs_once(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    await sign_in(web, "env_scoped_admin")
    db_service = world.demo_seed.db.id
    env_db = world.demo_seed.env_db.id
    secrets = world.demo_seed.env_db_secrets.id
    (demo.targets / ".env.db").unlink()
    secrets_before = snapshot(demo.targets)[".env.db.secrets"]

    page = (await post(web, f"/services/{db_service}/deploy/preflight")).text
    assert badges(page) == {".env.db": "proceed"}
    assert ".env.db.secrets" not in page and "docker compose" not in page
    assert "they are not changed" in page

    since = time.time()
    done = await post(web, f"/services/{db_service}/deploy", chosen(env_db))
    assert "1 environment written. The post-deploy command ran once." in done.text
    assert "docker compose" not in done.text
    assert (demo.targets / ".env.db").read_text(encoding="utf-8") == MANAGED[".env.db"]
    assert snapshot(demo.targets)[".env.db.secrets"] == secrets_before
    assert workloads.created_since("db", since) == 1

    refused = await post(web, f"/services/{db_service}/deploy", chosen(secrets))
    assert refused.status_code == 404
    assert snapshot(demo.targets)[".env.db.secrets"] == secrets_before
    assert workloads.created_since("db", since) == 1
