"""Add service and add environment forms on the seeded application."""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import (
    AllowedPath,
    AuditEntry,
    Deployment,
    Environment,
    EnvironmentVersion,
    FileType,
    Service,
)
from app.services.notify_events import FileChange, FileChanged
from app.tests.conftest import sign_in
from app.tests.notify_helpers import RecordingEvents
from app.tests.web_helpers import HTMX, breadcrumb_of, post

CERT_TEXT = "-----BEGIN CERTIFICATE-----\nc2VlZA==\n-----END CERTIFICATE-----\n"


@dataclass(frozen=True)
class Layout:
    """A real deployment folder under an allowed host directory, with ids of seeded rows."""

    host: Path
    deploy: Path
    deployment: int
    mysql: int
    web: int
    legacy: int
    cron: int


@pytest.fixture
async def layout(web_app: FastAPI, tmp_path: Path) -> Layout:
    """Allow a temporary host directory and move the seeded prod deployment into it."""
    host = (tmp_path / "vhosts").resolve()
    deploy = host / "prod"
    (deploy / "environments").mkdir(parents=True)
    async with web_app.state.session_factory() as db:
        db.add(AllowedPath(path=str(host)))
        deployments = dict((await db.execute(select(Deployment.name, Deployment))).all())
        deployments["prod"].root_path = str(deploy)
        services = {
            (service.deployment_id, service.name): service.id
            for service in (await db.execute(select(Service))).scalars()
        }
        await db.commit()
        prod, legacy = deployments["prod"].id, deployments["legacy"].id
    return Layout(
        host=host,
        deploy=deploy,
        deployment=prod,
        mysql=services[(prod, "mysql")],
        web=services[(prod, "web")],
        legacy=legacy,
        cron=services[(legacy, "cron")],
    )


def env_data(layout: Layout, **overrides: str) -> dict[str, str]:
    """Valid add environment values for a paste into the seeded mysql service."""
    values = {
        "service": str(layout.mysql),
        "new_service": "",
        "name": ".env.cache",
        "target_path": "environments/.env.cache",
        "dist_path": "",
        "file_mode": "",
        "file_owner": "",
        "file_group": "",
        "source": "paste",
        "content": "CACHE_HOST=cache\r\nCACHE_PORT=6379",
    }
    return {**values, **overrides}


async def environment_names(web_app: FastAPI) -> set[str]:
    """Names of every stored environment."""
    async with web_app.state.session_factory() as db:
        return set((await db.execute(select(Environment.name))).scalars())


async def test_new_environment_page_has_every_field(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/services/{layout.mysql}/environments/new")
    page = response.text
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert ">Add environment</h1>" in page and ">Superadmin</span>" in page
    assert "Superadmin only." in page
    assert f'hx-post="/services/{layout.mysql}/environments/new"' in page
    assert 'enctype="multipart/form-data"' in page
    assert 'id="env-form"' in page and 'data-fragment="environment-form"' in page
    assert page.count('data-fragment="form-section"') == 3
    for field_id in ("svc", "nsvc", "en", "tp", "dp", "m", "o", "g", "content", "upload"):
        assert f'id="{field_id}"' in page
    assert f'<option value="{layout.mysql}" selected>mysql</option>' in page
    assert f'<option value="{layout.web}">web</option>' in page
    assert '<option value="new">Create a new service…</option>' in page
    assert "A service is required to create an environment." in page
    assert 'placeholder="0600"' in page and 'placeholder="www-data"' in page
    assert f"Resolves to {layout.deploy}/" in page
    assert f'hx-get="/services/{layout.mysql}/environments/target-check"' in page
    for source in ("paste", "upload", "import", "dist"):
        assert f'id="src-{source}"' in page
    assert re.search(r'name="source" value="paste" checked', page)
    assert "Reads the file from the target and saves it as version 1." in page
    assert "Copy the dist file" in page
    dist_choice = re.search(r'<input type="radio" name="source" value="dist"[^>]*>', page)
    assert dist_choice is not None and " disabled" in dist_choice.group(0)
    assert "<span data-dist-missing>Enter a dist file path above to copy it.</span>" in page
    assert re.search(r"<span data-dist-ready hidden>", page)
    assert ">Add environment</button>" in page
    assert f'href="/services/{layout.mysql}"' in page and ">Cancel</a>" in page
    crumbs = breadcrumb_of(page)
    assert [(crumb.label, crumb.type) for crumb in crumbs] == [
        ("Projects", None),
        ("Fancy Proj. Name", "Project"),
        ("prod", "Deployment"),
        ("mysql", "Service"),
        ("Add environment", None),
    ]
    assert crumbs[3].href == f"/services/{layout.mysql}"


async def test_the_form_page_loads_for_a_deployment_outside_the_allowed_paths(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/services/{layout.cron}/environments/new")
    assert response.status_code == 200
    hint = await web_client.get(
        f"/services/{layout.cron}/environments/target-check", params={"target_path": "x"}
    )
    assert "The deployment root is outside the allowed paths." in hint.text


@pytest.mark.parametrize("user", ["admin", "viewer", "deployment_admin", "env_scoped_admin"])
async def test_other_groups_are_refused_everywhere(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI, user: str
) -> None:
    await sign_in(web_client, user)
    url = f"/services/{layout.mysql}/environments/new"
    refused = {403, 404}
    assert (await web_client.get(url)).status_code in refused
    assert (
        await web_client.get(
            f"/services/{layout.mysql}/environments/target-check", params={"target_path": "x"}
        )
    ).status_code in refused
    assert (await post(web_client, url, env_data(layout))).status_code in refused
    service_url = f"/deployments/{layout.deployment}/services/new"
    assert (await web_client.get(service_url)).status_code in refused
    assert (await post(web_client, service_url, {"name": "extra"})).status_code in refused
    assert ".env.cache" not in await environment_names(web_app)
    async with web_app.state.session_factory() as db:
        assert "extra" not in set((await db.execute(select(Service.name))).scalars())


async def test_outside_the_scope_is_404(web_client: httpx.AsyncClient, layout: Layout) -> None:
    await sign_in(web_client, "ungranted_admin")
    assert (await web_client.get(f"/services/{layout.mysql}/environments/new")).status_code == 404
    assert (
        await web_client.get(f"/deployments/{layout.deployment}/services/new")
    ).status_code == 404


async def test_signed_out_requests_are_refused(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    for url in (
        f"/services/{layout.mysql}/environments/new",
        f"/services/{layout.mysql}/environments/target-check",
        f"/deployments/{layout.deployment}/services/new",
    ):
        response = await web_client.get(url, follow_redirects=False)
        assert response.status_code in (303, 401), url
    unsigned = await post(
        web_client, f"/services/{layout.mysql}/environments/new", env_data(layout)
    )
    assert unsigned.status_code in (303, 401)


async def test_target_check_fragment_states(web_client: httpx.AsyncClient, layout: Layout) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{layout.mysql}/environments/target-check"
    good = await web_client.get(url, params={"target_path": "environments/.env.cache"})
    assert f"Resolves to {layout.deploy}/environments/.env.cache" in good.text
    assert 'id="tp-h"' in good.text and good.headers["cache-control"] == "no-store"
    bad = await web_client.get(url, params={"target_path": "../../etc/cache.env"})
    assert 'role="alert"' in bad.text and "Resolves outside the deployment root" in bad.text
    empty = await web_client.get(url)
    assert f"Resolves to {layout.deploy}/" in empty.text


async def test_paste_creates_the_environment_with_version_one(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/services/{layout.mysql}/environments/new", env_data(layout)
    )
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/services/{layout.mysql}?added=environment"
    async with web_app.state.session_factory() as db:
        environment = (
            await db.execute(select(Environment).where(Environment.name == ".env.cache"))
        ).scalar_one()
        version = (
            await db.execute(
                select(EnvironmentVersion).where(
                    EnvironmentVersion.environment_id == environment.id
                )
            )
        ).scalar_one()
        assert environment.target_path == "environments/.env.cache"
        assert environment.file_mode is None and environment.dist_path is None
        assert version.number == 1 and "CACHE_HOST" not in version.content_encrypted
        text = web_app.state.crypto.decrypt_text(version.content_encrypted)
        assert text == "CACHE_HOST=cache\nCACHE_PORT=6379"
        entry = (
            await db.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "environment.create",
                    AuditEntry.environment_id == environment.id,
                )
            )
        ).scalar_one()
        assert entry.actor_name == "root" and entry.version_ids == [version.id]
        assert "CACHE_HOST" not in str(entry.details)
    landed = await web_client.get(response.headers["hx-redirect"])
    assert "Environment added" in landed.text and ".env.cache" in landed.text


async def test_a_new_service_is_created_with_the_environment(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    data = env_data(layout, service="new", new_service="redis", name="redis.conf")
    response = await post(web_client, f"/services/{layout.mysql}/environments/new", data)
    created = re.fullmatch(r"/services/(\d+)\?added=environment", response.headers["hx-redirect"])
    assert created is not None and int(created.group(1)) not in (layout.mysql, layout.web)
    async with web_app.state.session_factory() as db:
        service = (await db.execute(select(Service).where(Service.name == "redis"))).scalar_one()
        assert service.id == int(created.group(1)) and service.deployment_id == layout.deployment
        actions = set(
            (
                await db.execute(
                    select(AuditEntry.action).where(AuditEntry.service_id == service.id)
                )
            ).scalars()
        )
        assert actions == {"service.create", "environment.create"}


async def test_upload_creates_the_environment_from_the_file(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/services/{layout.mysql}/environments/new",
        env_data(layout, source="upload", content="ignored"),
        files={"upload": ("cache.env", b"UPLOADED=1\r\n")},
    )
    assert response.status_code == 200
    async with web_app.state.session_factory() as db:
        content = (
            await db.execute(
                select(EnvironmentVersion.content_encrypted)
                .join(Environment, Environment.id == EnvironmentVersion.environment_id)
                .where(Environment.name == ".env.cache")
            )
        ).scalar_one()
    assert web_app.state.crypto.decrypt_text(content) == "UPLOADED=1\r\n"


async def test_upload_refusals_swap_the_form(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{layout.mysql}/environments/new"
    data = env_data(layout, source="upload")
    none = await post(web_client, url, data)
    assert none.status_code == 422 and "Choose a text file to upload." in none.text
    assert re.search(r'name="source" value="upload" checked', none.text)
    binary = await post(web_client, url, data, files={"upload": ("x", b"\xff\xfe")})
    assert binary.status_code == 422 and "The file is not valid UTF-8 text." in binary.text
    assert ".env.cache" not in await environment_names(web_app)


async def test_import_reads_the_target_file_and_refuses_an_escaping_symlink(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI, tmp_path: Path
) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{layout.mysql}/environments/new"
    (layout.deploy / "environments" / ".env.cache").write_text("LIVE=1\n")
    created = await post(web_client, url, env_data(layout, source="import"))
    assert created.status_code == 200
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET=1\n")
    (layout.deploy / "environments" / "linked").symlink_to(secret)
    refused = await post(
        web_client,
        url,
        env_data(layout, source="import", name="linked", target_path="environments/linked"),
    )
    assert refused.status_code == 422
    assert "Resolves outside the deployment root and the allowed paths." in refused.text
    assert "SECRET" not in refused.text
    async with web_app.state.session_factory() as db:
        content = (
            await db.execute(
                select(EnvironmentVersion.content_encrypted)
                .join(Environment, Environment.id == EnvironmentVersion.environment_id)
                .where(Environment.name == ".env.cache")
            )
        ).scalar_one()
        assert web_app.state.crypto.decrypt_text(content) == "LIVE=1\n"
    assert "linked" not in await environment_names(web_app)


async def test_the_dist_source_copies_the_dist_file_as_version_one(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    (layout.deploy / "environments" / ".env.cache-dist").write_text("CACHE_HOST=\nCACHE_PORT=\n")
    response = await post(
        web_client,
        f"/services/{layout.mysql}/environments/new",
        env_data(
            layout, source="dist", dist_path="environments/.env.cache-dist", content="ignored"
        ),
    )
    assert response.status_code == 200 and "hx-redirect" in response.headers
    async with web_app.state.session_factory() as db:
        environment = (
            await db.execute(select(Environment).where(Environment.name == ".env.cache"))
        ).scalar_one()
        assert environment.dist_path == "environments/.env.cache-dist"
        version = (
            await db.execute(
                select(EnvironmentVersion).where(
                    EnvironmentVersion.environment_id == environment.id
                )
            )
        ).scalar_one()
        assert web_app.state.crypto.decrypt_text(version.content_encrypted) == (
            "CACHE_HOST=\nCACHE_PORT=\n"
        )
        assert version.comment == "Initial content (copied from the dist file)"
        entry = (
            await db.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "environment.create",
                    AuditEntry.environment_id == environment.id,
                )
            )
        ).scalar_one()
        assert entry.details["source"] == "dist"


async def test_the_dist_source_refusals_swap_the_form_and_enable_the_choice(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI, tmp_path: Path
) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{layout.mysql}/environments/new"
    no_path = await post(web_client, url, env_data(layout, source="dist"))
    assert no_path.status_code == 422
    assert "Enter the dist file path to copy the file." in no_path.text
    assert re.search(r'name="source" value="dist" checked', no_path.text)
    data = env_data(layout, source="dist", dist_path="environments/.env.cache-dist")
    missing = await post(web_client, url, data)
    assert missing.status_code == 422
    assert "The dist file does not exist or is not a regular file." in missing.text
    choice = re.search(r'<input type="radio" name="source" value="dist"[^>]*>', missing.text)
    assert choice is not None
    assert " disabled" not in choice.group(0) and " checked" in choice.group(0)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET=1\n")
    (layout.deploy / "environments" / ".env.cache-dist").symlink_to(secret)
    escaped = await post(web_client, url, data)
    assert escaped.status_code == 422
    assert "Resolves outside the deployment root and the allowed paths." in escaped.text
    assert "Fix the dist file path to copy the file." in escaped.text
    assert "SECRET" not in escaped.text
    assert ".env.cache" not in await environment_names(web_app)


async def test_refused_values_swap_the_form_with_messages_and_keep_input(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    data = env_data(
        layout,
        name="bad name",
        target_path="../../etc/cache.env",
        dist_path="environments/.env.cache",
        file_mode="999",
        content="KEEP_ME=1\r\nSECOND=2",
    )
    response = await post(web_client, f"/services/{layout.mysql}/environments/new", data)
    page = response.text
    assert response.status_code == 422 and "<html" not in page
    assert page.lstrip().startswith("<form") and 'id="env-form"' in page
    assert "Use 1 to 100 letters" in page
    assert "Resolves outside the deployment root and the allowed paths." in page
    assert "Use an octal mode such as 0600." in page
    assert 'aria-invalid="true"' in page and "border-danger" in page
    assert 'value="bad name"' in page and 'value="../../etc/cache.env"' in page
    assert "KEEP_ME=1" in page and response.headers["cache-control"] == "no-store"
    assert "bad name" not in await environment_names(web_app)


async def test_content_is_escaped_in_the_swapped_form(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    data = env_data(layout, name="", content="</textarea><script>alert(1)</script>")
    response = await post(web_client, f"/services/{layout.mysql}/environments/new", data)
    assert response.status_code == 422
    assert "<script>" not in response.text and "&lt;/textarea&gt;" in response.text


async def test_a_duplicate_name_or_target_is_refused(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{layout.mysql}/environments/new"
    duplicate = await post(web_client, url, env_data(layout, name=".env.db"))
    assert "This service already has a file named .env.db." in duplicate.text
    shared = await post(web_client, url, env_data(layout, target_path="environments/.env.db"))
    assert "Another file of this deployment already uses this target path." in shared.text


async def test_an_oversized_form_is_refused_before_it_is_parsed(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/services/{layout.mysql}/environments/new",
        env_data(layout, content="A" * (2 * 1024 * 1024)),
    )
    assert response.status_code == 422 and "The form is larger than 1024 KiB." in response.text
    assert ".env.cache" not in await environment_names(web_app)


async def test_a_body_without_a_length_is_refused(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    await web_client.get("/healthz")

    async def chunks() -> AsyncIterator[bytes]:
        yield b"name=x"

    response = await web_client.post(
        f"/services/{layout.mysql}/environments/new",
        content=chunks(),
        headers={
            **HTMX,
            "X-CSRFToken": web_client.cookies[CSRF_COOKIE_NAME],
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 411


async def test_service_page_toasts_follow_the_added_flag(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    base = f"/services/{layout.mysql}"
    assert "Environment added" in (await web_client.get(f"{base}?added=environment")).text
    assert "Service added" in (await web_client.get(f"{base}?added=service")).text
    plain = await web_client.get(f"{base}?added=anything")
    assert "Service added" not in plain.text and "Environment added" not in plain.text
    assert "Environment added" not in (await web_client.get(base)).text


async def test_new_service_page_and_create(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    page = await web_client.get(f"/deployments/{layout.deployment}/services/new")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert ">Add service</h1>" in page.text and "Superadmin only." in page.text
    assert f'hx-post="/deployments/{layout.deployment}/services/new"' in page.text
    assert 'id="service-form"' in page.text and 'id="svc-name"' in page.text
    assert "Use the Compose service name." in page.text
    assert f'href="/deployments/{layout.deployment}"' in page.text
    created = await post(
        web_client, f"/deployments/{layout.deployment}/services/new", {"name": " redis "}
    )
    assert created.status_code == 200
    async with web_app.state.session_factory() as db:
        service = (await db.execute(select(Service).where(Service.name == "redis"))).scalar_one()
        assert created.headers["hx-redirect"] == f"/services/{service.id}?added=service"
        entry = (
            await db.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "service.create", AuditEntry.service_id == service.id
                )
            )
        ).scalar_one()
        assert entry.actor_name == "root" and entry.deployment_id == layout.deployment


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Enter a service name."),
        ("Bad Name", "Use 1 to 100 lowercase letters"),
        ("mysql", "A service named mysql already exists in this deployment."),
    ],
)
async def test_new_service_refusals_swap_the_form(
    web_client: httpx.AsyncClient, layout: Layout, name: str, message: str
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/deployments/{layout.deployment}/services/new", {"name": name}
    )
    assert response.status_code == 422 and message in response.text
    assert response.text.lstrip().startswith("<form") and 'aria-invalid="true"' in response.text


async def test_adding_an_environment_announces_the_file_to_the_notifier(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    """The add environment route hands its notifier to the service: version 1 is a new file."""
    recorder = RecordingEvents()
    web_app.state.notifier = recorder
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/services/{layout.mysql}/environments/new", env_data(layout)
    )
    assert response.status_code == 200
    async with web_app.state.session_factory() as db:
        created = (
            await db.execute(select(Environment).where(Environment.name == ".env.cache"))
        ).scalar_one()
        project = (await db.get(Deployment, layout.deployment)).project_id
    assert recorder.events == [
        FileChanged(
            project,
            layout.deployment,
            layout.mysql,
            created.id,
            ".env.cache",
            1,
            FileChange.CREATED,
            "root",
        )
    ]
    bad = await post(
        web_client,
        f"/services/{layout.mysql}/environments/new",
        env_data(layout, name=".env.cache", target_path="environments/.env.cache2"),
    )
    assert bad.status_code == 422 and len(recorder.events) == 1


async def test_the_form_has_a_type_select_and_the_name_endings_that_select_a_certificate(
    web_client: httpx.AsyncClient, layout: Layout
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/services/{layout.mysql}/environments/new")).text
    assert 'id="ft"' in page and 'name="file_type"' in page and 'data-js="file-type"' in page
    assert '<option value="env" selected>ENV file</option>' in page
    assert '<option value="cert">Certificate</option>' in page
    assert 'data-js="file-name"' in page and 'data-cert-suffixes=".pem .crt .cer .key"' in page
    assert "names ending in .pem, .crt, .cer, .key" in page
    assert "<div data-dist-field>" in page and "data-dist-cert hidden>" in page


async def test_a_certificate_is_created_with_its_type_and_no_dist_file(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    pem = "-----BEGIN CERTIFICATE-----\r\nc2VlZA==\r\n-----END CERTIFICATE-----"
    response = await post(
        web_client,
        f"/services/{layout.mysql}/environments/new",
        env_data(
            layout,
            name="tls.pem",
            target_path="environments/tls.pem",
            file_type="cert",
            content=pem,
        ),
    )
    assert response.status_code == 200
    async with web_app.state.session_factory() as db:
        environment = (
            await db.execute(select(Environment).where(Environment.name == "tls.pem"))
        ).scalar_one()
        assert environment.file_type is FileType.CERT and environment.dist_path is None
        entry = (
            await db.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "environment.create",
                    AuditEntry.environment_id == environment.id,
                )
            )
        ).scalar_one()
        assert entry.details["file_type"] == "cert"
    page = await web_client.get(f"/environments/{environment.id}")
    assert "Certificate" in page.text and "Not applicable" in page.text


async def test_a_refused_certificate_swaps_the_form_with_the_type_kept(
    web_client: httpx.AsyncClient, layout: Layout, web_app: FastAPI
) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{layout.mysql}/environments/new"
    not_pem = await post(web_client, url, env_data(layout, file_type="cert"))
    assert not_pem.status_code == 422
    assert "A certificate must hold at least one PEM block" in not_pem.text
    assert '<option value="cert" selected>Certificate</option>' in not_pem.text
    assert "<div data-dist-field hidden>" in not_pem.text
    assert "CACHE_HOST" in not_pem.text
    dist_choice = re.search(r'<input type="radio" name="source" value="dist"[^>]*>', not_pem.text)
    assert dist_choice is not None and " disabled" in dist_choice.group(0)
    with_dist = await post(
        web_client,
        url,
        env_data(layout, file_type="cert", dist_path="environments/x-dist", content=CERT_TEXT),
    )
    assert with_dist.status_code == 422 and "A certificate has no dist file." in with_dist.text
    unknown = await post(web_client, url, env_data(layout, file_type="certificate"))
    assert unknown.status_code == 422 and "Choose the type of the file." in unknown.text
    assert "tls.pem" not in await environment_names(web_app)
    assert ".env.cache" not in await environment_names(web_app)
