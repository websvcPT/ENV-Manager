"""Deployment form, root path validation and the path picker on the seeded application."""

import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AllowedPath, AuditEntry, Deployment, Project
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX, breadcrumb_of, post


@dataclass(frozen=True)
class Layout:
    """A writable allowed host directory with folders, and ids of the seeded rows."""

    host: Path
    fancy: int
    prod: int
    legacy: int


@pytest.fixture
async def layout(web_app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    """Allow a temporary host directory (treated as a mount) next to the seeded paths."""
    host = (tmp_path / "vhosts").resolve()
    for name in ("alpha", "beta", "alpha/inner"):
        (host / name).mkdir(parents=True)
    (host / "note.txt").write_text("plain file")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (host / "escape").symlink_to(outside)
    monkeypatch.setattr("app.services.paths.read_mount_points", lambda *args: [host])
    async with web_app.state.session_factory() as db:
        db.add(AllowedPath(path=str(host)))
        await db.commit()
        fancy_query = select(Project.id).where(Project.name == "Fancy Proj. Name")
        fancy = (await db.execute(fancy_query)).scalar_one()
        deployments = dict((await db.execute(select(Deployment.name, Deployment.id))).all())
    return Layout(host=host, fancy=fancy, prod=deployments["prod"], legacy=deployments["legacy"])


def form_data(layout: Layout, **overrides: str) -> dict[str, str]:
    """Valid form values for a new deployment under the host directory."""
    values = {
        "name": "gamma",
        "root": str(layout.host / "alpha"),
        "dist_missing_policy": "block",
        "drift_policy": "halt",
        "missing_target_policy": "create",
        "file_mode": "0600",
        "file_owner": "",
        "file_group": "",
    }
    return {**values, **overrides}


def check_items(fragment: str) -> list[str]:
    """Visible text of the validation lines."""
    items = re.findall(r"<li.*?</li>", fragment, re.DOTALL)
    return [re.sub(r"<[^>]+>", "", item) for item in items]


async def test_new_page_has_the_form_and_the_picker(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/projects/{layout.fancy}/deployments/new")
    page = response.text
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert ">New deployment</h1>" in page and ">Superadmin</span>" in page
    assert "Post-deploy commands are configured per service, not here." in page
    assert f'hx-post="/projects/{layout.fancy}/deployments"' in page
    assert 'id="deployment-form"' in page and 'data-fragment="deployment-form"' in page
    assert page.count('data-fragment="form-section"') == 3
    for field_id in ("name", "root", "mode", "owner", "group"):
        assert f'id="{field_id}"' in page
    assert 'hx-get="/paths/validate"' in page
    assert 'hx-trigger="input changed delay:400ms from:#root"' in page
    assert "Choose a path under an allowed path. It is validated as you type." in page
    assert 'value="block" checked' in page and 'value="halt" checked' in page
    assert 'value="create" checked' in page and 'value="0600"' in page
    assert ">Create deployment</button>" in page
    assert f'href="/projects/{layout.fancy}"' in page and ">Cancel</a>" in page
    assert '<dialog id="path-picker"' in page and str(layout.host) in page
    assert 'data-js="pick-path"' in page
    assert [(crumb.label, crumb.type) for crumb in breadcrumb_of(page)] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("New deployment", None),
    ]


async def test_edit_page_is_prefilled_and_shows_the_root_checks(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    edit_page = await web_client.get(f"/deployments/{layout.legacy}/edit")
    assert ">Edit legacy</h1>" in edit_page.text
    assert 'value="legacy"' in edit_page.text and 'value="/srv/old/legacy"' in edit_page.text
    assert "outside the allowed paths" in edit_page.text
    assert f'hx-post="/deployments/{layout.legacy}/edit"' in edit_page.text
    assert ">Save deployment</button>" in edit_page.text
    assert f'href="/deployments/{layout.legacy}"' in edit_page.text


@pytest.mark.parametrize("user", ["admin", "viewer"])
async def test_admins_and_viewers_get_403_everywhere(
    web_client: httpx.AsyncClient, layout: Layout, user: str
) -> None:
    await sign_in(web_client, user)
    new_url = f"/projects/{layout.fancy}/deployments/new"
    assert (await web_client.get(new_url)).status_code == 403
    assert (await web_client.get(f"/deployments/{layout.prod}/edit")).status_code == 403
    assert (await web_client.get("/paths/validate?root=/etc")).status_code == 403
    assert (await web_client.get("/paths/browse")).status_code == 403
    created = await post(web_client, f"/projects/{layout.fancy}/deployments", form_data(layout))
    assert created.status_code == 403
    saved = await post(web_client, f"/deployments/{layout.prod}/edit", form_data(layout))
    assert saved.status_code == 403


async def test_outside_the_scope_is_404_and_paths_stay_closed(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "ungranted_admin")
    assert (await web_client.get(f"/projects/{layout.fancy}/deployments/new")).status_code == 404
    assert (await web_client.get(f"/deployments/{layout.prod}/edit")).status_code == 404
    assert (await web_client.get("/paths/validate?root=/etc")).status_code == 403
    assert (await web_client.get("/paths/browse")).status_code == 403
    async with web_app.state.session_factory() as db:
        names = set((await db.execute(select(Deployment.name))).scalars())
    assert "gamma" not in names


async def test_signed_out_requests_are_refused(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    for url in (
        f"/projects/{layout.fancy}/deployments/new",
        f"/deployments/{layout.prod}/edit",
        "/paths/validate?root=/etc",
        "/paths/browse",
    ):
        response = await web_client.get(url, follow_redirects=False)
        assert response.status_code in (303, 401), url


async def test_validate_fragment_states(web_client: httpx.AsyncClient, layout: Layout) -> None:
    await sign_in(web_client, "root")
    empty = await web_client.get("/paths/validate?root=", headers=HTMX)
    assert check_items(empty.text) == [
        "Choose a path under an allowed path. It is validated as you type."
    ]
    inside = await web_client.get("/paths/validate", params={"root": str(layout.host / "alpha")})
    assert check_items(inside.text) == [
        f"Inside allowed path {layout.host}",
        "Exists, mounted and writable",
    ]
    assert "text-ok-fg" in inside.text
    assert inside.headers["cache-control"] == "no-store"
    missing = await web_client.get("/paths/validate", params={"root": str(layout.host / "nope")})
    assert check_items(missing.text)[1:] == ["Path does not exist."]
    plain = await web_client.get("/paths/validate", params={"root": str(layout.host / "note.txt")})
    assert check_items(plain.text)[1:] == ["Path is not a directory."]


@pytest.mark.parametrize("probe", ["/etc", "/definitely/not/there", "escape"])
async def test_outside_paths_answer_with_a_single_error(
    web_client: httpx.AsyncClient, layout: Layout, probe: str
) -> None:
    await sign_in(web_client, "root")
    root = str(layout.host / probe) if probe == "escape" else probe
    response = await web_client.get("/paths/validate", params={"root": root})
    items = check_items(response.text)
    assert len(items) == 1 and items[0].startswith("Path is outside the allowed paths (")
    assert "exist" not in items[0] and "text-err-fg" in response.text


async def test_create_redirects_and_audits(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/projects/{layout.fancy}/deployments",
        form_data(layout, drift_policy="overwrite", file_mode="640", file_owner="www-data"),
    )
    assert response.status_code == 200
    async with web_app.state.session_factory() as db:
        created = (
            await db.execute(select(Deployment).where(Deployment.name == "gamma"))
        ).scalar_one()
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "deployment.create"))
        ).scalar_one()
    assert response.headers["hx-redirect"] == f"/deployments/{created.id}"
    assert created.project_id == layout.fancy and created.root_path == str(layout.host / "alpha")
    assert (created.file_mode, created.file_owner, created.drift_policy.value) == (
        "0640",
        "www-data",
        "overwrite",
    )
    assert entry.deployment_id == created.id and entry.actor_name == "root"
    page = await web_client.get(f"/deployments/{created.id}")
    assert page.status_code == 200 and "0640, www-data:" in page.text


async def test_refused_create_swaps_the_form_with_every_message(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/projects/{layout.fancy}/deployments",
        form_data(layout, name="prod", root="/etc", file_mode="12"),
    )
    page = response.text
    assert response.status_code == 422
    assert page.lstrip().startswith("<form") and 'id="deployment-form"' in page
    assert "<html" not in page
    assert "A deployment named prod already exists in this project." in page
    assert "Path is outside the allowed paths" in page and 'aria-invalid="true"' in page
    assert "Use an octal mode such as 0600." in page
    assert 'value="prod"' in page and 'value="/etc"' in page
    assert "Choose a path under an allowed path" not in page
    async with web_app.state.session_factory() as db:
        assert (
            await db.execute(select(AuditEntry.id).where(AuditEntry.action == "deployment.create"))
        ).first() is None


async def test_refused_empty_root_shows_a_message_under_the_field(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/projects/{layout.fancy}/deployments", form_data(layout, root="")
    )
    assert response.status_code == 422
    assert "Enter a root path." in response.text


async def test_edit_saves_and_audits_then_shows_the_toast(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/deployments/{layout.prod}/edit",
        form_data(layout, name="prod", root=str(layout.host / "beta"), file_group="deploy"),
    )
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/deployments/{layout.prod}?saved=true"
    async with web_app.state.session_factory() as db:
        stored = await db.get(Deployment, layout.prod)
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "deployment.update"))
        ).scalar_one()
    assert stored is not None
    assert (stored.root_path, stored.file_group) == (str(layout.host / "beta"), "deploy")
    assert entry.details["changed"] == ["root_path", "file_owner", "file_group"]
    page = await web_client.get(f"/deployments/{layout.prod}?saved=true")
    assert "Deployment saved" in page.text
    assert "Deployment saved" not in (await web_client.get(f"/deployments/{layout.prod}")).text


async def test_legacy_deployment_must_be_moved_inside_the_list_to_save(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    """A root that is outside the allowed paths is refused even when it did not change."""
    await sign_in(web_client, "root")
    unchanged = await post(
        web_client,
        f"/deployments/{layout.legacy}/edit",
        form_data(layout, name="legacy", root="/srv/old/legacy"),
    )
    assert unchanged.status_code == 422 and "Path is outside the allowed paths" in unchanged.text
    moved = await post(
        web_client,
        f"/deployments/{layout.legacy}/edit",
        form_data(layout, name="legacy", root=str(layout.host / "beta")),
    )
    assert moved.status_code == 200


async def test_picker_lists_roots_with_folders_then_one_directory(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    top = await web_client.get("/paths/browse", headers=HTMX)
    assert top.status_code == 200 and 'data-fragment="path-picker"' in top.text
    assert top.headers["cache-control"] == "no-store"
    assert str(layout.host) in top.text and "allowed root" in top.text
    assert str(layout.host / "alpha") in top.text and str(layout.host / "escape") not in top.text
    assert "note.txt" not in top.text
    assert "disabled" in top.text
    inside = await web_client.get("/paths/browse", params={"path": str(layout.host / "alpha")})
    assert str(layout.host / "alpha" / "inner") in inside.text
    assert f'data-path="{layout.host / "alpha"}"' in inside.text
    assert "Up one level" in inside.text and "disabled" not in inside.text
    at_root = await web_client.get("/paths/browse", params={"path": str(layout.host)})
    assert "All allowed paths" in at_root.text
    empty = await web_client.get(
        "/paths/browse", params={"path": str(layout.host / "alpha" / "inner")}
    )
    assert "No folders inside this one." in empty.text


async def test_picker_sorts_folders_ignoring_case_and_offers_a_filter(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    for name in ("Zeta", "beta", "Alpha2", "gamma"):
        (layout.host / "alpha" / name).mkdir()
    await sign_in(web_client, "root")
    page = (await web_client.get("/paths/browse", params={"path": str(layout.host / "alpha")})).text
    names = re.findall(r'<li data-path-entry data-name="([^"]+)">', page)
    assert names == ["alpha2", "beta", "gamma", "inner", "zeta"]
    assert 'data-js="path-filter"' in page and 'aria-controls="path-picker-list"' in page
    assert 'id="path-filter-count"' in page and "5 folders shown" in page
    assert re.search(r"<li data-path-filter-empty hidden[^>]*>No folder matches\.</li>", page)
    empty = await web_client.get(
        "/paths/browse", params={"path": str(layout.host / "alpha" / "inner")}
    )
    assert 'data-js="path-filter"' not in empty.text


@pytest.mark.parametrize("probe", ["/etc", "/definitely/not/there", "escape", "note.txt", "../x"])
async def test_picker_never_opens_anything_outside_the_list(
    web_client: httpx.AsyncClient, layout: Layout, probe: str
) -> None:
    await sign_in(web_client, "root")
    path = str(layout.host / probe) if probe in {"escape", "note.txt"} else probe
    response = await web_client.get("/paths/browse", params={"path": path})
    assert response.status_code == 422
    assert "That folder cannot be opened." in response.text
    assert str(layout.host) in response.text
    assert path not in response.text
