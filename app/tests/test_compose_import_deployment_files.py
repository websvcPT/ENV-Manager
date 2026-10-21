"""The Compose import of a deployment's own files and of the post-deploy commands it offers."""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    DEPLOYMENT_FILES_NAME,
    Deployment,
    DeployStatus,
    DeployTrigger,
    Environment,
    HookType,
    Service,
    ServiceHook,
)
from app.services.compose_import import (
    COMMAND_OWNER_MESSAGE,
    EMPTY_COMMAND_MESSAGE,
    HAS_SERVICES_MESSAGE,
    ComposeImportError,
    ComposeSelection,
    FileChoice,
    import_compose,
)
from app.services.compose_scan import IN_FILES_REASON
from app.services.docker_cli import NOT_MOUNTED_REASON, DockerCli
from app.services.hook_settings import UNAVAILABLE_SUFFIX
from app.services.services import ensure_deployment_files, find_deployment_files
from app.services.versions import create_version
from app.tests.compose_helpers import GEOTRAVEL, add_stack, mounted_cli, real_cli
from app.tests.deploy_helpers import Harness, audit_of, runs, version_text

SERVICES = ("api", "db", "frontend")
SERVICE_FILES = (FileChoice("environments/.env.db", "db"),)
OWN_FILES = (FileChoice(".env", DEPLOYMENT_FILES_NAME),)
UP = "docker compose -f docker-compose.yml up -d"


def geotravel(
    files: tuple[FileChoice, ...] = SERVICE_FILES + OWN_FILES,
    commands: tuple[tuple[str, str], ...] = (),
    services: tuple[str, ...] = SERVICES,
) -> ComposeSelection:
    """The choices of the GeoTravel shaped project."""
    return ComposeSelection(("docker-compose.yml",), services, files, commands)


async def run_import(
    h: Harness, deployment: Deployment, chosen: ComposeSelection, cli: DockerCli
) -> None:
    """Import as the seeded superadmin."""
    await import_compose(h.db, h.crypto, cli, deployment, chosen, actor=h.seed.users.superadmin)


async def stored_hooks(h: Harness, deployment: Deployment) -> dict[str, ServiceHook]:
    """The post-deploy commands of the deployment by service name."""
    rows = await h.db.execute(
        select(Service.name, ServiceHook)
        .join(ServiceHook, ServiceHook.service_id == Service.id)
        .where(Service.deployment_id == deployment.id, ServiceHook.type == HookType.POST_DEPLOY)
    )
    return dict(rows.all())


async def totals(h: Harness) -> tuple[int, int, int]:
    """How many services, environments and post-deploy commands exist."""
    return (
        (await h.db.execute(select(func.count(Service.id)))).scalar_one(),
        (await h.db.execute(select(func.count(Environment.id)))).scalar_one(),
        (await h.db.execute(select(func.count(ServiceHook.id)))).scalar_one(),
    )


async def test_the_deployment_env_goes_to_the_deployment_files_and_the_compose_file_is_linked(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(h, GEOTRAVEL, name="geotravel")
    result = await import_compose(
        h.db, h.crypto, real_cli(tmp_path), deployment, geotravel(), actor=h.seed.users.superadmin
    )
    assert (result.services, result.environments, result.deployment_files) == (3, 2, 1)
    files = await find_deployment_files(h.db, deployment.id)
    assert files is not None
    rows = await h.db.scalars(select(Environment).where(Environment.service_id == files.id))
    own = {environment.target_path: environment for environment in rows}
    assert list(own) == [".env"]
    assert await version_text(h.db, h.crypto, own[".env"].id, 1) == GEOTRAVEL[".env"]
    await h.db.refresh(deployment)
    assert deployment.compose_files == ["docker-compose.yml"]
    names = await h.db.scalars(select(Service.name).where(Service.deployment_id == deployment.id))
    assert sorted(names) == ["Deployment files", "api", "db", "frontend"]
    imported = [run for run in await runs(h.db) if run.trigger is DeployTrigger.COMPOSE]
    assert sorted(run.service_name for run in imported) == ["Deployment files", "db"]
    (entry,) = await audit_of(h.db, "deployment.import_compose")
    details = entry.details
    assert details["services"] == list(SERVICES) and details["deployment_files"] == 1


async def test_a_compose_file_cannot_be_imported_as_a_deployment_file(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(h, GEOTRAVEL, name="geotravel")
    before = await totals(h)
    chosen = geotravel(files=(FileChoice("docker-compose.yml", DEPLOYMENT_FILES_NAME),))
    with pytest.raises(ComposeImportError) as refused:
        await run_import(h, deployment, chosen, real_cli(tmp_path))
    assert refused.value.errors == {
        "docker-compose.yml": "This file is not offered or is chosen twice."
    }
    assert await totals(h) == before


async def test_the_first_deploy_of_the_deployment_files_finds_them_in_sync(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, root = await add_stack(h, GEOTRAVEL, name="geotravel")
    await run_import(h, deployment, geotravel(), real_cli(tmp_path))
    files = await find_deployment_files(h.db, deployment.id)
    assert files is not None
    outcome = await h.deploy(files.id, user="superadmin")
    assert outcome.status is DeployStatus.SUCCESS
    assert (root / ".env").read_text() == GEOTRAVEL[".env"]
    assert h.runner.requests[-1].changed is False


async def test_ticked_commands_are_stored_with_the_form_defaults_and_their_own_audit(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(h, GEOTRAVEL, name="geotravel")
    api = f"{UP} --no-deps api"
    commands = ((DEPLOYMENT_FILES_NAME, f"  {UP}  \n\n"), ("api", api))
    result = await import_compose(
        h.db,
        h.crypto,
        mounted_cli(tmp_path),
        deployment,
        geotravel(commands=commands),
        actor=h.seed.users.superadmin,
    )
    assert result.commands == 2
    hooks = await stored_hooks(h, deployment)
    assert sorted(hooks) == ["Deployment files", "api"]
    assert hooks[DEPLOYMENT_FILES_NAME].commands == UP and hooks["api"].commands == api
    for hook in hooks.values():
        assert hook.working_directory is None and hook.timeout_seconds == 120
        assert hook.run_only_if_changed is True
    saved = await audit_of(h.db, "service.hook_save")
    assert sorted(str(entry.target_name) for entry in saved) == ["Deployment files", "api"]
    by_name = {entry.target_name: entry.details for entry in saved}
    assert by_name["api"]["new_commands"] == api and by_name["api"]["previous_commands"] == ""
    (entry,) = await audit_of(h.db, "deployment.import_compose")
    details = entry.details
    assert details["post_deploy"] == ["Deployment files", "api"]
    assert "docker compose" not in repr(details)


async def test_a_stored_command_runs_after_a_deploy_that_changes_a_deployment_file(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, root = await add_stack(h, GEOTRAVEL, name="geotravel")
    chosen = geotravel(commands=((DEPLOYMENT_FILES_NAME, UP),))
    await run_import(h, deployment, chosen, mounted_cli(tmp_path))
    files = await find_deployment_files(h.db, deployment.id)
    assert files is not None
    env = await h.db.scalar(
        select(Environment).where(Environment.service_id == files.id, Environment.name == ".env")
    )
    assert env is not None
    changed = "ENVIRONMENT_NAME=prod\nTZ=Europe/Lisbon\n"
    await create_version(h.db, h.crypto, env.id, changed, author=h.seed.users.superadmin)
    await h.db.commit()
    outcome = await h.deploy(files.id, user="superadmin")
    assert outcome.status is DeployStatus.SUCCESS
    assert (root / ".env").read_text() == changed
    request = h.runner.requests[-1]
    assert request.changed is True and request.hook is not None
    assert request.hook.commands == UP


async def test_no_command_is_stored_without_a_usable_docker_and_nothing_changes(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, _ = await add_stack(h, GEOTRAVEL, name="geotravel")
    before = await totals(h)
    with pytest.raises(ComposeImportError) as refused:
        await run_import(h, deployment, geotravel(commands=(("api", UP),)), real_cli(tmp_path))
    assert str(refused.value) == f"{NOT_MOUNTED_REASON} {UNAVAILABLE_SUFFIX}"
    assert await totals(h) == before


@pytest.mark.parametrize(
    ("chosen", "key", "message"),
    [
        (
            geotravel(commands=(("api", "docker compose down -v"),)),
            "command:api",
            None,
        ),
        (geotravel(commands=(("api", "ls -la"),)), "command:api", None),
        (geotravel(commands=(("db", "  \n "),)), "command:db", EMPTY_COMMAND_MESSAGE),
        (
            geotravel(commands=(("api", UP),), services=("db", "frontend")),
            "command:api",
            COMMAND_OWNER_MESSAGE,
        ),
        (
            geotravel(files=SERVICE_FILES, commands=((DEPLOYMENT_FILES_NAME, UP),)),
            f"command:{DEPLOYMENT_FILES_NAME}",
            COMMAND_OWNER_MESSAGE,
        ),
    ],
    ids=["safety-net", "not-docker", "empty", "service-not-chosen", "no-deployment-file"],
)
async def test_a_refused_command_changes_nothing(
    harness: Harness,
    tmp_path: Path,
    chosen: ComposeSelection,
    key: str,
    message: str | None,
) -> None:
    h = harness
    deployment, _ = await add_stack(h, GEOTRAVEL, name="geotravel")
    before = await totals(h)
    with pytest.raises(ComposeImportError) as refused:
        await run_import(h, deployment, chosen, mounted_cli(tmp_path))
    assert list(refused.value.errors) == [key]
    if message is not None:
        assert refused.value.errors[key] == message
    assert await totals(h) == before


async def test_deployment_files_made_before_the_import_keep_their_env_and_lose_the_compose_file(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    deployment, root = await add_stack(h, GEOTRAVEL, name="geotravel")
    files = await ensure_deployment_files(h.db, deployment, actor=h.seed.users.superadmin)
    h.db.add(Environment(service_id=files.id, name=".env", target_path=".env"))
    managed = Environment(
        service_id=files.id, name="docker-compose.yml", target_path="docker-compose.yml"
    )
    h.db.add(managed)
    await h.db.flush()
    await create_version(
        h.db, h.crypto, managed.id, GEOTRAVEL["docker-compose.yml"], author=h.seed.users.superadmin
    )
    await h.db.commit()
    with pytest.raises(ComposeImportError) as refused:
        await run_import(h, deployment, geotravel(), real_cli(tmp_path))
    assert refused.value.errors == {".env": IN_FILES_REASON}
    await h.db.refresh(deployment)
    await h.db.refresh(files)
    await run_import(h, deployment, geotravel(files=SERVICE_FILES), real_cli(tmp_path))
    rows = await h.db.scalars(select(Environment.name).where(Environment.service_id == files.id))
    assert sorted(rows) == [".env"]
    (removed,) = [
        entry
        for entry in await audit_of(h.db, "environment.delete")
        if entry.target_name == "docker-compose.yml"
    ]
    assert removed.details == {
        "versions": 1,
        "target_path": "docker-compose.yml",
        "reason": "compose_file_linked",
    }
    assert (root / "docker-compose.yml").read_text() == GEOTRAVEL["docker-compose.yml"]
    with pytest.raises(ComposeImportError) as again:
        await run_import(h, deployment, geotravel(files=SERVICE_FILES), real_cli(tmp_path))
    assert str(again.value) == HAS_SERVICES_MESSAGE
