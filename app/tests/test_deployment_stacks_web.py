"""The New deployment page with the stacks found on the host, the Compose project field and the
suggestion of the edit form, on the seeded application with a fake Docker."""

import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AllowedPath, AuditEntry, Deployment, Project
from app.tests.conftest import sign_in
from app.tests.fake_docker import FakeDocker
from app.tests.stack_helpers import answer, make_stack, project
from app.tests.web_helpers import HTMX, post

BETA_FILES = ("docker-compose.yml", "docker-compose.override.yml")


@dataclass(frozen=True)
class Host:
    """The allowed host folder with its stacks and the seeded ids the tests use."""

    root: Path
    fancy: int
    prod: int

    @property
    def new_url(self) -> str:
        """The New deployment page of the Fancy project."""
        return f"/projects/{self.fancy}/deployments/new"

    @property
    def create_url(self) -> str:
        """Where the form posts."""
        return f"/projects/{self.fancy}/deployments"


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker that knows no project."""
    docker = FakeDocker(tmp_path / "fake")
    answer(docker, [])
    return docker


@pytest.fixture
async def host(
    web_app: FastAPI, tmp_path: Path, fake: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> Host:
    """Allow a temporary host folder (a mount) with an ``alpha`` stack, on the fake Docker."""
    root = (tmp_path / "vhosts").resolve()
    make_stack(root / "alpha")
    make_stack(root / "beta", "docker-compose.yml", "docker-compose.override.yml")
    monkeypatch.setattr("app.services.paths.read_mount_points", lambda *args: [root])
    web_app.state.docker_cli = fake.cli()
    async with web_app.state.session_factory() as db:
        db.add(AllowedPath(path=str(root)))
        await db.commit()
        fancy = (
            await db.execute(select(Project.id).where(Project.name == "Fancy Proj. Name"))
        ).scalar_one()
        prod_query = select(Deployment.id).where(Deployment.name == "prod")
        prod = (await db.execute(prod_query)).scalar_one()
    return Host(root, fancy, prod)


def row_of(page: str, folder: Path | str) -> str:
    """The table row of the stack in a folder."""
    found = re.search(
        rf'<tr data-fragment="stack-row" data-folder="{re.escape(str(folder))}">(.*?)</tr>',
        page,
        re.DOTALL,
    )
    assert found is not None, f"no row for {folder}"
    return found.group(1)


def text_of(markup: str) -> str:
    """The visible text of a piece of markup, with the white space collapsed."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def use_query(row: str) -> dict[str, list[str]]:
    """The folder, project and files the Use button of a row carries."""
    found = re.search(r'hx-get="(/projects/[^"]+/use\?[^"]+)"', row)
    assert found is not None
    address = unescape(found.group(1))
    return parse_qs(urlsplit(address).query)


def form_values(fragment: str) -> dict[str, str]:
    """The value of each named input of the form fragment."""
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input[^>]* name="([^"]+)"[^>]* value="([^"]*)"', fragment)
    }


def form_data(host: Host, **overrides: object) -> dict[str, object]:
    """Valid values for a new deployment of the ``alpha`` stack."""
    values: dict[str, object] = {
        "name": "alpha stack",
        "root": str(host.root / "alpha"),
        "dist_missing_policy": "block",
        "drift_policy": "halt",
        "missing_target_policy": "create",
        "file_mode": "0600",
        "file_owner": "",
        "file_group": "",
        "compose_project": "",
        "compose_file": ["compose.yaml"],
        "import_next": "on",
    }
    return {**values, **overrides}


async def deployment_named(web_app: FastAPI, name: str) -> Deployment | None:
    """The deployment with a name, None when there is none."""
    async with web_app.state.session_factory() as db:
        found: Deployment | None = await db.scalar(
            select(Deployment).where(Deployment.name == name)
        )
    return found


async def audit_details(web_app: FastAPI, action: str) -> list[dict[str, Any]]:
    """The details of every audit entry of an action, oldest first."""
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry.details).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return [dict(details) for details in rows.scalars()]


async def test_the_new_page_switches_between_the_stacks_and_the_folder_form(
    web_client: httpx.AsyncClient, host: Host
) -> None:
    await sign_in(web_client, "root")
    stacks = (await web_client.get(host.new_url)).text
    assert 'aria-current="page"' in stacks
    assert re.search(r'aria-current="page"[^>]*>Detected stacks<', stacks)
    assert f'href="{host.new_url}?source=folder"' in stacks
    assert 'id="detected-stacks"' in stacks and 'aria-busy="true"' in stacks
    assert f'hx-get="{host.new_url}/stacks"' in stacks and 'hx-trigger="load"' in stacks
    assert 'id="stack-form"' in stacks and 'id="deployment-form"' not in stacks
    assert '<dialog id="path-picker"' in stacks

    folder = (await web_client.get(f"{host.new_url}?source=folder")).text
    assert re.search(r'aria-current="page"[^>]*>Enter the folder<', folder)
    assert 'id="deployment-form"' in folder and 'id="detected-stacks"' not in folder
    assert 'name="compose_project"' in folder
    assert 'name="compose_file"' not in folder and 'name="import_next"' not in folder
    assert 'id="detected-stacks"' in (await web_client.get(f"{host.new_url}?source=x")).text


async def test_the_stacks_fragment_lists_docker_and_folder_finds_with_their_actions(
    web_client: httpx.AsyncClient, host: Host, fake: FakeDocker, web_app: FastAPI, tmp_path: Path
) -> None:
    beta = host.root / "beta"
    outside = make_stack(tmp_path / "elsewhere")
    answer(
        fake,
        [
            project("beta-stack", [beta / name for name in BETA_FILES]),
            project("away", [outside / "compose.yaml"], "exited(1)"),
        ],
        {"beta-stack": str(beta), "away": str(outside)},
    )
    async with web_app.state.session_factory() as db:
        owner = await db.get(Project, host.fancy)
        assert owner is not None
        db.add(
            Deployment(
                project_id=owner.id, name="gamma", root_path=str(make_stack(host.root / "gamma"))
            )
        )
        await db.commit()
        gamma_query = select(Deployment.id).where(Deployment.name == "gamma")
        used = (await db.execute(gamma_query)).scalar_one()
    await sign_in(web_client, "root")
    response = await web_client.get(f"{host.new_url}/stacks", headers=HTMX)
    page = response.text
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert page.lstrip().startswith("<section") and 'data-fragment="detected-stacks"' in page
    assert 'aria-busy="false"' in page and 'id="detected-stacks-scan"' in page
    assert f'hx-get="{host.new_url}/stacks/loading"' in page
    assert "Docker was not used" not in page

    folder_only = row_of(page, host.root / "alpha")
    assert "Folder" in text_of(folder_only) and "Not in Docker" in text_of(folder_only)
    assert use_query(folder_only) == {
        "folder": [str(host.root / "alpha")],
        "compose_file": ["compose.yaml"],
    }

    docker_stack = row_of(page, beta)
    assert "beta-stack" in docker_stack and "2 running" in text_of(docker_stack)
    assert "Docker and folder" in text_of(docker_stack)
    assert use_query(docker_stack) == {
        "folder": [str(beta)],
        "project": ["beta-stack"],
        "compose_file": ["docker-compose.yml", "docker-compose.override.yml"],
    }

    taken = row_of(page, host.root / "gamma")
    assert f'href="/deployments/{used}"' in taken and "Used by gamma" in text_of(taken)
    assert 'data-row-action="use"' not in taken

    away = row_of(page, outside)
    assert "The folder is outside the allowed paths." in text_of(away)
    assert 'data-row-action="use"' not in away and "0 of 1 running" in text_of(away)
    assert page.count('data-fragment="stack-item"') == page.count('data-fragment="stack-row"')


async def test_without_a_usable_docker_the_reason_is_shown_and_the_folders_are_still_listed(
    web_client: httpx.AsyncClient, host: Host, fake: FakeDocker
) -> None:
    fake.switch("no-daemon")
    await sign_in(web_client, "root")
    page = (await web_client.get(f"{host.new_url}/stacks")).text
    assert "Docker was not used:" in text_of(page)
    assert "Only the allowed paths were searched." in text_of(page)
    assert f'data-folder="{host.root / "alpha"}"' in page


async def test_scan_again_answers_with_the_loading_state_that_asks_for_the_list(
    web_client: httpx.AsyncClient, host: Host
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"{host.new_url}/stacks/loading", headers=HTMX)
    assert response.status_code == 200
    assert 'aria-busy="true"' in response.text and 'hx-trigger="load"' in response.text
    assert f'hx-get="{host.new_url}/stacks"' in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_use_fills_the_form_from_a_docker_stack_with_a_read_only_project(
    web_client: httpx.AsyncClient, host: Host
) -> None:
    await sign_in(web_client, "root")
    beta = host.root / "beta"
    response = await web_client.get(
        f"{host.new_url}/use",
        params=[
            ("folder", str(beta)),
            ("project", "beta-stack"),
            ("compose_file", "docker-compose.yml"),
            ("compose_file", "docker-compose.override.yml"),
        ],
        headers=HTMX,
    )
    page = response.text
    assert response.status_code == 200 and page.lstrip().startswith("<form")
    values = form_values(page)
    assert values["name"] == "beta-stack" and values["root"] == str(beta)
    assert values["compose_project"] == "beta-stack" and values["project_locked"] == "true"
    assert re.findall(r'name="compose_file" value="([^"]+)"', page) == [
        "docker-compose.yml",
        "docker-compose.override.yml",
    ]
    assert re.search(r'<input[^>]* name="compose_project"[^>]* readonly', page)
    assert re.search(r'name="import_next"[^>]* checked', page)
    assert "Read from Docker." in page and "Import services and environment files next" in page
    assert "Exists, mounted and writable" in page
    assert 'value="block" checked' in page and 'value="0600"' in page
    assert f'hx-post="{host.create_url}"' in page


async def test_use_of_a_folder_find_leaves_the_project_empty_and_editable(
    web_client: httpx.AsyncClient, host: Host
) -> None:
    await sign_in(web_client, "root")
    page = (
        await web_client.get(
            f"{host.new_url}/use",
            params=[("folder", str(host.root / "alpha")), ("compose_file", "compose.yaml")],
        )
    ).text
    values = form_values(page)
    assert values["name"] == "alpha" and values["compose_project"] == ""
    assert "project_locked" not in values
    assert not re.search(r'<input[^>]* name="compose_project"[^>]* readonly', page)
    assert re.search(r'name="import_next"[^>]* checked', page)


async def test_use_of_a_folder_with_an_unusable_name_leaves_the_name_empty(
    web_client: httpx.AsyncClient, host: Host
) -> None:
    folder = make_stack(host.root / "+++")
    await sign_in(web_client, "root")
    page = (await web_client.get(f"{host.new_url}/use", params={"folder": str(folder)})).text
    assert form_values(page)["name"] == ""
    assert 'name="compose_file"' not in page and 'name="import_next"' not in page


async def test_use_shows_what_is_wrong_with_the_folder_or_the_files(
    web_client: httpx.AsyncClient, host: Host, tmp_path: Path
) -> None:
    await sign_in(web_client, "root")
    outside = (await web_client.get(f"{host.new_url}/use", params={"folder": str(tmp_path)})).text
    assert "Path is outside the allowed paths" in outside
    missing = (
        await web_client.get(
            f"{host.new_url}/use",
            params=[("folder", str(host.root / "alpha")), ("compose_file", "nope.yaml")],
        )
    ).text
    assert "does not exist" in missing and 'id="compose-files-e"' in missing


async def test_create_from_a_stack_links_the_files_stores_the_project_and_opens_the_import(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    beta = host.root / "beta"
    response = await post(
        web_client,
        host.create_url,
        form_data(
            host,
            name="beta stack",
            root=str(beta),
            compose_project="beta-stack",
            compose_file=["docker-compose.yml", "docker-compose.override.yml"],
        ),
    )
    created = await deployment_named(web_app, "beta stack")
    assert response.status_code == 200 and created is not None
    assert created.compose_project == "beta-stack"
    assert created.compose_files == ["docker-compose.yml", "docker-compose.override.yml"]
    target = urlsplit(response.headers["hx-redirect"])
    assert target.path == f"/deployments/{created.id}/import-compose"
    assert parse_qs(target.query) == {
        "compose_file": ["docker-compose.yml", "docker-compose.override.yml"]
    }
    (create,) = await audit_details(web_app, "deployment.create")
    assert create["compose_project"] == "beta-stack"
    (link,) = await audit_details(web_app, "deployment.compose_link")
    assert link["compose_files"] == ["docker-compose.yml", "docker-compose.override.yml"]
    page = await web_client.get(response.headers["hx-redirect"])
    assert page.status_code == 200
    assert re.search(r'value="docker-compose\.yml"[^>]* checked', page.text)


async def test_create_without_the_import_lands_on_the_files_tab_with_the_saved_toast(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    data = form_data(host)
    del data["import_next"]
    response = await post(web_client, host.create_url, data)
    created = await deployment_named(web_app, "alpha stack")
    assert created is not None and created.compose_files == ["compose.yaml"]
    assert response.headers["hx-redirect"] == f"/deployments/{created.id}/files?saved=true"
    page = await web_client.get(response.headers["hx-redirect"])
    assert "Deployment saved" in page.text
    assert "Deployment saved" not in (await web_client.get(f"/deployments/{created.id}/files")).text
    assert len(await audit_details(web_app, "deployment.compose_link")) == 1


async def test_create_from_the_folder_form_links_nothing_and_lands_on_the_files_tab(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    data = form_data(host, compose_file=[])
    del data["import_next"]
    response = await post(web_client, host.create_url, data)
    created = await deployment_named(web_app, "alpha stack")
    assert created is not None and created.compose_files is None and created.compose_project is None
    assert response.headers["hx-redirect"] == f"/deployments/{created.id}/files?saved=true"
    assert await audit_details(web_app, "deployment.compose_link") == []
    (create,) = await audit_details(web_app, "deployment.create")
    assert create["compose_project"] is None


@pytest.mark.parametrize("bad", ["Upper", "-start", "has space", "a" * 64, "dot.name"])
async def test_a_compose_project_that_breaks_the_rule_is_refused_and_nothing_is_created(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI, bad: str
) -> None:
    await sign_in(web_client, "root")
    response = await post(web_client, host.create_url, form_data(host, compose_project=bad))
    assert response.status_code == 422
    assert "Use 1 to 63 lowercase letters, numbers, dashes and underscores" in response.text
    assert 'id="compose_project-e"' in response.text
    assert await deployment_named(web_app, "alpha stack") is None
    assert await audit_details(web_app, "deployment.create") == []


async def test_a_refused_create_keeps_the_stack_fields_and_checks_the_files_against_the_root(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    """The root was edited after Use: the chips name files the new root does not hold."""
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        host.create_url,
        form_data(
            host,
            root=str(host.root / "beta"),
            compose_project="alpha",
            project_locked="true",
        ),
    )
    page = response.text
    assert response.status_code == 422 and page.lstrip().startswith("<form")
    assert "The Compose file compose.yaml does not exist." in page
    assert re.findall(r'name="compose_file" value="([^"]+)"', page) == ["compose.yaml"]
    assert re.search(r'<input[^>]* name="compose_project"[^>]* readonly', page)
    assert re.search(r'name="import_next"[^>]* checked', page)
    assert "Exists, mounted and writable" in page
    assert await deployment_named(web_app, "alpha stack") is None
    assert await audit_details(web_app, "deployment.compose_link") == []


@pytest.mark.parametrize("path", ["../escape.yaml", "/etc/passwd", "compose.yaml/../../x"])
async def test_posted_compose_files_outside_the_root_are_refused(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI, path: str
) -> None:
    await sign_in(web_client, "root")
    response = await post(web_client, host.create_url, form_data(host, compose_file=[path]))
    assert response.status_code == 422 and 'id="compose-files-e"' in response.text
    assert await deployment_named(web_app, "alpha stack") is None


async def test_the_files_are_not_checked_while_the_root_is_refused(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(web_client, host.create_url, form_data(host, root="/etc"))
    assert response.status_code == 422
    assert "Path is outside the allowed paths" in response.text
    assert 'id="compose-files-e"' not in response.text
    assert re.findall(r'name="compose_file" value="([^"]+)"', response.text) == ["compose.yaml"]


@pytest.mark.parametrize("user", ["admin", "viewer"])
async def test_admins_and_viewers_are_refused_on_every_stack_route(
    web_client: httpx.AsyncClient, host: Host, user: str
) -> None:
    await sign_in(web_client, user)
    for url in (
        host.new_url,
        f"{host.new_url}/stacks",
        f"{host.new_url}/stacks/loading",
        f"{host.new_url}/use?folder={host.root}",
        f"/deployments/{host.prod}/edit/compose-project-suggestion",
    ):
        assert (await web_client.get(url)).status_code == 403, url
    assert (await post(web_client, host.create_url, form_data(host))).status_code == 403


async def test_signed_out_requests_are_refused(web_client: httpx.AsyncClient, host: Host) -> None:
    for url in (
        f"{host.new_url}/stacks",
        f"{host.new_url}/stacks/loading",
        f"{host.new_url}/use",
        f"/deployments/{host.prod}/edit/compose-project-suggestion",
    ):
        response = await web_client.get(url, follow_redirects=False)
        assert response.status_code in (303, 401), url


async def test_the_edit_form_has_the_compose_project_field_and_saves_it_with_an_audit(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{host.prod}/edit")).text
    assert 'id="compose_project"' in page and "Compose project (optional)" in page
    assert not re.search(r'<input[^>]* name="compose_project"[^>]* readonly', page)
    values = {
        **form_data(host, name="prod", root=str(host.root / "alpha"), compose_project="prod-stack"),
    }
    del values["compose_file"], values["import_next"]
    saved = await post(web_client, f"/deployments/{host.prod}/edit", values)
    assert saved.headers["hx-redirect"] == f"/deployments/{host.prod}/settings?saved=true"
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, host.prod)
    assert stored is not None and stored.compose_project == "prod-stack"
    (entry,) = await audit_details(web_app, "deployment.update")
    assert entry["compose_project"] == "prod-stack"
    assert "compose_project" in entry["changed"]
    assert entry["previous_compose_project"] is None
    settings = (await web_client.get(f"/deployments/{host.prod}/settings")).text
    assert "prod-stack" in settings and "Not set" not in settings

    cleared = await post(
        web_client, f"/deployments/{host.prod}/edit", {**values, "compose_project": " "}
    )
    assert cleared.status_code == 200
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, host.prod)
    assert stored is not None and stored.compose_project is None
    page = (await web_client.get(f"/deployments/{host.prod}/settings")).text
    assert "Not set" in page


async def test_the_edit_form_refuses_a_bad_project_with_the_message(
    web_client: httpx.AsyncClient, host: Host
) -> None:
    await sign_in(web_client, "root")
    values = form_data(host, name="prod", compose_project="Bad Name")
    del values["compose_file"], values["import_next"]
    response = await post(web_client, f"/deployments/{host.prod}/edit", values)
    assert response.status_code == 422
    assert "Use 1 to 63 lowercase letters, numbers, dashes and underscores" in response.text
    assert 'value="Bad Name"' in response.text


async def test_the_edit_form_asks_for_the_suggestion_only_while_the_project_is_empty(
    web_client: httpx.AsyncClient, host: Host, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    empty = (await web_client.get(f"/deployments/{host.prod}/edit")).text
    address = f"/deployments/{host.prod}/edit/compose-project-suggestion"
    assert f'hx-get="{address}"' in empty and 'hx-trigger="load"' in empty
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, host.prod)
        assert stored is not None
        stored.compose_project = "set-already"
        await db.commit()
    filled = (await web_client.get(f"/deployments/{host.prod}/edit")).text
    assert "compose-project-suggestion" not in filled and 'value="set-already"' in filled


async def point_prod_at(web_app: FastAPI, deployment: int, root: Path) -> None:
    """Move a deployment's root to a folder."""
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, deployment)
        assert stored is not None
        stored.root_path = str(root)
        await db.commit()


async def test_the_suggestion_offers_the_one_project_that_has_the_root_as_its_folder(
    web_client: httpx.AsyncClient, host: Host, fake: FakeDocker, web_app: FastAPI
) -> None:
    alpha = host.root / "alpha"
    await point_prod_at(web_app, host.prod, alpha)
    answer(
        fake,
        [
            project("alpha-live", [alpha / "compose.yaml"]),
            project("other", [host.root / "beta" / "x.yml"]),
        ],
        {"alpha-live": str(alpha), "other": str(host.root / "beta")},
    )
    await sign_in(web_client, "root")
    response = await web_client.get(
        f"/deployments/{host.prod}/edit/compose-project-suggestion", headers=HTMX
    )
    page = response.text
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "Fill from the running containers" in page and "alpha-live" in page
    assert 'data-js="fill-field"' in page and 'data-target="compose_project"' in page
    assert 'data-value="alpha-live"' in page


async def test_the_suggestion_is_empty_with_none_or_several_projects_or_no_docker(
    web_client: httpx.AsyncClient, host: Host, fake: FakeDocker, web_app: FastAPI
) -> None:
    alpha = host.root / "alpha"
    await point_prod_at(web_app, host.prod, alpha)
    await sign_in(web_client, "root")
    url = f"/deployments/{host.prod}/edit/compose-project-suggestion"

    async def offered() -> bool:
        page = (await web_client.get(url)).text
        assert 'data-fragment="compose-project-suggestion"' in page
        return "Fill from the running containers" in page

    assert not await offered()
    answer(
        fake,
        [project("one", [alpha / "compose.yaml"]), project("two", [alpha / "compose.yaml"])],
        {"one": str(alpha), "two": str(alpha)},
    )
    assert not await offered()
    answer(fake, [project("one", [alpha / "compose.yaml"])], {"one": str(alpha)})
    assert await offered()
    fake.switch("no-daemon")
    web_app.state.docker_cli = fake.cli()
    assert not await offered()


async def test_the_suggestion_is_not_offered_to_a_deployment_that_has_a_project_or_a_root_outside(
    web_client: httpx.AsyncClient, host: Host, fake: FakeDocker, web_app: FastAPI, tmp_path: Path
) -> None:
    alpha = host.root / "alpha"
    answer(fake, [project("one", [alpha / "compose.yaml"])], {"one": str(alpha)})
    await point_prod_at(web_app, host.prod, alpha)
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, host.prod)
        assert stored is not None
        stored.compose_project = "mine"
        await db.commit()
    await sign_in(web_client, "root")
    url = f"/deployments/{host.prod}/edit/compose-project-suggestion"
    assert "Fill from" not in (await web_client.get(url)).text
    await point_prod_at(web_app, host.prod, make_stack(tmp_path / "away"))
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, host.prod)
        assert stored is not None
        stored.compose_project = None
        await db.commit()
    away = tmp_path / "away"
    answer(fake, [project("one", [away / "compose.yaml"])], {"one": str(away)})
    assert "Fill from" not in (await web_client.get(url)).text
