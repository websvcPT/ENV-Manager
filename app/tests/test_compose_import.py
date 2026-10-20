"""Importing the services and files of a Compose scan into a deployment that has none."""

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditEntry,
    Deployment,
    DeployResultStatus,
    DeployStatus,
    Environment,
    EnvironmentVersion,
    FileType,
    Service,
)
from app.services import compose_import
from app.services.compose_config import REMOTE_MESSAGE
from app.services.compose_import import (
    EMPTY_COMMENT,
    HAS_SERVICES_MESSAGE,
    IMPORT_COMMENT,
    NO_SERVICE_MESSAGE,
    ComposeImportError,
    ComposeSelection,
    FileChoice,
    import_compose,
)
from app.services.environments import (
    CleanEnvironment,
    EnvironmentForm,
    EnvironmentFormError,
    validate_environment,
)
from app.services.notify_events import FileChange, FileChanged, NotificationEvent
from app.services.paths import AllowedPaths
from app.tests.compose_helpers import PEM, add_stack, real_cli
from app.tests.deploy_helpers import Harness, audit_of, results_of, version_text

SERVICES = ("api", "web", "worker-1")
FILES = (
    FileChoice(".env.web", "web", dist_path=".env.web.example"),
    FileChoice(".env.optional", "web"),
    FileChoice("conf/nginx.conf", "web"),
    FileChoice("certs/server.key", "web"),
    FileChoice("conf/app.cfg", "web"),
    FileChoice(".env.shared", "api"),
    FileChoice(".env.api", "api", dist_path=".env.api-dist"),
    FileChoice(".env", "api"),
)


def selection(
    services: tuple[str, ...] = SERVICES,
    files: tuple[FileChoice, ...] = FILES,
    compose_files: tuple[str, ...] = ("compose.yaml",),
) -> ComposeSelection:
    """The choices of the whole fixture project, or the given part of them."""
    return ComposeSelection(compose_files, services, files)


class Recorder:
    """A notifier that keeps the events it is given."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[NotificationEvent] = []

    async def notify(self, event: NotificationEvent) -> None:
        """Keep the event."""
        self.events.append(event)


async def run_import(
    h: Harness,
    deployment: Deployment,
    chosen: ComposeSelection,
    tmp_path: Path,
    notifier: Recorder | None = None,
) -> compose_import.ComposeImportResult:
    """Import as the seeded superadmin with the real Compose."""
    return await import_compose(
        h.db,
        h.crypto,
        real_cli(tmp_path),
        deployment,
        chosen,
        actor=h.seed.users.superadmin,
        ip_address="203.0.113.7",
        **({"notifier": notifier} if notifier else {}),
    )


async def environments_of(h: Harness, deployment: Deployment) -> dict[str, tuple[str, Environment]]:
    """The environments of the deployment by target path, with their service names."""
    rows = await h.db.execute(
        select(Service.name, Environment)
        .join(Environment, Environment.service_id == Service.id)
        .where(Service.deployment_id == deployment.id)
    )
    return {environment.target_path: (name, environment) for name, environment in rows.all()}


async def counts(h: Harness) -> tuple[int, int, int, int]:
    """How many services, environments, versions and import audit entries exist."""
    services = (await h.db.execute(select(func.count(Service.id)))).scalar_one()
    environments = (await h.db.execute(select(func.count(Environment.id)))).scalar_one()
    versions = (await h.db.execute(select(func.count(EnvironmentVersion.id)))).scalar_one()
    entries = len(await audit_of(h.db, "deployment.import_compose"))
    return services, environments, versions, entries


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file below the root with its bytes and modification time."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def test_services_and_files_are_created_with_the_first_versions(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, root = await add_stack(h)
    (root / ".env.web").chmod(0o640)
    notifier = Recorder()
    result = await run_import(h, deployment, selection(), tmp_path, notifier)
    assert (result.services, result.environments, result.with_content, result.empty) == (3, 8, 7, 1)
    assert result.skipped == 5
    services = (
        (await h.db.execute(select(Service.name).where(Service.deployment_id == deployment.id)))
        .scalars()
        .all()
    )
    assert sorted(services) == ["api", "web", "worker-1"]
    created = await environments_of(h, deployment)
    assert {key: owner for key, (owner, _) in created.items()} == {
        ".env.web": "web",
        ".env.optional": "web",
        "conf/nginx.conf": "web",
        "certs/server.key": "web",
        "conf/app.cfg": "web",
        ".env.shared": "api",
        ".env.api": "api",
        ".env": "api",
    }
    web = created[".env.web"][1]
    assert (web.name, web.file_type, web.file_mode, web.dist_path) == (
        ".env.web",
        FileType.ENV,
        "0640",
        ".env.web.example",
    )
    assert created["certs/server.key"][1].file_type is FileType.CERT
    assert created["conf/nginx.conf"][1].file_mode == "0644"
    assert created[".env.api"][1].file_mode == "0644"
    assert created[".env.api"][1].dist_path == ".env.api-dist"
    assert await version_text(h.db, h.crypto, web.id, 1) == "WEB=1\n"
    assert await version_text(h.db, h.crypto, created["certs/server.key"][1].id) == PEM
    optional = created[".env.optional"][1]
    assert await version_text(h.db, h.crypto, optional.id) == ""
    versions = await h.db.scalars(select(EnvironmentVersion).order_by(EnvironmentVersion.id))
    by_environment = {version.environment_id: version for version in versions}
    assert {by_environment[web.id].number, by_environment[optional.id].number} == {1}
    assert by_environment[web.id].comment == IMPORT_COMMENT
    assert by_environment[optional.id].comment == EMPTY_COMMENT
    assert by_environment[web.id].author_id == h.seed.users.superadmin.id
    await h.db.refresh(deployment)
    assert deployment.compose_files == ["compose.yaml"]
    assert [event.change for event in notifier.events if isinstance(event, FileChanged)] == [
        FileChange.CREATED
    ] * 8


async def test_the_audit_holds_one_import_entry_and_the_usual_create_entries(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(h)
    await run_import(h, deployment, selection(), tmp_path)
    (entry,) = await audit_of(h.db, "deployment.import_compose")
    assert (entry.target_type, entry.target_id, entry.deployment_id) == (
        "deployment",
        deployment.id,
        deployment.id,
    )
    assert entry.ip_address == "203.0.113.7"
    details = entry.details
    assert isinstance(details, dict)
    assert details["compose_files"] == ["compose.yaml"]
    assert details["services"] == ["api", "web", "worker-1"]
    assert (details["environments"], details["with_content"], details["empty"]) == (8, 7, 1)
    assert details["skipped_count"] == 5
    items = cast(list[dict[str, str]], details["skipped"])
    skipped = {(item["kind"], item["name"]): item["reason"] for item in items}
    assert skipped[("service", "worker-1")] == "Another service is already named worker-1."
    assert ("ignored", "data") in skipped and ("ignored", "missing.conf") in skipped
    assert len(await audit_of(h.db, "service.create")) == 3
    created = [
        entry
        for entry in await audit_of(h.db, "environment.create")
        if entry.deployment_id == deployment.id
    ]
    assert len(created) == 8
    text = repr([entry.details for entry in await h.db.scalars(select(AuditEntry))])
    for content in ("WEB=1", "SHARED=1", "worker_processes", "PROJECT_VARIABLE"):
        assert content not in text


async def test_the_import_writes_nothing_to_the_host(harness: Harness, tmp_path: Path) -> None:
    h = harness
    deployment, root = await add_stack(h)
    before = snapshot(root.parent)
    await run_import(h, deployment, selection(), tmp_path)
    assert snapshot(root.parent) == before
    assert not (root / ".env.optional").exists()


async def test_the_first_deploy_after_an_import_sees_the_files_in_sync(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, root = await add_stack(h)
    await run_import(h, deployment, selection(), tmp_path)
    web_id = (
        await h.db.execute(
            select(Service.id).where(Service.deployment_id == deployment.id, Service.name == "web")
        )
    ).scalar_one()
    before = snapshot(root)
    outcome = await h.deploy(web_id, user="superadmin")
    assert outcome.status is DeployStatus.SUCCESS and outcome.run_id is not None
    results = await results_of(h.db, outcome.run_id)
    assert results[".env.web"].status is DeployResultStatus.UNCHANGED
    assert results["server.key"].status is DeployResultStatus.UNCHANGED
    assert results[".env.optional"].status is DeployResultStatus.WRITTEN
    after = snapshot(root)
    assert {key: value[0] for key, value in after.items() if key != ".env.optional"} == {
        key: value[0] for key, value in before.items()
    }


async def test_a_shared_file_belongs_to_the_chosen_owner(harness: Harness, tmp_path: Path) -> None:
    h = harness
    deployment, _ = await add_stack(h)
    files = tuple(
        FileChoice(choice.key, "worker-1") if choice.key == ".env.shared" else choice
        for choice in FILES
    )
    await run_import(h, deployment, selection(files=files), tmp_path)
    assert (await environments_of(h, deployment))[".env.shared"][0] == "worker-1"


async def test_a_file_of_the_same_name_in_another_folder_gets_the_folder_as_a_prefix(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(
        h,
        {
            "compose.yaml": (
                "services:\n  web:\n    image: x\n    env_file: [a/app.env, b/app.env]\n"
            ),
            "a/app.env": "A=1\n",
            "b/app.env": "B=1\n",
        },
    )
    chosen = selection(("web",), (FileChoice("a/app.env", "web"), FileChoice("b/app.env", "web")))
    await run_import(h, deployment, chosen, tmp_path)
    created = await environments_of(h, deployment)
    assert (created["a/app.env"][1].name, created["b/app.env"][1].name) == ("app.env", "b-app.env")


async def test_the_type_of_a_file_can_be_changed_before_the_import(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(
        h,
        {
            "compose.yaml": "services:\n  web:\n    image: x\n    env_file: [tls.pem, a.env]\n",
            "tls.pem": PEM,
            "a.env": "A=1\n",
        },
    )
    chosen = selection(
        ("web",),
        (FileChoice("tls.pem", "web", FileType.ENV), FileChoice("a.env", "web")),
    )
    await run_import(h, deployment, chosen, tmp_path)
    created = await environments_of(h, deployment)
    assert created["tls.pem"][1].file_type is FileType.ENV
    assert created["a.env"][1].file_type is FileType.ENV


async def test_the_compose_files_are_stored_with_the_base_first(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(
        h,
        {
            "app/compose.yaml": "services:\n  web:\n    image: x\n    env_file: web.env\n",
            "app/compose.override.yaml": "services:\n  web:\n    env_file: [web.env, more.env]\n",
            "app/web.env": "A=1\n",
        },
    )
    chosen = selection(
        ("web",),
        (FileChoice("app/web.env", "web"), FileChoice("app/more.env", "web")),
        ("app/compose.yaml", "app/compose.override.yaml"),
    )
    await run_import(h, deployment, chosen, tmp_path)
    await h.db.refresh(deployment)
    assert deployment.compose_files == ["app/compose.yaml", "app/compose.override.yaml"]


async def test_a_deployment_that_has_services_is_refused_and_unchanged(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(h)
    await run_import(h, deployment, selection(), tmp_path)
    before = await counts(h)
    with pytest.raises(ComposeImportError) as again:
        await run_import(h, deployment, selection(), tmp_path)
    assert str(again.value) == HAS_SERVICES_MESSAGE
    seeded = h.seed.deployments.fancy_prod
    stored = await h.db.get(Deployment, seeded.id)
    assert stored is not None
    with pytest.raises(ComposeImportError) as seeded_error:
        await run_import(h, stored, selection(), tmp_path)
    assert str(seeded_error.value) == HAS_SERVICES_MESSAGE
    assert await counts(h) == before


async def test_a_file_that_cannot_be_created_changes_nothing(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    deployment, _ = await add_stack(h)
    before = await counts(h)
    real = validate_environment

    async def refuse_one(
        db: AsyncSession, allowed: AllowedPaths, deployment: Deployment, form: EnvironmentForm
    ) -> CleanEnvironment:
        if form.target_path == ".env.api":
            raise EnvironmentFormError({"target_path": "Refused for the test."})
        return await real(db, allowed, deployment, form)

    monkeypatch.setattr("app.services.compose_import.validate_environment", refuse_one)
    with pytest.raises(ComposeImportError) as caught:
        await run_import(h, deployment, selection(), tmp_path)
    assert caught.value.errors == {".env.api": "Refused for the test."}
    assert await counts(h) == before
    assert await audit_of(h.db, "service.create") == []
    await h.db.refresh(deployment)
    assert deployment.compose_files is None
    assert await environments_of(h, deployment) == {}


ChoiceBuilder = Callable[[], tuple[ComposeSelection, dict[str, str]]]


def refused_choices() -> dict[str, ChoiceBuilder]:
    """Choices the scan does not match, each with the errors expected."""
    web = ("web",)
    return {
        "unknown service": lambda: (
            selection(("web", "ghost"), ()),
            {"service:ghost": "This service is not in the Compose files or cannot be used."},
        ),
        "unknown file": lambda: (
            selection(web, (FileChoice("nope.env", "web"),)),
            {"nope.env": "This file is not offered or is chosen twice."},
        ),
        "file chosen twice": lambda: (
            selection(web, (FileChoice(".env.web", "web"), FileChoice(".env.web", "web"))),
            {".env.web": "This file is not offered or is chosen twice."},
        ),
        "owner not selected": lambda: (
            selection(web, (FileChoice(".env.api", "api"),)),
            {".env.api": "Choose one of the selected services as the owner."},
        ),
        "type the content cannot have": lambda: (
            selection(web, (FileChoice(".env.web", "web", FileType.CERT),)),
            {
                ".env.web": (
                    "A certificate must hold at least one PEM block, "
                    "from -----BEGIN ...----- to -----END ...-----."
                )
            },
        ),
        "dist that was not suggested": lambda: (
            selection(web, (FileChoice(".env.web", "web", dist_path=".env.api"),)),
            {".env.web": "The dist file is not one of the suggested files."},
        ),
        "dist for a certificate": lambda: (
            selection(web, (FileChoice("certs/server.key", "web", dist_path=".env.api"),)),
            {"certs/server.key": "A certificate has no dist file."},
        ),
    }


@pytest.mark.parametrize("case", list(refused_choices()))
async def test_choices_that_do_not_match_the_scan_are_refused_before_anything_changes(
    harness: Harness, tmp_path: Path, case: str
) -> None:
    h = harness
    deployment, _ = await add_stack(h)
    before = await counts(h)
    chosen, errors = refused_choices()[case]()
    with pytest.raises(ComposeImportError) as caught:
        await run_import(h, deployment, chosen, tmp_path)
    assert caught.value.errors == errors
    assert await counts(h) == before


async def test_a_disabled_file_and_an_empty_selection_are_refused(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, root = await add_stack(h)
    outside = str(root.parent / "outside.env")
    with pytest.raises(ComposeImportError) as disabled:
        await run_import(
            h, deployment, selection(("api",), (FileChoice(outside, "api"),)), tmp_path
        )
    assert disabled.value.errors == {outside: "Outside the deployment folder or the allowed paths."}
    with pytest.raises(ComposeImportError) as nothing:
        await run_import(h, deployment, selection((), ()), tmp_path)
    assert str(nothing.value) == NO_SERVICE_MESSAGE


async def test_a_scan_that_fails_refuses_the_import(harness: Harness, tmp_path: Path) -> None:
    h = harness
    deployment, _ = await add_stack(
        h, {"compose.yaml": "include:\n  - oci://example.invalid/team/stack:1\nservices: {}\n"}
    )
    before = await counts(h)
    with pytest.raises(ComposeImportError) as caught:
        await run_import(h, deployment, selection(("web",), ()), tmp_path)
    assert str(caught.value) == REMOTE_MESSAGE
    assert await counts(h) == before
