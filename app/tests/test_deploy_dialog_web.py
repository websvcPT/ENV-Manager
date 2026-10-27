"""The deploy dialog routes and the controls that open it, on the seeded application.

The staging and production deployments are moved onto a real temporary folder and the docker CLI
is a fake script on a real socket file, so a deploy writes real files and runs a real (fake)
command whose calls the tests read back.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import (
    Deployment,
    DeployRun,
    Environment,
)
from app.seeds import EDITOR_SECRET_VALUES
from app.services.deploy_lock import hold_deploy_lock
from app.services.versions import create_version
from app.tests.audit_helpers import web_audit_entries
from app.tests.conftest import sign_in
from app.tests.fake_docker import FakeDocker
from app.tests.web_helpers import (
    HTMX,
    assert_never_cached,
    events_of,
    main_of,
    post,
    stage_deployments,
)

DEPLOY_ROLES = ("root", "admin", "project_admin", "deployment_admin")
SCOPED_BLOCKED = ("viewer", "env_scoped_viewer")
HOOK_COMMAND = "docker compose up -d --no-deps web"
MYSQL_COMMAND = "docker compose up -d --no-deps mysql"
PREFLIGHT_ROW = re.compile(
    r'<label data-fragment="preflight-row" data-env="([^"]+)"(.*?)</label>', re.DOTALL
)


@dataclass(frozen=True)
class Staged:
    """What the tests need: real roots, the fake docker and ids by name."""

    roots: dict[str, Path]
    docker: FakeDocker
    services: dict[str, int]
    environments: dict[str, int]
    seeded_runs: int

    def target(self, deployment: str, name: str) -> Path:
        """The path of a target file below a deployment root."""
        return self.roots[deployment] / "environments" / name


@pytest.fixture
async def staged(web_app: FastAPI, tmp_path: Path) -> Staged:
    """Deployments on a real folder, dist links removed, and a usable fake docker."""
    stage = await stage_deployments(web_app, tmp_path, MYSQL_COMMAND)
    async with web_app.state.session_factory() as db:
        seeded_runs = await db.scalar(select(func.max(DeployRun.id))) or 0
    return Staged(stage.roots, stage.docker, stage.services, stage.environments, seeded_runs)


async def preflight(
    http: httpx.AsyncClient, service: int, data: dict[str, object] | None = None
) -> httpx.Response:
    """Post the pre-flight request the Deploy button sends."""
    return await post(http, f"/services/{service}/deploy/preflight", data)


async def deploy(http: httpx.AsyncClient, service: int, data: dict[str, object]) -> httpx.Response:
    """Post the deploy request the dialog sends."""
    return await post(http, f"/services/{service}/deploy", data)


def chosen(*environments: int, force: bool = False, confirm: bool = False) -> dict[str, object]:
    """The form fields of a choice of files."""
    data: dict[str, object] = {"selection": "explicit", "environment": list(environments)}
    if force:
        data["force"] = "on"
    if confirm:
        data["confirm_force"] = "1"
    return data


async def new_runs(web_app: FastAPI, staged: Staged) -> list[DeployRun]:
    """The deploy runs made since the fixture, oldest first (the seed has runs of its own)."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(DeployRun).where(DeployRun.id > staged.seeded_runs).order_by(DeployRun.id)
        )
        return list(rows.scalars())


def checkbox_rows(page: str) -> dict[str, bool]:
    """Pre-flight rows by file name, True when the row's checkbox is checked."""
    checked: dict[str, bool] = {}
    for name, body in PREFLIGHT_ROW.findall(page):
        box = re.search(r'<input type="checkbox"[^>]*>', body)
        assert box is not None
        checked[name] = " checked" in box.group(0)
    return checked


def row_badges(page: str) -> dict[str, str]:
    """Pre-flight rows by file name with the outcome badge key."""
    return {
        name: re.findall(r'data-status="(\w+)"', body)[-1]
        for name, body in PREFLIGHT_ROW.findall(page)
    }


def timeout_of(page: str) -> int:
    """The htmx timeout set on the form of the fragment, in milliseconds."""
    config = re.search(r"hx-config='([^']+)'", page)
    assert config is not None
    return int(json.loads(config.group(1))["timeout"])


async def test_the_preflight_dry_runs_every_file_in_scope_and_writes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    response = await preflight(web_client, service)
    page = response.text

    assert response.status_code == 200
    assert_never_cached(response)
    assert "HX-Trigger" not in response.headers
    assert checkbox_rows(page) == {
        ".env.web": False,
        ".env.web.replica": False,
        ".env.web.tuning": True,
        "web-ca.pem": True,
    }
    assert set(row_badges(page).values()) == {"proceed"}
    assert "Dry run: 2 environments would be written, 0 warnings, 0 blocked." in page
    assert 'data-fragment="deploy-dialog"' in page and 'id="deploy-dialog-content"' in page
    assert "Force overwrite" in page and 'name="force"' in page
    assert "Deploy 2 environments" in page
    assert list((staged.roots["staging"] / "environments").iterdir()) == []
    (run,) = await new_runs(web_app, staged)
    assert run.dry_run is True
    assert len(await web_audit_entries(web_app, "service.deploy_dry_run")) == 1
    assert await web_audit_entries(web_app, "service.deploy") == []
    assert staged.docker.calls() == []


async def test_the_force_checkbox_follows_the_request_and_forces_the_dry_run(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    tuning = staged.environments["web/.env.web.tuning"]
    staged.target("staging", ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")

    halted = (await preflight(web_client, service, chosen(tuning))).text
    assert row_badges(halted)[".env.web.tuning"] == "halt"
    assert "Drift: Target changed outside the app" in halted
    assert re.search(r'<input type="checkbox" name="force"[^>]* checked', halted) is None

    forced = (await preflight(web_client, service, chosen(tuning, force=True))).text
    assert row_badges(forced)[".env.web.tuning"] == "warnout"
    assert re.search(r'<input type="checkbox" name="force"[^>]* checked', forced) is not None


async def test_rows_carry_the_version_each_one_was_checked_at(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    page = (await preflight(web_client, staged.services["staging/web"])).text
    assert f'<input type="hidden" name="version_{tuning}" value="2">' in page


async def test_a_chosen_version_comes_from_the_history_button_shape(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    response = await preflight(
        web_client,
        staged.services["staging/web"],
        {"environment": tuning, f"version_{tuning}": 1},
    )
    page = response.text
    assert [name for name, on in checkbox_rows(page).items() if on] == [".env.web.tuning"]
    assert "Deploying version 1 of .env.web.tuning, which is not the newest version (v2)." in page
    assert f'<input type="hidden" name="version_{tuning}" value="1">' in page


async def test_a_deploy_writes_the_chosen_files_runs_the_command_once_and_refreshes_panels(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    tuning = staged.environments["web/.env.web.tuning"]
    ca = staged.environments["web/web-ca.pem"]
    staged.docker.behavior('echo "recreated web"')
    web_app.state.docker_cli = staged.docker.cli()

    response = await deploy(web_client, service, chosen(tuning, ca))
    page = response.text

    assert response.status_code == 200
    assert_never_cached(response)
    assert events_of(response) == {"deploy-finished": True}
    assert 'data-tone="ok"' in page and "Success" in page
    assert "2 environments written. The post-deploy command ran once." in page
    assert "Ran once · exit code 0" in page and "recreated web" in page
    assert staged.target("staging", ".env.web.tuning").read_text(encoding="utf-8") == "WORKERS=4\n"
    assert staged.target("staging", "web-ca.pem").exists()
    assert not staged.target("staging", ".env.web").exists()
    assert len(staged.docker.calls()) == 1
    (run,) = [item for item in await new_runs(web_app, staged) if not item.dry_run]
    entry = (await web_audit_entries(web_app, "service.deploy"))[0]
    assert f'href="/audit?entry={entry.id}"' in page and run.audit_entry_id == entry.id


async def test_the_deploy_form_is_given_a_timeout_longer_than_the_longest_command(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    tuning = staged.environments["web/.env.web.tuning"]
    page = (await preflight(web_client, service)).text
    assert timeout_of(page) == 3_720_000
    confirm = (await deploy(web_client, service, chosen(tuning, force=True))).text
    assert timeout_of(confirm) == 3_720_000


async def test_deploying_nothing_is_refused_in_the_dialog_and_writes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    response = await deploy(web_client, staged.services["staging/web"], {"selection": "explicit"})
    assert response.status_code == 422
    assert "Select at least one environment to deploy." in response.text
    assert "Back to pre-flight" in response.text
    assert "HX-Trigger" not in response.headers
    assert await new_runs(web_app, staged) == []
    assert list((staged.roots["staging"] / "environments").iterdir()) == []


async def test_a_forced_deploy_waits_for_its_confirmation_and_then_overwrites_with_a_backup(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    tuning = staged.environments["web/.env.web.tuning"]
    target = staged.target("staging", ".env.web.tuning")
    target.write_text("WORKERS=99\n", encoding="utf-8")

    asked = await deploy(web_client, service, chosen(tuning, force=True))
    assert asked.status_code == 200
    assert "Force overwrite deploys" in asked.text and ".env.web.tuning" in asked.text
    assert "Force deploy 1 environment" in asked.text
    assert 'name="confirm_force" value="1"' in asked.text
    assert "HX-Trigger" not in asked.headers
    assert target.read_text(encoding="utf-8") == "WORKERS=99\n"
    assert await new_runs(web_app, staged) == []

    done = await deploy(web_client, service, chosen(tuning, force=True, confirm=True))
    assert events_of(done) == {"deploy-finished": True}
    assert "forced past the checks" in done.text and "previous file backed up" in done.text
    assert target.read_text(encoding="utf-8") == "WORKERS=4\n"
    backups = [path for path in target.parent.iterdir() if ".bak-" in path.name]
    assert [path.read_text(encoding="utf-8") for path in backups] == ["WORKERS=99\n"]
    entry = (await web_audit_entries(web_app, "service.deploy"))[0]
    assert entry.details["force"] is True


async def test_a_confirmation_alone_never_forces_a_deploy(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    target = staged.target("staging", ".env.web.tuning")
    target.write_text("WORKERS=99\n", encoding="utf-8")
    response = await deploy(
        web_client, staged.services["staging/web"], {**chosen(tuning), "confirm_force": "1"}
    )
    assert "Halted: drift" in response.text
    assert target.read_text(encoding="utf-8") == "WORKERS=99\n"
    entry = (await web_audit_entries(web_app, "service.deploy"))[0]
    assert entry.details["force"] is False


async def test_a_halted_deploy_links_the_drift_view_and_skips_the_command(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    ca = staged.environments["web/web-ca.pem"]
    staged.target("staging", ".env.web.tuning").write_text("WORKERS=99\n", encoding="utf-8")
    page = (await deploy(web_client, staged.services["staging/web"], chosen(tuning, ca))).text
    assert 'data-tone="warn"' in page and "Halted: drift" in page
    assert f'href="/environments/{tuning}/drift"' in page and "Open drift view" in page
    assert 'data-status="halted"' in page and 'data-status="skipped"' in page
    assert "Not run because an environment was not deployed." in page
    assert staged.docker.calls() == []
    assert staged.target("staging", "web-ca.pem").exists()


async def test_a_blocked_deploy_names_the_missing_keys_and_links_the_dist_report(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    dist = staged.roots["staging"] / "environments" / ".env.web.tuning-dist"
    dist.write_text("WORKERS=\nEXTRA_ONE=\nEXTRA_TWO=\n", encoding="utf-8")
    async with web_app.state.session_factory() as db:
        environment = await db.get(Environment, tuning)
        assert environment is not None
        environment.dist_path = "environments/.env.web.tuning-dist"
        await db.commit()
    service = staged.services["staging/web"]

    checked = (await preflight(web_client, service, chosen(tuning))).text
    assert row_badges(checked)[".env.web.tuning"] == "block"
    assert "2 missing keys, policy: block" in checked

    page = (await deploy(web_client, service, chosen(tuning))).text
    assert 'data-tone="err"' in page and "Blocked: missing keys" in page
    assert "Missing EXTRA_ONE, EXTRA_TWO" in page
    assert f'href="/environments/{tuning}/dist"' in page and "Open dist report" in page
    assert not staged.target("staging", ".env.web.tuning").exists()


async def test_a_failing_command_reads_as_deployed_post_deploy_failed_with_scrubbed_output(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    staged.docker.behavior('echo "workers=4 failed to start"; exit 3')
    page = (await deploy(web_client, staged.services["staging/web"], chosen(tuning))).text
    assert 'data-tone="warn"' in page and "Deployed, post-deploy failed" in page
    assert (
        "The post-deploy command exited with code 3, so the workload may still run the old files."
        in page
    )
    assert "exit code 3" in page and "failed to start" in page
    assert "WORKERS=4" not in page


async def test_a_deploy_into_a_folder_that_is_gone_fails_and_offers_a_retry(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    (staged.roots["staging"] / "environments").rmdir()
    service = staged.services["staging/web"]
    page = (await deploy(web_client, service, chosen(tuning))).text
    assert 'data-tone="err"' in page and ">Failed</p>" in page
    assert "Retry deploy" in page and f'hx-post="/services/{service}/deploy/preflight"' in page
    assert 'data-status="skipped"' in page


async def test_a_second_deploy_into_a_busy_folder_is_refused_without_a_run(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    with hold_deploy_lock(web_app.state.lock_dir, str(staged.roots["staging"])):
        response = await deploy(web_client, staged.services["staging/web"], chosen(tuning))
    assert "Another deploy is running for this deployment folder." in response.text
    assert "HX-Trigger" not in response.headers
    assert "View audit entry" not in response.text
    assert [item for item in await new_runs(web_app, staged) if not item.dry_run] == []


async def test_the_preview_shows_the_command_text_to_the_superadmin_only(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    service = staged.services["staging/web"]
    await sign_in(web_client, "root")
    page = (await preflight(web_client, service)).text
    assert HOOK_COMMAND in page and "Command text is shown to the superadmin only." in page
    assert "will run <strong>once</strong>, after these 2 environments are written." in page

    await sign_in(web_client, "admin")
    page = (await preflight(web_client, service)).text
    assert HOOK_COMMAND not in page and "superadmin only" not in page
    assert "will run <strong>once</strong>" in page


async def test_no_response_holds_a_value_or_the_command_for_an_admin(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["prod/mysql"]
    editor = staged.environments["mysql/.env.db.app"]
    for response in (
        await preflight(web_client, service),
        await deploy(web_client, service, chosen(editor)),
    ):
        assert not any(value in response.text for value in EDITOR_SECRET_VALUES)
        assert MYSQL_COMMAND not in response.text


@pytest.mark.parametrize("role", SCOPED_BLOCKED)
async def test_viewers_cannot_open_or_run_a_deploy(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged, role: str
) -> None:
    await sign_in(web_client, role)
    service = staged.services["prod/mysql"]
    db_file = staged.environments["mysql/.env.db"]
    assert (await preflight(web_client, service)).status_code == 403
    response = await deploy(web_client, service, chosen(db_file))
    assert response.status_code == 403
    assert not staged.target("prod", ".env.db").exists()
    assert await new_runs(web_app, staged) == []
    assert staged.docker.calls() == []


async def test_a_service_outside_the_scope_answers_404_and_names_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "ungranted_admin")
    service = staged.services["prod/mysql"]
    db_file = staged.environments["mysql/.env.db"]
    for response in (
        await preflight(web_client, service),
        await deploy(web_client, service, chosen(db_file)),
        await preflight(web_client, 999999),
    ):
        assert response.status_code == 404
        assert "mysql" not in response.text
    assert await new_runs(web_app, staged) == []


async def test_a_scoped_admin_deploys_only_their_file_and_the_command_runs_once(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "env_scoped_admin")
    service = staged.services["prod/mysql"]
    db_file = staged.environments["mysql/.env.db"]
    secrets = staged.environments["mysql/.env.db.secrets"]

    page = (await preflight(web_client, service, chosen(db_file))).text
    assert list(checkbox_rows(page)) == [".env.db"]
    assert ".env.db.secrets" not in page and MYSQL_COMMAND not in page
    assert "The service may also load environments you cannot see; they are not changed." in page

    page = (await deploy(web_client, service, chosen(db_file))).text
    assert "1 environment written. The post-deploy command ran once." in page
    assert staged.target("prod", ".env.db").exists()
    assert not staged.target("prod", ".env.db.secrets").exists()
    assert len(staged.docker.calls()) == 1

    before = len(await new_runs(web_app, staged))
    for data in (chosen(secrets), chosen(db_file, secrets), chosen(999999)):
        response = await deploy(web_client, service, data)
        assert response.status_code == 404
    assert not staged.target("prod", ".env.db.secrets").exists()
    assert len(await new_runs(web_app, staged)) == before
    assert len(staged.docker.calls()) == 1


async def test_a_scoped_admin_opens_the_audit_entry_of_their_own_deploy(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    """The deploy entry names the service, which an environment grant does not cover, but it is
    the admin's own entry, so the result link opens it; others without the scope get not found."""
    await sign_in(web_client, "env_scoped_admin")
    service = staged.services["prod/mysql"]
    db_file = staged.environments["mysql/.env.db"]
    page = (await deploy(web_client, service, chosen(db_file))).text
    link = re.search(r'href="(/audit\?entry=(\d+))"', page)
    assert link is not None
    entry = (await web_audit_entries(web_app, "service.deploy"))[0]
    assert int(link.group(2)) == entry.id
    assert entry.environment_id is None and entry.service_id == service

    opened = await web_client.get(link.group(1))
    assert opened.status_code == 200
    body = main_of(opened.text)
    assert f"Entry #{entry.id}" in body and "service.deploy" in body
    listing = main_of((await web_client.get("/audit?action=service.deploy")).text)
    assert f'id="audit-entry-{entry.id}"' in listing

    web_client.cookies.clear()
    await sign_in(web_client, "ungranted_admin")
    hidden = await web_client.get(link.group(1))
    assert hidden.status_code == 404
    assert "Entry not found" in hidden.text and "mysql" not in main_of(hidden.text)


async def test_an_environment_of_another_service_is_not_deployable_through_this_one(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    response = await deploy(
        web_client,
        staged.services["staging/web"],
        chosen(staged.environments["mysql/.env.db"]),
    )
    assert response.status_code == 404
    assert not staged.target("prod", ".env.db").exists()
    assert await new_runs(web_app, staged) == []


async def test_a_deploy_writes_the_version_the_pre_flight_showed_not_a_newer_one(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    tuning = staged.environments["web/.env.web.tuning"]
    page = (await preflight(web_client, service, chosen(tuning))).text
    assert f'name="version_{tuning}" value="2"' in page
    async with web_app.state.session_factory() as db:
        await create_version(
            db, web_app.state.crypto, tuning, "WORKERS=8\n", author=None, comment="later"
        )
        await db.commit()

    await deploy(web_client, service, {**chosen(tuning), f"version_{tuning}": 2})
    assert staged.target("staging", ".env.web.tuning").read_text(encoding="utf-8") == "WORKERS=4\n"


async def test_a_pinned_older_version_is_deployed_as_asked(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    tuning = staged.environments["web/.env.web.tuning"]
    page = (
        await deploy(
            web_client,
            staged.services["staging/web"],
            {**chosen(tuning), f"version_{tuning}": 1},
        )
    ).text
    assert staged.target("staging", ".env.web.tuning").read_text(encoding="utf-8") == "WORKERS=2\n"
    assert 'data-fragment="result-row"' in page and ">v1</td>" in page


async def test_both_posts_need_the_csrf_token(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    await sign_in(web_client, "admin")
    service = staged.services["staging/web"]
    tuning = staged.environments["web/.env.web.tuning"]
    assert CSRF_COOKIE_NAME in web_client.cookies
    for url in (f"/services/{service}/deploy/preflight", f"/services/{service}/deploy"):
        response = await web_client.post(url, data=chosen(tuning), headers=HTMX)
        assert response.status_code == 403, url
    assert await new_runs(web_app, staged) == []


async def test_the_service_page_offers_the_dialog_to_those_who_may_deploy(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    service = staged.services["staging/web"]
    for role in ("root", "admin"):
        await sign_in(web_client, role)
        page = (await web_client.get(f"/services/{service}")).text
        assert 'id="deploy-dialog"' in page and 'id="deploy-dialog-loading"' in page
        assert f'hx-post="/services/{service}/deploy/preflight"' in page
        assert "Deploy service" in page and 'data-target="deploy-dialog"' in page
    await sign_in(web_client, "viewer")
    page = (await web_client.get(f"/services/{service}")).text
    for absent in ("deploy-dialog", "Deploy service", "/deploy/preflight"):
        assert absent not in page


async def test_a_service_outside_the_allowed_paths_offers_no_deploy(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    service = staged.services["staging/web"]
    async with web_app.state.session_factory() as db:
        deployment = await db.scalar(select(Deployment).where(Deployment.name == "staging"))
        assert deployment is not None
        deployment.root_path = "/outside/the/allowed/paths"
        await db.commit()
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/services/{service}")).text
    assert "Deploy service" not in page and "deploy-dialog" not in page
    assert "This deployment is outside the allowed paths." in main_of(page)


async def test_the_files_tab_opens_the_dialog_from_each_service_row(
    web_client: httpx.AsyncClient, web_app: FastAPI, staged: Staged
) -> None:
    async with web_app.state.session_factory() as db:
        deployment = await db.scalar(select(Deployment.id).where(Deployment.name == "staging"))
    await sign_in(web_client, "admin")
    page = (await web_client.get(f"/deployments/{deployment}/files")).text
    service = staged.services["staging/web"]
    assert f'hx-post="/services/{service}/deploy/preflight"' in page
    assert 'id="deploy-dialog"' in page
    overview = (await web_client.get(f"/deployments/{deployment}")).text
    assert "/deploy/preflight" not in overview and "deploy-dialog" not in overview
    assert f'href="/services/{service}#deploy"' not in page
    await sign_in(web_client, "viewer")
    page = (await web_client.get(f"/deployments/{deployment}/files")).text
    assert "/deploy/preflight" not in page and "deploy-dialog" not in page


async def test_the_history_page_offers_deploy_this_version_to_those_who_may_deploy(
    web_client: httpx.AsyncClient, staged: Staged
) -> None:
    tuning = staged.environments["web/.env.web.tuning"]
    service = staged.services["staging/web"]
    await sign_in(web_client, "admin")
    page = main_of((await web_client.get(f"/environments/{tuning}/history")).text)
    assert "Deploy v2" in page and 'id="deploy-dialog"' in page
    assert f'hx-post="/services/{service}/deploy/preflight"' in page
    vals = re.search(r'hx-vals="([^"]+)"', page)
    assert vals is not None
    decoded = json.loads(vals.group(1).replace("&#34;", '"'))
    assert decoded == {"environment": tuning, f"version_{tuning}": 2}
    await sign_in(web_client, "viewer")
    page = main_of((await web_client.get(f"/environments/{tuning}/history")).text)
    assert "Deploy v" not in page and "deploy-dialog" not in page
