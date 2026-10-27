"""The Docker Compose import screens: who may use them, each step and what a forged field does."""

import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import (
    DEPLOYMENT_FILES_NAME,
    AllowedPath,
    AuditEntry,
    Deployment,
    Environment,
    HookType,
    Project,
    Service,
    ServiceHook,
)
from app.services.compose_import import (
    COMMAND_STORED_MESSAGE,
    DIST_NEEDED_MESSAGE,
    HAS_SERVICES_MESSAGE,
)
from app.services.compose_scan import IN_FILES_REASON
from app.services.file_types import NOT_PEM_MESSAGE
from app.tests.compose_helpers import GEOTRAVEL, PROJECT, mounted_cli, write_project
from app.tests.conftest import sign_in
from app.tests.web_helpers import (
    assert_never_cached,
    breadcrumb_of,
    item_header_of,
    main_of,
    post,
)

OVERRIDE = "services:\n  web:\n    environment:\n      MODE: test\n"
SUB_COMPOSE = "services:\n  tool:\n    image: t\n"
EXTRA = {"compose.override.yaml": OVERRIDE, "sub/compose.yaml": SUB_COMPOSE}
SERVICES = ["api", "web", "worker-1"]
FILE_KEYS = [
    ".env.web",
    ".env.optional",
    "conf/nginx.conf",
    "certs/server.key",
    "conf/app.cfg",
    ".env.shared",
    ".env.api",
]
OWNERS = {
    ".env.web": "web",
    ".env.optional": "web",
    "conf/nginx.conf": "web",
    "certs/server.key": "web",
    "conf/app.cfg": "web",
    ".env.shared": "api",
    ".env.api": "api",
}
DIST = {".env.web": ".env.web.example", ".env.api": ".env.api-dist"}
CONFIRM_TITLE = "Import services and files"


@dataclass(frozen=True)
class Layout:
    """The host folder, a deployment with no services on a real Compose project, one that has
    services, one whose folder is outside the allowed paths, and the project's real root."""

    root: Path
    empty: int
    seeded: int
    outside: int


@pytest.fixture
async def layout(web_app: FastAPI, tmp_path: Path) -> Layout:
    """Allow a temporary folder and put a deployment without services on the fixture project."""
    host = (tmp_path / "vhosts").resolve()
    root = host / "imported"
    root.mkdir(parents=True)
    write_project(root, {**PROJECT, **EXTRA})
    async with web_app.state.session_factory() as db:
        db.add(AllowedPath(path=str(host)))
        project = (
            await db.execute(select(Project.id).where(Project.name == "Fancy Proj. Name"))
        ).scalar_one()
        empty = Deployment(project_id=project, name="imported", root_path=str(root))
        outside = Deployment(project_id=project, name="elsewhere", root_path="/srv/not-allowed/x")
        db.add_all([empty, outside])
        seeded = (
            await db.execute(select(Deployment.id).where(Deployment.name == "prod"))
        ).scalar_one()
        await db.commit()
        return Layout(root, empty.id, seeded, outside.id)


def base(layout: Layout, tail: str = "", deployment: int | None = None) -> str:
    """The address of an import route of a deployment (the empty one by default)."""
    return f"/deployments/{deployment or layout.empty}/import-compose{tail}"


def form_of(
    services: list[str] | None = None,
    keys: list[str] | None = None,
    *,
    owners: dict[str, str] | None = None,
    dist: dict[str, str] | None = None,
    compose_files: list[str] | None = None,
) -> dict[str, object]:
    """The fields of a confirm or import post: the files, services and the choice per file."""
    keys = FILE_KEYS if keys is None else keys
    fields: dict[str, object] = {
        "compose_file": compose_files or ["compose.yaml"],
        "service": SERVICES if services is None else services,
        "file": keys,
    }
    for key in keys:
        fields[f"owner:{key}"] = (owners or OWNERS).get(key, "")
        fields[f"type:{key}"] = "cert" if key == "certs/server.key" else "env"
    for key, path in (DIST if dist is None else dist).items():
        if key in keys:
            fields[f"dist:{key}"] = path
    return fields


async def counts(web_app: FastAPI) -> tuple[int, int, int]:
    """How many services, environments and import audit entries exist."""
    async with web_app.state.session_factory() as db:
        services = (await db.execute(select(func.count(Service.id)))).scalar_one()
        environments = (await db.execute(select(func.count(Environment.id)))).scalar_one()
        entries = (
            await db.execute(
                select(func.count(AuditEntry.id)).where(
                    AuditEntry.action == "deployment.import_compose"
                )
            )
        ).scalar_one()
    return services, environments, entries


def row_of(page: str, key: str, fragment: str = "import-file-row") -> str:
    """The markup of the file row with this key."""
    opening = rf'<tr data-fragment="{fragment}" data-file="{re.escape(key)}"'
    found = re.search(opening + ".*?</tr>", page, re.DOTALL)
    assert found, key
    return found.group(0)


def own_row_of(page: str, key: str) -> str:
    """The markup of the deployment file row with this key."""
    return row_of(page, key, "import-deployment-file-row")


def hidden_fields(page: str) -> dict[str, list[str]]:
    """The hidden inputs of a fragment by name."""
    fields: dict[str, list[str]] = {}
    for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page):
        fields.setdefault(name, []).append(value)
    return fields


async def test_the_action_is_offered_to_the_superadmin_while_there_are_no_services(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    empty = (await web_client.get(f"/deployments/{layout.empty}")).text
    assert f'href="{base(layout)}"' in empty and 'id="import-compose-open"' in empty
    assert f'href="/deployments/{layout.empty}/services/new"' in empty
    seeded = (await web_client.get(f"/deployments/{layout.seeded}")).text
    assert "import-compose" not in seeded
    outside = (await web_client.get(f"/deployments/{layout.outside}")).text
    assert "import-compose" not in outside


@pytest.mark.parametrize("user", ["admin", "viewer", "deployment_admin", "env_scoped_admin"])
async def test_other_groups_are_refused_everywhere_and_nothing_is_created(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout, user: str
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, user)
    page = (await web_client.get(f"/deployments/{layout.seeded}")).text
    assert "import-compose" not in page
    refused = {403, 404}
    assert (await web_client.get(base(layout))).status_code in refused
    for tail in ("/preview", "/confirm", ""):
        response = await post(web_client, base(layout, tail), form_of())
        assert response.status_code in refused, tail
    assert await counts(web_app) == before


async def test_a_user_outside_the_scope_gets_404_and_a_signed_out_request_is_refused(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "ungranted_admin")
    assert (await web_client.get(base(layout))).status_code == 404
    assert (await post(web_client, base(layout, "/confirm"), form_of())).status_code == 404
    web_client.cookies.clear()
    response = await web_client.get(base(layout), follow_redirects=False)
    assert response.status_code in (303, 401)
    assert (await post(web_client, base(layout, "/preview"), form_of())).status_code in (303, 401)


async def test_step_one_lists_the_compose_files_under_the_deployment_header(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(base(layout))
    page = response.text
    assert_never_cached(response)
    header = item_header_of(page)
    assert (header.type, header.title) == ("deployment", "Deployment: imported")
    assert dict(header.location) == {"root": f"Deployment path: {layout.root}"}
    assert [(crumb.label, crumb.type) for crumb in breadcrumb_of(page)] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("imported", "Deployment"),
        ("Import from Docker Compose", None),
    ]
    body = main_of(page)
    offers = re.findall(
        r'<input type="checkbox" name="compose_file" value="([^"]+)" id="cf-\d+"( checked)?', body
    )
    assert offers == [
        ("compose.yaml", " checked"),
        ("compose.override.yaml", " checked"),
        ("sub/compose.yaml", ""),
    ]
    assert 'name="extra_path"' in body and 'hx-post="' + base(layout, "/preview") in body
    assert "Working. Reading the Compose files can take up to 30 seconds." in body
    assert 'id="import-confirm"' in page and "Reading the Compose files again" in page


async def test_step_one_ticks_the_files_of_the_back_link_and_types_the_others(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    chosen = {"compose_file": ["sub/compose.yaml", "x/y.yaml"]}
    page = (await web_client.get(base(layout), params=chosen)).text
    ticked = re.findall(r'name="compose_file" value="([^"]+)" id="cf-\d+" checked', page)
    assert ticked == ["sub/compose.yaml"]
    assert re.search(r'name="extra_path" type="text" value="x/y.yaml"', page)


async def test_a_deployment_with_services_is_refused_at_every_step(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    page = await web_client.get(base(layout, deployment=layout.seeded))
    assert page.status_code == 409 and HAS_SERVICES_MESSAGE in page.text
    assert 'name="compose_file"' not in page.text
    for tail in ("/preview", "/confirm", ""):
        response = await post(web_client, base(layout, tail, layout.seeded), form_of())
        assert response.status_code == 409, tail
        assert HAS_SERVICES_MESSAGE in response.text
    assert await counts(web_app) == before


async def test_a_deployment_outside_the_allowed_paths_says_so(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    page = await web_client.get(base(layout, deployment=layout.outside))
    assert page.status_code == 200 and "The deployment is outside the allowed paths." in page.text
    response = await post(
        web_client,
        base(layout, "/preview", layout.outside),
        {"compose_file": ["compose.yaml"]},
    )
    assert response.status_code == 422
    assert "The deployment is outside the allowed paths." in response.text


async def test_the_preview_shows_a_row_per_service_and_per_file(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        base(layout, "/preview"),
        {"compose_file": ["compose.override.yaml", "compose.yaml"]},
    )
    page = response.text
    assert response.status_code == 200
    assert_never_cached(response)
    assert hidden_fields(page)["compose_file"] == ["compose.yaml", "compose.override.yaml"]
    services = re.findall(r'<tr data-fragment="import-service-row" data-service="([^"]+)"', page)
    assert sorted(services) == ["api", "web", "worker-1", "worker-1"]
    assert "Another service is already named worker-1." in page
    web = row_of(page, ".env.web")
    assert " checked" in web and 'value=".env.web.example"' in web and "Bind mount" not in web
    assert "env_file" in web and "data-compose-pick" in web
    shared = row_of(page, ".env.shared")
    options = re.findall(r'<option value="([^"]*)"', shared.split('name="owner:', 1)[1])
    assert "api" in options and "worker-1" in options
    assert "Choose a service" not in page and 'name="file" value=".env"' not in page
    own = re.findall(r'<tr data-fragment="import-deployment-file-row" data-file="([^"]+)"', page)
    assert own == [".env"]
    assert re.search(r'name="deployment_file" value="\.env"[^>]*checked', page)
    assert 'data-fragment="import-no-commands"' in page and 'name="run:' not in page
    guessed = row_of(page, "missing.conf")
    assert "Not on disk, looks like a file" in guessed and "Bind mount" in guessed
    assert " checked" not in guessed.split("</td>", 1)[0]
    optional = row_of(page, ".env.optional")
    assert "Optional" in optional and "Not on disk" in optional
    assert " checked" not in optional.split("</td>", 1)[0]
    outside = re.search(r'data-file="(/[^"]*outside\.env)"', page)
    assert outside is not None
    assert "Outside the deployment folder or the allowed paths." in row_of(page, outside.group(1))
    assert " disabled" in row_of(page, outside.group(1)).split("</td>", 1)[0]
    assert "Not offered" in page and "A folder, not a file." in page
    assert (
        "4 services and 10 files found (1 of the deployment itself), 1 cannot be created, "
        "1 file cannot be imported, 2 not offered."
    ) in page
    assert f'hx-post="{base(layout, "/confirm")}"' in page and 'data-js="compose-service"' in page
    href = re.search(r'href="([^"]*\?compose_file[^"]*)"', page)
    assert href is not None
    back = parse_qs(urlparse(html.unescape(href.group(1))).query)
    assert back["compose_file"] == ["compose.yaml", "compose.override.yaml"]
    assert await counts(web_app) == before


async def test_a_typed_path_is_scanned_after_the_ticked_files(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        base(layout, "/preview"),
        {"compose_file": ["compose.yaml"], "extra_path": "sub/compose.yaml"},
    )
    assert response.status_code == 200
    assert hidden_fields(response.text)["compose_file"] == ["compose.yaml", "sub/compose.yaml"]
    assert 'data-service="tool"' in response.text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "Choose at least one Compose file."),
        ({"compose_file": ["../elsewhere.yaml"]}, "Choose at least one Compose file."),
        (
            {"compose_file": ["compose.yaml"], "extra_path": "../escape.yaml"},
            "A Compose file path must be relative to the deployment folder.",
        ),
        (
            {"compose_file": ["compose.yaml"], "extra_path": "missing.yaml"},
            "The Compose file missing.yaml does not exist.",
        ),
    ],
)
async def test_a_refused_scan_draws_step_one_again_with_the_reason(
    web_client: httpx.AsyncClient, layout: Layout, data: dict[str, object], message: str
) -> None:
    """A posted path that is not one of the files found is dropped, never scanned."""
    await sign_in(web_client, "root")
    response = await post(web_client, base(layout, "/preview"), data)
    assert response.status_code == 422
    assert message in response.text
    assert 'role="alert"' in response.text and 'data-fragment="import-files"' in response.text
    assert 'name="compose_file"' in response.text
    if "extra_path" in data:
        assert f'value="{data["extra_path"]}"' in response.text
        assert re.search(r'value="compose.yaml" id="cf-0" checked', response.text)


async def test_confirm_shows_what_the_import_creates_and_carries_the_judged_choices(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    response = await post(web_client, base(layout, "/confirm"), form_of())
    page = response.text
    assert response.status_code == 200
    assert_never_cached(response)
    assert "3 services" in page and "7 files" in page
    assert (
        'data-import-count="content">6 saved as version 1 as they are on disk, 1 created empty'
        in page
    )
    assert 'data-import-count="linked">2<' in page
    assert f'hx-post="{base(layout)}"' in page and "Working. Scanning the Compose files" in page
    fields = hidden_fields(page)
    assert fields["compose_file"] == ["compose.yaml"]
    assert fields["service"] == ["worker-1", "api", "web"]
    assert fields["file"] == [
        ".env.web",
        ".env.optional",
        "conf/nginx.conf",
        "certs/server.key",
        "conf/app.cfg",
        ".env.shared",
        ".env.api",
    ]
    assert fields["owner:.env.web"] == ["web"] and fields["type:certs/server.key"] == ["cert"]
    assert fields["dist:.env.web"] == [".env.web.example"]
    assert "Nothing is deployed." in page
    assert await counts(web_app) == before


async def test_confirm_ignores_a_forged_dist_path_and_a_forged_owner(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    owners = {**OWNERS, ".env.api": "nobody"}
    forged = form_of(owners=owners, dist={".env.web": "../../etc/passwd", ".env.api": "x"})
    page = (await post(web_client, base(layout, "/confirm"), forged)).text
    fields = hidden_fields(page)
    assert fields["dist:.env.web"] == [".env.web.example"]
    assert fields["dist:.env.api"] == [".env.api-dist"]
    assert fields["owner:.env.api"] == ["api"]
    assert "passwd" not in page


async def test_a_file_whose_service_is_not_selected_is_left_out(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    page = (await post(web_client, base(layout, "/confirm"), form_of(["web"], FILE_KEYS))).text
    fields = hidden_fields(page)
    assert fields["service"] == ["web"]
    assert ".env.api" not in fields["file"] and ".env.shared" not in fields["file"]
    assert "Not imported, because no service that uses them is selected" in page
    assert ".env.api" in page and 'data-import-count="left">3<' in page


async def test_a_shared_file_moves_to_a_selected_service(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    owners = {**OWNERS, ".env.shared": "worker-1"}
    page = (
        await post(web_client, base(layout, "/confirm"), form_of(["api", "web"], owners=owners))
    ).text
    assert hidden_fields(page)["owner:.env.shared"] == ["api"]


@pytest.mark.parametrize(
    ("fields", "label", "message"),
    [
        (form_of([]), "Services", "Select at least one service."),
        (
            form_of(keys=["no-such-file"]),
            "no-such-file",
            "This file is no longer found in the Compose files.",
        ),
        (
            form_of(keys=[".env.optional"]) | {"start:.env.optional": "1"},
            ".env.optional",
            DIST_NEEDED_MESSAGE,
        ),
        (
            form_of(keys=["conf/nginx.conf"]) | {"type:conf/nginx.conf": "cert"},
            "conf/nginx.conf",
            NOT_PEM_MESSAGE,
        ),
    ],
)
async def test_confirm_names_what_cannot_be_imported(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    layout: Layout,
    fields: dict[str, object],
    label: str,
    message: str,
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    response = await post(web_client, base(layout, "/confirm"), fields)
    page = html.unescape(response.text)
    assert response.status_code == 422
    assert "Review the selection" in page and label in page and message in page
    assert 'data-fragment="import-confirm"' in page and "import-confirm-submit" not in page
    assert await counts(web_app) == before


async def test_confirm_reports_a_scan_that_fails(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client, base(layout, "/confirm"), form_of(compose_files=["missing.yaml"])
    )
    assert response.status_code == 422
    assert "The Compose files cannot be read" in response.text
    assert "The Compose file missing.yaml does not exist." in response.text


async def test_the_import_creates_everything_and_leaves_for_the_deployment_page(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    confirm = (await post(web_client, base(layout, "/confirm"), form_of())).text
    fields: dict[str, object] = dict(hidden_fields(confirm))
    response = await post(web_client, base(layout), fields)
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/deployments/{layout.empty}?imported=3,7,6,1,6,0"
    after = await counts(web_app)
    assert tuple(now - was for now, was in zip(after, before, strict=True)) == (3, 7, 1)
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, layout.empty)
        assert deployment is not None and deployment.compose_files == ["compose.yaml"]
    page = (await web_client.get(response.headers["hx-redirect"])).text
    assert "Import finished" in page
    assert (
        "Imported 3 services and 7 files (6 with content, 1 created empty). 6 items not imported."
        in page
    )
    assert "import-compose" not in page
    again = await post(web_client, base(layout), fields)
    assert again.status_code == 409 and HAS_SERVICES_MESSAGE in again.text


async def test_the_runs_of_the_import_are_listed_as_imported_from_compose(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    confirm = (await post(web_client, base(layout, "/confirm"), form_of())).text
    await post(web_client, base(layout), dict(hidden_fields(confirm)))
    async with web_app.state.session_factory() as db:
        web_service = (
            await db.execute(
                select(Service.id).where(
                    Service.deployment_id == layout.empty, Service.name == "web"
                )
            )
        ).scalar_one()
    page = (await web_client.get(f"/deployments/{layout.empty}")).text
    assert page.count("Imported from Compose") >= 4
    history = (await web_client.get(f"/deployments/{layout.empty}/history")).text
    assert history.count("data-history-trigger") == 2
    assert "found identical to the host file, nothing written." in history
    service_history = (await web_client.get(f"/services/{web_service}/history")).text
    assert service_history.count("data-history-trigger") == 1
    assert "Imported from Compose" in service_history
    projects = (await web_client.get("/projects")).text
    assert "Imported from Compose" in projects


async def test_the_import_names_the_files_it_refuses_and_changes_nothing(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    owners = {".env.web": "web", ".env.api": "web"}
    fields = form_of(["web"], [".env.web", ".env.api"], owners=owners)
    fields["dist:.env.api"] = "other-dist"
    response = await post(web_client, base(layout), fields)
    assert response.status_code == 422
    page = response.text
    assert "Nothing was imported" in page and "Some of the choices cannot be imported." in page
    assert ".env.api" in page and "The dist file is not one of the suggested files." in page
    assert await counts(web_app) == before


async def test_a_forged_import_is_judged_against_the_files_as_they_are(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    before = await counts(web_app)
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        base(layout),
        form_of(["web"], ["../../etc/passwd"], owners={"../../etc/passwd": "web"}),
    )
    assert response.status_code == 422
    assert "This file is not offered or is chosen twice." in response.text
    unknown = await post(web_client, base(layout), form_of(["ghost"], []))
    assert unknown.status_code == 422 and "service:ghost" in unknown.text
    assert await counts(web_app) == before


@pytest.mark.parametrize(
    ("query", "toast"),
    [
        ("1,1,0,1,0,0", "Imported 1 service and 1 file (0 with content, 1 created empty)."),
        (
            "2,5,4,1,3,0",
            "Imported 2 services and 5 files (4 with content, 1 created empty). "
            "3 items not imported.",
        ),
        (
            "2,5,4,1,0,3",
            "Imported 2 services and 5 files (4 with content, 1 created empty). "
            "3 post-deploy commands set.",
        ),
        ("1,1,0,1,0", None),
        ("x", None),
        ("1,2,3", None),
        ("1,2,3,4,5,-6", None),
        ("<b>,1,1,1,1,1", None),
    ],
)
async def test_the_toast_echoes_six_plain_numbers_and_nothing_else(
    web_client: httpx.AsyncClient, layout: Layout, query: str, toast: str | None
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{layout.seeded}", params={"imported": query})).text
    if toast is None:
        assert "Import finished" not in page
    else:
        assert toast in page


async def test_files_not_on_disk_start_from_their_dist_file_by_default(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    root = layout.root.parent / "geotravel"
    root.mkdir()
    write_project(root, GEOTRAVEL)
    async with web_app.state.session_factory() as db:
        project = (
            await db.execute(select(Project.id).where(Project.name == "Fancy Proj. Name"))
        ).scalar_one()
        deployment = Deployment(project_id=project, name="geotravel", root_path=str(root))
        db.add(deployment)
        await db.commit()
        deployment_id = deployment.id
    await sign_in(web_client, "root")
    address = f"/deployments/{deployment_id}/import-compose"
    preview = {"compose_file": ["docker-compose.yml"]}
    page = (await post(web_client, f"{address}/preview", preview)).text
    frontend = row_of(page, "environments/.env.frontend")
    assert re.search(r'name="file" value="environments/\.env\.frontend"[^>]*checked', frontend)
    assert re.search(r'name="dist:environments/\.env\.frontend"[^>]*checked', frontend)
    assert re.search(r'name="start:environments/\.env\.frontend"[^>]*checked', frontend)
    assert "Not on disk" in frontend and "created empty" not in frontend
    database = row_of(page, "environments/.env.db")
    assert 'name="start:' not in database
    assert re.search(r'name="dist:environments/\.env\.db"[^>]*checked', database)
    api = row_of(page, "environments/.env.api")
    assert "Bind mount" in api and "Not on disk, looks like a file" in api
    assert re.search(r'name="start:environments/\.env\.api"[^>]*checked', api)
    env = own_row_of(page, ".env")
    assert "Deployment environment file" in env and "Not on disk" not in env
    assert re.search(r'name="deployment_file" value="\.env"[^>]*checked', env)
    fields = {
        "compose_file": ["docker-compose.yml"],
        "service": ["api", "db", "frontend"],
        "file": ["environments/.env.db", "environments/.env.frontend", "environments/.env.api"],
        "owner:environments/.env.db": "db",
        "owner:environments/.env.frontend": "frontend",
        "owner:environments/.env.api": "api",
        "dist:environments/.env.frontend": "environments/.env.frontend-dist",
        "dist:environments/.env.api": "environments/.env.api-dist",
        "start:environments/.env.frontend": "1",
        "start:environments/.env.api": "1",
    }
    confirm = (await post(web_client, f"{address}/confirm", fields)).text
    assert (
        'data-import-count="content">1 saved as version 1 as they are on disk, '
        "2 started from their dist file, 0 created empty"
    ) in confirm
    carried = hidden_fields(confirm)
    assert carried["start:environments/.env.frontend"] == ["1"]
    response = await post(web_client, address, dict(carried))
    assert response.headers["hx-redirect"] == f"/deployments/{deployment_id}?imported=3,3,3,0,4,0"
    assert not (root / "environments/.env.frontend").exists()


API_UP = "docker compose -f compose.yaml up -d --no-deps api"
FILES_UP = "docker compose -f compose.yaml up -d"
RUN_FILES = f"run:{DEPLOYMENT_FILES_NAME}"
OWN_ROW = r'data-fragment="import-deployment-file-row" data-file="([^"]+)"'
COMMAND_FILES = f"command:{DEPLOYMENT_FILES_NAME}"


def with_commands(fields: dict[str, object], commands: dict[str, str]) -> dict[str, object]:
    """The fields with the deployment ``.env`` ticked and a ticked command per owner."""
    fields = {**fields, "deployment_file": [".env"], "type:.env": "env"}
    for owner, text in commands.items():
        fields[f"run:{owner}"] = "1"
        fields[f"command:{owner}"] = text
    return fields


async def test_the_preview_offers_each_command_unticked_while_docker_is_usable(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout, tmp_path: Path
) -> None:
    web_app.state.docker_cli = mounted_cli(tmp_path)
    await sign_in(web_client, "root")
    preview = await post(web_client, base(layout, "/preview"), {"compose_file": ["compose.yaml"]})
    page = preview.text
    assert 'data-fragment="import-no-commands"' not in page
    row = r'<tr data-fragment="import-service-row" data-service="api".*?</tr>'
    api = re.search(row, page, re.S)
    assert api is not None
    assert f'name="command:api" value="{html.escape(API_UP)}"' in api.group(0)
    assert re.search(r'<input type="checkbox" name="run:api" value="1"[^>]*>', api.group(0))
    assert not re.search(r'name="run:[^"]*"[^>]*checked', page)
    own = re.search(r'data-fragment="import-deployment-files".*?</section>', page, re.S)
    assert own is not None
    assert f'name="{COMMAND_FILES}" value="{html.escape(FILES_UP)}"' in own.group(0)
    assert f'name="{RUN_FILES}"' in own.group(0)


async def test_ticked_commands_are_reviewed_and_stored_by_the_import(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout, tmp_path: Path
) -> None:
    web_app.state.docker_cli = mounted_cli(tmp_path)
    await sign_in(web_client, "root")
    fields = with_commands(form_of(), {"api": API_UP, DEPLOYMENT_FILES_NAME: FILES_UP})
    confirm = (await post(web_client, base(layout, "/confirm"), fields)).text
    assert 'data-import-count="commands">2<' in confirm
    assert 'data-import-count="deployment-files">1 file<' in confirm
    review = re.search(r'data-import-command="api".*?</ol>', confirm, re.S)
    assert review is not None
    words = re.findall(r"<code[^>]*>([^<]*)</code>", review.group(0))
    assert words == API_UP.split()
    assert "No post-deploy command is set." not in confirm
    carried = hidden_fields(confirm)
    assert carried["run:api"] == ["1"] and carried["command:api"] == [API_UP]
    assert carried["deployment_file"] == [".env"]
    response = await post(web_client, base(layout), dict(carried))
    assert response.status_code == 200, response.text
    assert response.headers["hx-redirect"].endswith(",2")
    async with web_app.state.session_factory() as db:
        rows = await db.execute(
            select(Service.name, ServiceHook.commands)
            .join(ServiceHook, ServiceHook.service_id == Service.id)
            .where(Service.deployment_id == layout.empty)
        )
        assert dict(rows.all()) == {"api": API_UP, DEPLOYMENT_FILES_NAME: FILES_UP}
    page = (await web_client.get(response.headers["hx-redirect"])).text
    assert "2 post-deploy commands set." in page


@pytest.mark.parametrize(
    ("commands", "label", "message"),
    [
        ({"api": "docker compose down"}, "Post-deploy commands: api", "down"),
        ({"api": ""}, "Post-deploy commands: api", "Enter the command, or untick it."),
    ],
    ids=["safety-net", "empty"],
)
async def test_a_refused_command_is_named_in_the_confirmation(
    web_client: httpx.AsyncClient,
    web_app: FastAPI,
    layout: Layout,
    tmp_path: Path,
    commands: dict[str, str],
    label: str,
    message: str,
) -> None:
    web_app.state.docker_cli = mounted_cli(tmp_path)
    before = await counts(web_app)
    await sign_in(web_client, "root")
    response = await post(web_client, base(layout, "/confirm"), with_commands(form_of(), commands))
    assert response.status_code == 422
    assert label in response.text and message in response.text
    assert await counts(web_app) == before


async def test_a_command_needs_docker_and_the_deployment_command_a_deployment_file(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    preview = await post(web_client, base(layout, "/preview"), {"compose_file": ["compose.yaml"]})
    page = preview.text
    assert "No post-deploy command is offered. The Docker socket is not mounted." in page
    fields = with_commands(form_of(), {"api": API_UP})
    response = await post(web_client, base(layout, "/confirm"), fields)
    assert response.status_code == 422
    assert "The Docker socket is not mounted. Post-deploy commands are off" in response.text
    refused = await post(web_client, base(layout), fields)
    assert refused.status_code == 422 and "The Docker socket is not mounted." in refused.text
    alone = {**form_of(), RUN_FILES: "1", COMMAND_FILES: FILES_UP}
    response = await post(web_client, base(layout, "/confirm"), alone)
    assert response.status_code == 422
    assert "Select a deployment file for the post-deploy command" in response.text


async def test_deployment_files_made_before_the_import_leave_it_offered(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    made = await post(web_client, f"/deployments/{layout.empty}/deployment-files", {})
    assert made.status_code == 200
    page = (await web_client.get(base(layout))).text
    assert HAS_SERVICES_MESSAGE not in page
    detail = main_of((await web_client.get(f"/deployments/{layout.empty}")).text)
    assert f'href="/deployments/{layout.empty}/import-compose"' in detail


async def test_what_the_deployment_files_hold_already_is_not_offered_again(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout, tmp_path: Path
) -> None:
    web_app.state.docker_cli = mounted_cli(tmp_path)
    await sign_in(web_client, "root")
    made = await post(web_client, f"/deployments/{layout.empty}/deployment-files", {})
    files_id = int(made.headers["hx-redirect"].split("/")[2])
    async with web_app.state.session_factory() as db:
        db.add(ServiceHook(service_id=files_id, type=HookType.POST_DEPLOY, commands=FILES_UP))
        await db.commit()
    preview = await post(web_client, base(layout, "/preview"), {"compose_file": ["compose.yaml"]})
    assert 'data-fragment="import-command-set"' in preview.text
    assert f'name="{RUN_FILES}"' not in preview.text
    fields = with_commands(form_of(), {DEPLOYMENT_FILES_NAME: FILES_UP})
    refused = await post(web_client, base(layout, "/confirm"), fields)
    assert refused.status_code == 422 and COMMAND_STORED_MESSAGE in refused.text

    added = await post(
        web_client,
        f"/services/{files_id}/environments/new",
        {"service": str(files_id), "name": ".env", "target_path": ".env", "source": "import"},
    )
    assert added.status_code == 200, added.text
    preview = await post(web_client, base(layout, "/preview"), {"compose_file": ["compose.yaml"]})
    env = own_row_of(preview.text, ".env")
    assert IN_FILES_REASON in env
    assert re.search(r'name="deployment_file" value="\.env"[^>]*disabled', env)


async def test_the_import_links_the_compose_files_instead_of_importing_them(
    web_client: httpx.AsyncClient, web_app: FastAPI, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    preview = await post(
        web_client,
        base(layout, "/preview"),
        {"compose_file": ["compose.yaml", "compose.override.yaml"]},
    )
    own = re.findall(OWN_ROW, preview.text)
    assert own == [".env"]
    fields = with_commands(form_of(compose_files=["compose.yaml", "compose.override.yaml"]), {})
    confirm = (await post(web_client, base(layout, "/confirm"), fields)).text
    done = await post(web_client, base(layout), dict(hidden_fields(confirm)))
    assert done.status_code == 200, done.text
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, layout.empty)
        assert deployment is not None
        assert deployment.compose_files == ["compose.yaml", "compose.override.yaml"]
        targets = await db.scalars(
            select(Environment.target_path)
            .join(Service, Service.id == Environment.service_id)
            .where(Service.deployment_id == layout.empty)
        )
        assert not {"compose.yaml", "compose.override.yaml"} & set(targets)
    page = main_of((await web_client.get(f"/deployments/{layout.empty}")).text)
    rows = re.findall(r'data-fragment="compose-file-row" data-file="([^"]+)"', page)
    assert rows == ["compose.yaml", "compose.override.yaml"]
