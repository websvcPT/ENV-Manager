"""Environment settings edit on the web application: controls, policy overrides, path rules and
the deployed state of a file whose target path changes."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import (
    AllowedPath,
    AuditEntry,
    Deployment,
    DeployResult,
    DistMissingPolicy,
    DriftPolicy,
    Environment,
    FileType,
    MissingTargetPolicy,
)
from app.services.deploy_baseline import load_baselines
from app.tests.conftest import sign_in
from app.tests.settings_helpers import browser_as
from app.tests.web_helpers import HTMX, EnvironmentIds, environment_ids, post

OTHERS = ("admin", "viewer", "project_admin", "deployment_admin", "env_scoped_admin")


@dataclass(frozen=True)
class Layout:
    """The ids of the seeded files these tests edit, with the deployments on an allowed folder."""

    host: Path
    ids: EnvironmentIds


@pytest.fixture
async def layout(web_app: FastAPI, tmp_path: Path) -> Layout:
    """Allow a temporary host folder and move every seeded deployment into it."""
    host = (tmp_path / "vhosts").resolve()
    async with web_app.state.session_factory() as db:
        db.add(AllowedPath(path=str(host)))
        for deployment in (await db.execute(select(Deployment))).scalars():
            folder = host / deployment.name
            folder.mkdir(parents=True, exist_ok=True)
            deployment.root_path = str(folder)
        await db.commit()
    return Layout(host=host, ids=await environment_ids(web_app))


def values(**overrides: str) -> dict[str, str]:
    """Settings of the seeded `.env.db` file with nothing set but the overrides."""
    base = {
        "target_path": "environments/.env.db",
        "dist_path": "",
        "file_mode": "",
        "file_owner": "",
        "file_group": "",
        "dist_missing_policy": "",
        "drift_policy": "",
        "missing_target_policy": "",
    }
    return {**base, **overrides}


async def stored(web_app: FastAPI, environment_id: int) -> Environment:
    """A stored environment."""
    async with web_app.state.session_factory() as db:
        found = await db.get(Environment, environment_id)
    assert isinstance(found, Environment)
    return found


async def entries(web_app: FastAPI, action: str) -> list[AuditEntry]:
    """The audit entries of one action, oldest first."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


def changes_of(entry: AuditEntry) -> dict[str, dict[str, str | None]]:
    """The old and new value of each setting an audit entry lists."""
    return cast(dict[str, dict[str, str | None]], entry.details["changes"])


async def test_only_the_superadmin_gets_the_settings_controls(
    web_app: FastAPI, layout: Layout
) -> None:
    editor = layout.ids.editor
    service_id = (await stored(web_app, editor)).service_id
    async with browser_as(web_app, "root") as http:
        page = (await http.get(f"/environments/{editor}")).text
        service = (await http.get(f"/services/{service_id}")).text
    assert 'id="environment-settings-open"' in page
    assert f'href="/environments/{editor}/settings"' in page
    assert 'data-row-action="settings"' in service
    for user in OTHERS:
        async with browser_as(web_app, user) as http:
            response = await http.get(f"/environments/{editor}")
            if response.status_code == 200:
                assert "environment-settings-open" not in response.text, user
            for url in (
                f"/environments/{editor}/settings",
                f"/environments/{editor}/settings/target-check",
            ):
                assert (await http.get(url, headers=HTMX)).status_code in (403, 404), (user, url)
            refused = await post(http, f"/environments/{editor}/settings", values())
            assert refused.status_code in (403, 404), user
    assert await entries(web_app, "environment.update") == []


async def test_anonymous_visitors_are_sent_to_sign_in(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    url = f"/environments/{layout.ids.editor}/settings"
    assert (await web_client.get(url, follow_redirects=False)).status_code in (303, 401)


async def test_the_form_shows_the_stored_values_and_the_deployment_policies(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/environments/{layout.ids.db}/settings")
    page = response.text
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert ">File settings</h1>" in page and "Superadmin only." in page
    assert 'id="env-settings"' in page and 'data-fragment="environment-settings"' in page
    assert f'hx-post="/environments/{layout.ids.db}/settings"' in page
    assert f'hx-get="/environments/{layout.ids.db}/settings/target-check"' in page
    assert page.count('data-fragment="form-section"') == 2
    for field_id in ("tp", "dp", "m", "o", "g"):
        assert f'id="{field_id}"' in page
    for name in ("dist_missing_policy", "drift_policy", "missing_target_policy"):
        assert re.search(rf'name="{name}" value="" checked', page), name
    assert "Currently halt." in page and "Currently block." in page
    assert ">Save settings</button>" in page and ">Cancel</a>" in page
    assert 'data-fragment="target-move"' not in page


async def test_saving_stores_the_overrides_and_audits_the_old_and_new_values(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    environment_id = layout.ids.db
    response = await post(
        web_client,
        f"/environments/{environment_id}/settings",
        values(
            file_mode="640",
            file_owner="www-data",
            dist_path="environments/.env.db-reference",
            drift_policy="overwrite",
            dist_missing_policy="warn",
            missing_target_policy="halt",
        ),
    )
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/environments/{environment_id}?updated=true"
    environment = await stored(web_app, environment_id)
    assert environment.file_mode == "0640" and environment.file_owner == "www-data"
    assert environment.dist_path == "environments/.env.db-reference"
    assert environment.drift_policy is DriftPolicy.OVERWRITE
    assert environment.dist_missing_policy is DistMissingPolicy.WARN
    assert environment.missing_target_policy is MissingTargetPolicy.HALT
    assert "Settings saved" in (await web_client.get(response.headers["hx-redirect"])).text
    (entry,) = await entries(web_app, "environment.update")
    assert entry.environment_id == environment_id
    assert changes_of(entry)["drift_policy"] == {"from": None, "to": "overwrite"}
    assert changes_of(entry)["file_mode"] == {"from": None, "to": "0640"}
    assert "target_path" not in changes_of(entry)

    form = (await web_client.get(f"/environments/{environment_id}/settings")).text
    assert re.search(r'name="drift_policy" value="overwrite" checked', form)

    cleared = await post(web_client, f"/environments/{environment_id}/settings", values())
    assert cleared.status_code == 200
    environment = await stored(web_app, environment_id)
    assert environment.drift_policy is None and environment.file_mode is None
    assert environment.dist_missing_policy is None and environment.missing_target_policy is None
    assert len(await entries(web_app, "environment.update")) == 2


async def test_a_save_that_changes_nothing_writes_no_audit_entry(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    environment = await stored(web_app, layout.ids.db)
    same = values(target_path=environment.target_path, dist_path=environment.dist_path or "")
    response = await post(web_client, f"/environments/{layout.ids.db}/settings", same)
    assert response.status_code == 200
    assert await entries(web_app, "environment.update") == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target_path": ""}, "Enter a target path."),
        ({"target_path": "/etc/passwd"}, "relative to the deployment root"),
        ({"target_path": "../outside"}, "outside the deployment root"),
        (
            {"target_path": "environments/.env.db.secrets"},
            "Another file of this deployment already uses this target path.",
        ),
        (
            {"dist_path": "environments/.env.db"},
            "The dist file must be a different file from the target.",
        ),
        ({"file_mode": "9"}, "octal mode"),
        ({"file_owner": "bad owner!"}, "Owner must be"),
        ({"drift_policy": "ignore"}, "Choose one of the listed policies."),
    ],
)
async def test_a_refused_value_shows_the_reason_and_changes_nothing(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    layout: Layout,
    overrides: dict[str, str],
    message: str,
) -> None:
    await sign_in(web_client, "root")
    before = await stored(web_app, layout.ids.db)
    response = await post(
        web_client, f"/environments/{layout.ids.db}/settings", values(**overrides)
    )
    assert response.status_code == 422, response.text
    assert message in response.text and 'id="env-settings"' in response.text
    after = await stored(web_app, layout.ids.db)
    assert (after.target_path, after.dist_path, after.file_mode, after.drift_policy) == (
        before.target_path,
        before.dist_path,
        before.file_mode,
        before.drift_policy,
    )
    assert await entries(web_app, "environment.update") == []


async def test_a_changed_target_path_of_a_deployed_file_needs_the_confirmation(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    tuning = layout.ids.tuning
    old = (await stored(web_app, tuning)).target_path
    async with web_app.state.session_factory() as db:
        result = (
            await db.execute(select(DeployResult).where(DeployResult.environment_id == tuning))
        ).scalar_one()
        result.written_hash = "0" * 64
        await db.commit()
        assert tuning in await load_baselines(db, [tuning])
    moved = values(target_path="environments/.env.web.tuning.moved")

    check = (
        await web_client.get(
            f"/environments/{tuning}/settings/target-check",
            params={"target_path": moved["target_path"]},
            headers=HTMX,
        )
    ).text
    assert 'data-fragment="target-move"' in check and "version 1" in check
    assert old in check and 'id="ack-move"' in check
    same = (
        await web_client.get(
            f"/environments/{tuning}/settings/target-check", params={"target_path": old}
        )
    ).text
    assert 'data-fragment="target-move"' not in same

    refused = await post(web_client, f"/environments/{tuning}/settings", moved)
    assert refused.status_code == 422
    assert "Confirm that the file counts as not deployed at the new path" in refused.text
    assert (await stored(web_app, tuning)).target_path == old

    accepted = await post(
        web_client, f"/environments/{tuning}/settings", {**moved, "acknowledge_move": "1"}
    )
    assert accepted.status_code == 200
    assert (await stored(web_app, tuning)).target_path == moved["target_path"]
    (entry,) = await entries(web_app, "environment.update")
    assert changes_of(entry)["target_path"] == {"from": old, "to": moved["target_path"]}

    async with web_app.state.session_factory() as db:
        assert await load_baselines(db, [tuning]) == {}
    page = (await web_client.get(f"/environments/{tuning}")).text
    assert "never deployed" in page


async def test_a_file_that_was_never_deployed_moves_without_a_confirmation(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    editor = layout.ids.editor
    moved = values(target_path="environments/.env.editor.moved")
    check = (
        await web_client.get(
            f"/environments/{editor}/settings/target-check",
            params={"target_path": moved["target_path"]},
        )
    ).text
    assert "was never deployed to" in check and 'id="ack-move"' not in check
    response = await post(web_client, f"/environments/{editor}/settings", moved)
    assert response.status_code == 200
    assert (await stored(web_app, editor)).target_path == moved["target_path"]


async def test_the_form_shows_the_stored_type_as_text_and_has_no_type_control(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    env_form = (await web_client.get(f"/environments/{layout.ids.db}/settings")).text
    assert re.search(r'<p id="ft"[^>]*>ENV file</p>', env_form)
    assert "To change it, delete the file and create it again." in env_form
    assert 'name="file_type"' not in env_form and "<select" not in env_form
    assert 'id="dp"' in env_form
    cert_form = (await web_client.get(f"/environments/{layout.ids.ca}/settings")).text
    assert re.search(r'<p id="ft"[^>]*>Certificate</p>', cert_form)
    assert 'name="file_type"' not in cert_form and 'id="dp"' not in cert_form


@pytest.mark.parametrize("submitted", ["cert", "certificate", "ENV", " cert "])
async def test_a_submitted_type_other_than_the_stored_one_is_refused(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout, submitted: str
) -> None:
    await sign_in(web_client, "root")
    url = f"/environments/{layout.ids.db}/settings"
    refused = await post(web_client, url, values(file_type=submitted))
    assert refused.status_code == 422
    assert "The type of a file cannot be changed." in refused.text
    assert (await stored(web_app, layout.ids.db)).file_type is FileType.ENV
    assert await entries(web_app, "environment.update") == []


async def test_a_certificate_keeps_its_type_and_the_stored_type_is_accepted(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    cert_url = f"/environments/{layout.ids.ca}/settings"
    target = (await stored(web_app, layout.ids.ca)).target_path
    to_env = await post(web_client, cert_url, values(target_path=target, file_type="env"))
    assert to_env.status_code == 422
    assert "The type of a file cannot be changed." in to_env.text
    assert (await stored(web_app, layout.ids.ca)).file_type is FileType.CERT
    same = await post(web_client, cert_url, values(target_path=target, file_type="cert"))
    assert same.status_code == 200
    assert (await stored(web_app, layout.ids.ca)).file_type is FileType.CERT
    env_url = f"/environments/{layout.ids.db}/settings"
    assert (await post(web_client, env_url, values(file_type="env"))).status_code == 200
    assert (await stored(web_app, layout.ids.db)).file_type is FileType.ENV


async def test_a_certificate_with_a_dist_path_is_refused(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    stored_cert = await post(
        web_client, f"/environments/{layout.ids.ca}/settings", values(dist_path="x-dist")
    )
    assert stored_cert.status_code == 422
    assert "A certificate has no dist file." in stored_cert.text
    assert (await stored(web_app, layout.ids.ca)).dist_path is None
