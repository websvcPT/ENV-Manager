"""The drift view routes on the seeded application: masking, reveal, import, overwrite and roles.

The seeded deployments are moved onto a real temporary folder so the target is a real file read
through the confined reader and an overwrite is a real forced deploy. The production MySQL file
`.env.db.app` (distinctive secret values, three versions) and `.env.db` serve as the files.
"""

import re
import stat
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select

from app.models import (
    AllowedPath,
    Deployment,
    DeployRun,
    Environment,
    EnvironmentVersion,
)
from app.seeds import EDITOR_SECRET_VALUES
from app.services.notify_events import FileChange, FileChanged
from app.tests.audit_helpers import web_audit_entries
from app.tests.conftest import sign_in
from app.tests.dist_helpers import relocate_deployments
from app.tests.notify_helpers import RecordingEvents, file_event_ids
from app.tests.web_helpers import (
    HTMX,
    EnvironmentIds,
    add_version,
    assert_never_cached,
    hx_vals,
    main_of,
    post,
)

EDITORS = ("root", "admin", "project_admin", "deployment_admin")
OUT_OF_SCOPE = ("env_scoped_admin", "env_scoped_viewer", "ungranted_admin")
TARGET_VALUE = "disk-secret-value-9f3a"
DEPLOYED_VALUE = "deployed-secret-value-4c1d"
MANAGED_VALUE = "managed-secret-value-77be"
SAME_VALUE = "same-secret-value-02aa"
ALL_VALUES = (
    TARGET_VALUE,
    DEPLOYED_VALUE,
    MANAGED_VALUE,
    SAME_VALUE,
    "only-managed-secret-5e5e",
    "only-disk-secret-c0c0",
    *EDITOR_SECRET_VALUES,
)
DEPLOYED_TEXT = (
    f"DRIFT_KEY={DEPLOYED_VALUE}\nDROPPED_KEY=dropped-secret-0001\nSAME_KEY={SAME_VALUE}\n"
)
MANAGED_TEXT = (
    f"DRIFT_KEY={MANAGED_VALUE}\nONLY_MANAGED=only-managed-secret-5e5e\nSAME_KEY={SAME_VALUE}\n"
)
TARGET_TEXT = f"DRIFT_KEY={TARGET_VALUE}\nONLY_DISK=only-disk-secret-c0c0\nSAME_KEY={SAME_VALUE}\n"
KEY_NAMES = ("DRIFT_KEY", "DROPPED_KEY", "ONLY_MANAGED", "ONLY_DISK", "SAME_KEY")


@dataclass(frozen=True)
class World:
    """Where the files of the production deployment live and the ids the tests use."""

    root: Path
    service: int
    environment: int
    scoped: int

    @property
    def target(self) -> Path:
        """The target of the drift file on disk."""
        return self.root / "environments" / ".env.db.app"

    @property
    def scoped_target(self) -> Path:
        """The target of the file a scoped admin holds."""
        return self.root / "environments" / ".env.db"


@pytest.fixture
async def world(web_app: FastAPI, tmp_path: Path, ids: EnvironmentIds) -> World:
    """The production deployment on a real folder with no owner, no dist links."""
    async with web_app.state.session_factory() as db:
        roots = await relocate_deployments(db, tmp_path / "host")
        for deployment in (await db.execute(select(Deployment))).scalars():
            deployment.file_owner = None
            deployment.file_group = None
        for environment in (await db.execute(select(Environment))).scalars():
            environment.dist_path = None
        service = await db.scalar(
            select(Environment.service_id).where(Environment.id == ids.editor)
        )
        await db.commit()
    assert service is not None
    return World(roots["prod"], service, ids.editor, ids.db)


async def deployed_then_drifted(web_app: FastAPI, web: httpx.AsyncClient, world: World) -> None:
    """Deploy a version of the drift file, save a newer one and edit the target on disk."""
    await add_version(web_app, world.environment, DEPLOYED_TEXT)
    await sign_in(web, "root")
    deployed = await post(
        web,
        f"/services/{world.service}/deploy",
        {"selection": "explicit", "environment": [world.environment]},
    )
    assert deployed.status_code == 200 and "Success" in deployed.text, deployed.text[:400]
    await add_version(web_app, world.environment, MANAGED_TEXT)
    world.target.write_text(TARGET_TEXT, encoding="utf-8")
    web.cookies.clear()


def drift_url(environment: int) -> str:
    """The drift view of an environment."""
    return f"/environments/{environment}/drift"


async def page_vals(http: httpx.AsyncClient, environment: int, action: str) -> dict[str, object]:
    """The hx-vals of an action control on the drift page, as the signed-in user sees it."""
    page = (await http.get(drift_url(environment))).text
    return hx_vals(page, f"/environments/{environment}/{action}")


async def count(web_app: FastAPI, model: type, *where: object) -> int:
    """How many rows of a model match."""
    async with web_app.state.session_factory() as db:
        return (
            await db.scalar(select(func.count()).select_from(model).where(*where)) or 0  # type: ignore[arg-type]
        )


async def newest_number(web_app: FastAPI, environment: int) -> int:
    """The newest version number of an environment."""
    async with web_app.state.session_factory() as db:
        number = await db.scalar(
            select(func.max(EnvironmentVersion.number)).where(
                EnvironmentVersion.environment_id == environment
            )
        )
    assert number is not None
    return int(number)


@pytest.mark.parametrize("role", [*EDITORS, "viewer"])
async def test_the_masked_page_never_contains_a_value_for_any_role(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World, role: str
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, role)
    runs = await count(web_app, DeployRun)
    for headers in ({}, HTMX):
        response = await web_client.get(drift_url(world.environment), headers=headers)
        assert response.status_code == 200
        assert_never_cached(response)
        leaked = [value for value in ALL_VALUES if value in response.text]
        assert not leaked, (role, leaked)
        assert 'data-fragment="drift-view"' in response.text
        for key in ("DRIFT_KEY", "ONLY_DISK", "ONLY_MANAGED", "DROPPED_KEY"):
            assert key in response.text, key
        assert "SAME_KEY" not in response.text
        assert "BEGIN" not in response.text and "<textarea" not in response.text
        assert str(world.root) not in response.text
    assert await count(web_app, DeployRun) == runs
    assert await web_audit_entries(web_app, "environment.reveal") == []


async def test_the_page_lists_what_differs_per_column_with_masked_cells(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "root")
    page = main_of((await web_client.get(drift_url(world.environment))).text)
    rows = re.findall(
        r'<div data-fragment="drift-row".*?(?=<div data-fragment="drift-row"|</section>)',
        page,
        re.DOTALL,
    )
    assert len(rows) == 4
    assert "Changed on disk" in rows[0] and "Changed in managed" in rows[0]
    assert "DRIFT_KEY" in rows[0] and "Added on disk" in rows[1] and "ONLY_DISK" in rows[1]
    assert "DROPPED_KEY" in rows[2]
    assert "Removed on disk" in rows[2] and "Removed in managed" in rows[2]
    assert "Added in managed" in rows[3] and "ONLY_MANAGED" in rows[3]
    assert "not set" in rows[1] and "••••••••" in rows[0]
    assert "1 unchanged key hidden" in page
    assert 'data-status="drift"' in page and ">Drift<" in page
    assert "The target file changed outside the app since the last deploy." in page
    for slot in ("drift-target-0", "drift-deployed-0", "drift-managed-0"):
        assert re.search(rf'<span id="{slot}" data-value class="[^"]*"></span>', page), slot
    assert "hash " in page and "latest version" in page and "never deployed" not in page


async def test_a_viewer_gets_the_masked_comparison_and_no_control(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "viewer")
    page = main_of((await web_client.get(drift_url(world.environment))).text)
    assert "Values are masked for the Viewer group." in page
    for absent in (
        "Reveal",
        "Hide values",
        "hx-post",
        "hx-vals",
        "overwrite-dialog",
        "Import target",
        "Overwrite target",
        "dialog-open",
        'type="hidden"',
    ):
        assert absent not in page, absent
    assert ">Back to service</a>" in page and f'href="/services/{world.service}"' in page
    post_base = f"/environments/{world.environment}"
    assert (await post(web_client, f"{post_base}/drift/reveal", {"managed": 1})).status_code == 403
    assert (await post(web_client, f"{post_base}/import", {})).status_code == 403
    assert (await post(web_client, f"{post_base}/overwrite", {})).status_code == 403


async def test_an_editor_gets_the_controls_with_the_numbers_and_hash_the_page_was_built_from(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    page = main_of((await web_client.get(drift_url(world.environment))).text)
    base = f"/environments/{world.environment}"
    managed = await newest_number(web_app, world.environment)
    reveal = hx_vals(page, f"{base}/drift/reveal")
    assert reveal["managed"] == managed and reveal["deployed"] == managed - 1
    assert isinstance(reveal["target"], str) and len(reveal["target"]) == 64
    assert reveal["columns"] == "target,deployed,managed"
    assert hx_vals(page, f"{base}/import") == {
        "base_version": managed,
        "target_hash": reveal["target"],
    }
    assert "Overwrite target…" in page and "Import target as new version" in page
    assert ">Cancel</a>" in page and 'data-js="dialog-open"' in page
    form = re.search(r'<form id="overwrite-form"[^>]*>', page)
    assert form is not None
    assert "hx-config='{\"timeout\": 3720000}'" in form.group(0)
    assert 'hx-sync="this:drop"' in form.group(0)
    assert f'name="base_version" value="{managed}"' in page
    assert 'name="confirm_overwrite" value="1"' in page
    assert "forced deploy of this file" in page and "backup copy" in page
    assert "audit log" in page


async def test_a_reveal_answers_only_the_values_audited_and_uncached(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    base = f"/environments/{world.environment}"
    page = (await web_client.get(drift_url(world.environment))).text
    values = hx_vals(page, f"{base}/drift/reveal")
    answer = await post(web_client, f"{base}/drift/reveal", values)

    assert answer.status_code == 200
    assert_never_cached(answer)
    for value in (TARGET_VALUE, DEPLOYED_VALUE, MANAGED_VALUE, "only-disk-secret-c0c0"):
        assert value in answer.text
    assert SAME_VALUE not in answer.text
    for name in KEY_NAMES:
        assert name not in answer.text
    assert answer.text.count("<span hx-ignore data-reveal-value>") == answer.text.count(
        '<template hx type="partial"'
    )
    assert 'hx-target="#drift-target-0"' in answer.text
    assert 'hx-target="#drift-managed-3"' in answer.text or "drift-managed-" in answer.text
    entries = await web_audit_entries(web_app, "environment.reveal")
    assert {entry.details["column"] for entry in entries} == {"target", "deployed", "managed"}
    flat = " ".join(f"{entry.details} {entry.target_name}" for entry in entries)
    assert not any(value in flat for value in ALL_VALUES)
    assert str(world.root) not in flat


async def test_a_reveal_of_a_stale_page_answers_a_notice_without_any_value(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    base = f"/environments/{world.environment}"
    values = await page_vals(web_client, world.environment, "drift/reveal")
    world.target.write_text("DRIFT_KEY=changed-after-the-page\n", encoding="utf-8")
    answer = await post(web_client, f"{base}/drift/reveal", values)
    assert answer.status_code == 409
    assert_never_cached(answer)
    assert 'hx-target="#drift-notice"' in answer.text
    assert "The files changed since this page was opened." in answer.text
    assert not any(value in answer.text for value in ALL_VALUES)
    assert "changed-after-the-page" not in answer.text
    assert await web_audit_entries(web_app, "environment.reveal") == []
    assert (await post(web_client, f"{base}/drift/reveal", {})).status_code == 404


async def test_importing_saves_the_target_as_a_new_version_and_the_view_reads_no_drift(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    base = f"/environments/{world.environment}"
    before = await newest_number(web_app, world.environment)
    values = await page_vals(web_client, world.environment, "import")
    done = await post(web_client, f"{base}/import", values)

    assert done.status_code == 200
    assert done.headers["HX-Redirect"] == f"{base}/drift?imported={before + 1}"
    assert done.headers["Cache-Control"] == "no-store"
    assert await newest_number(web_app, world.environment) == before + 1
    (entry,) = await web_audit_entries(web_app, "environment.import")
    assert entry.details == {"number": before + 1, "source": "target"}
    assert not any(value in str(entry.details) for value in ALL_VALUES)
    again = await web_client.get(done.headers["HX-Redirect"])
    assert f"Imported the target as version {before + 1}. Pending deploy." in again.text
    assert 'data-status="deployed"' in again.text and ">No drift<" in again.text
    assert "Import target as new version" not in again.text
    assert world.target.read_text(encoding="utf-8") == TARGET_TEXT


async def test_an_import_of_a_changed_file_or_an_old_page_is_refused_in_the_notice(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    base = f"/environments/{world.environment}"
    values = await page_vals(web_client, world.environment, "import")
    before = await newest_number(web_app, world.environment)

    world.target.write_text(TARGET_TEXT + "LATE=1\n", encoding="utf-8")
    changed = await post(web_client, f"{base}/import", values)
    assert changed.status_code == 422
    assert 'id="drift-notice"' in changed.text
    assert "The target file changed since this page was opened. Reload the page." in changed.text
    await add_version(web_app, world.environment, "NEWER=1\n")
    stale = await post(web_client, f"{base}/import", values)
    assert stale.status_code == 422
    assert "This file changed since you opened it." in stale.text
    assert await newest_number(web_app, world.environment) == before + 1
    assert await web_audit_entries(web_app, "environment.import") == []
    assert not any(value in stale.text + changed.text for value in ALL_VALUES)


async def test_overwriting_waits_for_its_confirmation_then_replaces_the_target_with_a_backup(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    base = f"/environments/{world.environment}"
    managed = await newest_number(web_app, world.environment)
    runs = await count(web_app, DeployRun)

    asked = await post(web_client, f"{base}/overwrite", {"base_version": managed})
    assert asked.status_code == 422 and "Confirm the overwrite to continue." in asked.text
    assert 'id="overwrite-form"' in asked.text
    assert world.target.read_text(encoding="utf-8") == TARGET_TEXT
    assert await count(web_app, DeployRun) == runs

    done = await post(
        web_client, f"{base}/overwrite", {"base_version": managed, "confirm_overwrite": "1"}
    )
    assert done.status_code == 200
    assert done.headers["HX-Redirect"] == (
        f"{base}/drift?overwritten={managed}&backup=1&same=0&post=ok"
    )
    assert world.target.read_text(encoding="utf-8") == MANAGED_TEXT
    (backup,) = [entry for entry in world.target.parent.iterdir() if ".bak-" in entry.name]
    assert backup.read_text(encoding="utf-8") == TARGET_TEXT
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    forced = (await web_audit_entries(web_app, "service.deploy"))[-1]
    assert forced.details["force"] is True
    page = await web_client.get(done.headers["HX-Redirect"])
    assert f"Overwrote the target with version {managed}." in page.text
    assert "The previous file was kept as a backup next to it." in page.text
    assert 'data-status="deployed"' in page.text and ">No drift<" in page.text
    assert "Overwrite target…" not in page.text


async def test_an_overwrite_of_an_old_page_or_an_undrifted_target_is_refused_in_the_form(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    base = f"/environments/{world.environment}"
    managed = await newest_number(web_app, world.environment)
    confirmed = {"confirm_overwrite": "1"}

    old = await post(web_client, f"{base}/overwrite", {"base_version": managed - 1, **confirmed})
    assert old.status_code == 422 and "This file changed since you opened it." in old.text
    world.target.write_text(MANAGED_TEXT, encoding="utf-8")
    nothing = await post(web_client, f"{base}/overwrite", {"base_version": managed, **confirmed})
    assert nothing.status_code == 422 and "nothing to overwrite" in nothing.text
    assert world.target.read_text(encoding="utf-8") == MANAGED_TEXT
    assert [e for e in world.target.parent.iterdir() if ".bak-" in e.name] == []


async def test_the_toast_after_an_overwrite_says_when_the_post_deploy_command_failed(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, "admin")
    page = await web_client.get(
        f"{drift_url(world.environment)}?overwritten=3&backup=1&same=0&post=failed"
    )
    assert "The post-deploy command failed, see the service page." in page.text
    assert 'role="status"' in page.text
    plain = await web_client.get(f"{drift_url(world.environment)}?overwritten=abc&imported=x")
    assert "Overwrote" not in plain.text and "Imported the target" not in plain.text


@pytest.mark.parametrize("role", OUT_OF_SCOPE)
async def test_users_outside_the_scope_get_404_on_every_drift_route(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World, role: str
) -> None:
    await deployed_then_drifted(web_app, web_client, world)
    await sign_in(web_client, role)
    base = f"/environments/{world.environment}"
    assert (await web_client.get(drift_url(world.environment))).status_code == 404
    for path in ("drift/reveal", "import", "overwrite"):
        response = await post(web_client, f"{base}/{path}", {"managed": 1, "base_version": 1})
        assert response.status_code == 404, path
        assert not any(value in response.text for value in ALL_VALUES)
    assert (await web_client.get("/environments/999999/drift")).status_code == 404
    assert (await post(web_client, "/environments/999999/overwrite")).status_code == 404


async def test_a_scoped_admin_uses_the_view_of_the_file_it_holds_without_learning_a_path(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    world.scoped_target.write_text("SCOPED_KEY=scoped-secret-value\n", encoding="utf-8")
    await sign_in(web_client, "env_scoped_admin")
    page = (await web_client.get(drift_url(world.scoped))).text
    assert str(world.root) not in page and "scoped-secret-value" not in page
    assert 'data-status="never"' in page and ">Never deployed<" in page
    assert "Import target as new version" in page and "Overwrite target…" in page
    values = hx_vals(page, f"/environments/{world.scoped}/import")
    done = await post(web_client, f"/environments/{world.scoped}/import", values)
    assert done.status_code == 200 and "imported=" in done.headers["HX-Redirect"]


@pytest.mark.parametrize(
    ("setup", "badge", "label", "sentence"),
    [
        ("missing", "drift", "Target missing", "The target file does not exist."),
        ("folder", "drift", "Target missing", "The target folder does not exist."),
        ("symlink", "failed", "Cannot read target", "The target is a symbolic link"),
        ("binary", "failed", "Cannot read target", "not valid UTF-8 text"),
        ("outside", "blocked", "Outside allowed paths", "The deployment is outside the allowed"),
        ("never", "never", "Never deployed", "was never deployed by the app"),
        ("same", "deployed", "No drift", "matches the managed version"),
    ],
)
async def test_every_state_reads_in_path_free_words_with_no_action_where_none_can_work(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    world: World,
    tmp_path: Path,
    setup: str,
    badge: str,
    label: str,
    sentence: str,
) -> None:
    if setup == "same":
        world.target.write_text((await _managed_text(web_app, world.environment)), encoding="utf-8")
    elif setup == "never":
        world.target.write_text("OTHER=1\n", encoding="utf-8")
    elif setup == "symlink":
        outside = tmp_path / "outside.env"
        outside.write_text("X=1\n", encoding="utf-8")
        world.target.symlink_to(outside)
    elif setup == "binary":
        world.target.write_bytes(b"A=\xff\xfe\n")
    elif setup == "folder":
        world.target.parent.rmdir()
    elif setup == "outside":
        async with web_app.state.session_factory() as db:
            await db.execute(delete(AllowedPath))
            await db.commit()
    await sign_in(web_client, "root")
    page = (await web_client.get(drift_url(world.environment))).text
    assert f'data-status="{badge}"' in page and f">{label}<" in page
    assert sentence in page
    assert str(world.root) not in page and str(tmp_path) not in page
    actionable = setup == "never"
    assert ("Overwrite target…" in page) is actionable
    assert ("Import target as new version" in page) is actionable
    assert ("overwrite-dialog" in page) is actionable
    assert not any(value in page for value in ALL_VALUES)


async def _managed_text(web_app: FastAPI, environment: int) -> str:
    """The text of the newest version of an environment."""
    async with web_app.state.session_factory() as db:
        version = await db.scalar(
            select(EnvironmentVersion)
            .where(EnvironmentVersion.environment_id == environment)
            .order_by(EnvironmentVersion.number.desc())
            .limit(1)
        )
        assert version is not None
        return str(web_app.state.crypto.decrypt_text(version.content_encrypted))


async def test_the_halted_result_link_opens_the_view(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    await sign_in(web_client, "root")
    world.target.write_text("DISK_ONLY=1\n", encoding="utf-8")
    halted = await post(
        web_client,
        f"/services/{world.service}/deploy",
        {"selection": "explicit", "environment": [world.environment]},
    )
    assert f'href="/environments/{world.environment}/drift"' in halted.text
    opened = await web_client.get(f"/environments/{world.environment}/drift")
    assert opened.status_code == 200 and "Never deployed" in opened.text
    assert world.target.read_text(encoding="utf-8") == "DISK_ONLY=1\n"


async def test_importing_the_target_announces_the_file_change_to_the_notifier(
    web_app: FastAPI, web_client: httpx.AsyncClient, world: World
) -> None:
    """The import route hands its notifier to the service."""
    await deployed_then_drifted(web_app, web_client, world)
    recorder = RecordingEvents()
    web_app.state.notifier = recorder
    await sign_in(web_client, "admin")
    before = await newest_number(web_app, world.environment)
    values = await page_vals(web_client, world.environment, "import")
    assert (
        await post(web_client, f"/environments/{world.environment}/import", values)
    ).status_code == 200
    project, deployment, service, name = await file_event_ids(web_app, world.environment)
    assert recorder.events == [
        FileChanged(
            project,
            deployment,
            service,
            world.environment,
            name,
            before + 1,
            FileChange.IMPORTED,
            "admin",
        )
    ]
