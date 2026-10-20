"""The demo project of the seed and the committed demo stack agree, without Docker.

The stack lives under ``demo/`` (Compose file, dist templates and the baseline of target files);
the seed describes the same deployment in the database. These tests read both and compare paths,
dist keys and baseline states, so a change to one without the other fails here.
"""

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings, get_settings
from app.core.crypto import CryptoService, generate_master_key
from app.core.db import create_engine, create_session_factory
from app.models import (
    AccessGrant,
    AllowedPath,
    Deployment,
    DistOutcome,
    Environment,
    EnvironmentVersion,
    FileType,
    HookType,
    Project,
    Service,
    ServiceHook,
    User,
)
from app.seeds import (
    DEMO_DEPLOYMENT_NAME,
    DEMO_FILES,
    DEMO_HOOK_COMMANDS,
    DEMO_PROJECT_NAME,
    DEMO_SERVICE_NAMES,
    DEMO_STACK_FOLDER,
    SeedData,
    SeedDemo,
    ensure_demo_project,
    seed,
)
from app.services.dist import DistSkipReason, compare_text_to_dist
from app.services.dist_check import evaluate_dist
from app.services.env_parser import parse_env
from app.services.permissions import (
    Capability,
    ItemLevel,
    compute_visibility,
    load_user_scope,
    permitted_environment_ids,
)

runner = CliRunner()
DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
STACK_DIR = DEMO_DIR / DEMO_STACK_FOLDER
BASELINE_DIR = DEMO_DIR / "baseline" / "environments"
EXPECTED_BASELINE = {
    ".env.api": "drift",
    ".env.db": "identical",
    ".env.db.secrets": "identical",
    "demo-ca.pem": "missing",
}


@pytest.fixture
async def demo_data(engine: AsyncEngine, crypto: CryptoService) -> SeedData:
    """The seed dataset with the demo project, rooted at the repository's demo folder."""
    async with create_session_factory(engine)() as session:
        return await seed(session, crypto, demo_root=DEMO_DIR)


def _demo(data: SeedData) -> SeedDemo:
    """The demo references of a seed run that was given a demo root."""
    assert data.demo is not None
    return data.demo


def _environments(demo: SeedDemo) -> dict[str, Environment]:
    """The demo environments by file name."""
    return {
        environment.name: environment
        for environment in (demo.env_api, demo.env_ca, demo.env_db, demo.env_db_secrets)
    }


async def _latest_text(db: AsyncSession, crypto: CryptoService, environment_id: int) -> str:
    """The decrypted content of the newest version of an environment."""
    latest = await db.scalar(
        select(EnvironmentVersion)
        .where(EnvironmentVersion.environment_id == environment_id)
        .order_by(EnvironmentVersion.number.desc())
        .limit(1)
    )
    assert latest is not None
    return crypto.decrypt_text(latest.content_encrypted)


def _compose_env_files(text: str) -> dict[str, list[str]]:
    """The env files each service of the Compose file loads, read from its text."""
    loaded: dict[str, list[str]] = {}
    service = ""
    listing = False
    for line in text.splitlines():
        if found := re.fullmatch(r"  (\w+):", line):
            service, listing = found.group(1), False
            loaded[service] = []
        elif line == "    env_file:":
            listing = True
        elif listing and (item := re.fullmatch(r"      - (\S+)", line)):
            loaded[service].append(item.group(1))
        elif line.startswith("    ") and not line.startswith("      "):
            listing = False
    return loaded


async def test_seed_without_a_demo_root_has_no_demo_project(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """The demo part is opt in: the default dataset is unchanged."""
    assert seed_data.demo is None
    async with create_session_factory(seeded_database)() as db:
        assert await db.scalar(select(Project).where(Project.name == DEMO_PROJECT_NAME)) is None


async def test_demo_structure_follows_the_committed_stack(
    engine: AsyncEngine, demo_data: SeedData
) -> None:
    """Names, root, allowed path, services, targets and dist links match the files on disk."""
    demo = _demo(demo_data)
    assert demo.project.name == DEMO_PROJECT_NAME
    assert demo.deployment.name == DEMO_DEPLOYMENT_NAME
    assert demo.deployment.root_path == str(STACK_DIR)
    assert demo.allowed_path.path == str(STACK_DIR)
    assert (STACK_DIR / "compose.yaml").is_file()
    async with create_session_factory(engine)() as db:
        service_names = select(Service.name).where(Service.deployment_id == demo.deployment.id)
        assert sorted((await db.execute(service_names)).scalars()) == sorted(DEMO_SERVICE_NAMES)
        paths = (await db.execute(select(AllowedPath.path))).scalars()
        assert str(STACK_DIR) in set(paths)
    environments = _environments(demo)
    assert sorted(environments) == sorted(demo_file.name for demo_file in DEMO_FILES)
    for demo_file in DEMO_FILES:
        environment = environments[demo_file.name]
        service = demo.api if demo_file.service == "api" else demo.db
        assert environment.service_id == service.id
        assert environment.target_path == demo_file.target_path
        assert environment.dist_path == demo_file.dist_path
        if demo_file.file_type is FileType.CERT:
            assert environment.dist_path is None
        else:
            assert (STACK_DIR / str(demo_file.dist_path)).is_file()
    assert [path.name for path in (STACK_DIR / "environments").glob("demo-ca.pem*")] == []


async def test_demo_services_store_the_recreate_command(
    engine: AsyncEngine, demo_data: SeedData
) -> None:
    """Each service recreates only its own workload after a deploy, and its command names it."""
    demo = _demo(demo_data)
    expected = {
        demo.api.id: "docker compose up -d --no-deps --force-recreate api",
        demo.db.id: "docker compose up -d --no-deps --force-recreate db",
    }
    async with create_session_factory(engine)() as db:
        for service in (demo.api, demo.db):
            hook = await db.scalar(select(ServiceHook).where(ServiceHook.service_id == service.id))
            assert hook is not None
            assert hook.type is HookType.POST_DEPLOY
            assert hook.commands == expected[service.id]
            assert hook.commands == DEMO_HOOK_COMMANDS[service.name]
            assert "{" not in hook.commands


async def test_demo_dist_comparison_covers_clean_and_blocked(
    engine: AsyncEngine, demo_data: SeedData, crypto: CryptoService
) -> None:
    """One file has a dist key the managed version lacks; the certificate has no dist file."""
    demo = _demo(demo_data)
    reports = {}
    async with create_session_factory(engine)() as db:
        for name, environment in _environments(demo).items():
            if environment.dist_path is None:
                continue
            dist_text = (STACK_DIR / environment.dist_path).read_text(encoding="utf-8")
            text = await _latest_text(db, crypto, environment.id)
            reports[name] = compare_text_to_dist(text, dist_text)
    assert sorted(reports) == [".env.api", ".env.db", ".env.db.secrets"]
    for name in (".env.api", ".env.db"):
        assert reports[name].clean, name
    blocked = reports[".env.db.secrets"]
    assert blocked.skipped is None
    assert blocked.missing == ("DEMO_DB_BACKUP_PASSWORD",)
    assert (blocked.extra, blocked.empty) == ((), ())


async def test_demo_dist_checks_read_the_committed_templates(
    engine: AsyncEngine, demo_data: SeedData, crypto: CryptoService
) -> None:
    """A real check of each demo file: two pass, the secrets file blocks, the certificate has no
    dist file and is not applicable."""
    demo = _demo(demo_data)
    outcomes = {}
    async with create_session_factory(engine)() as db:
        for name, environment in _environments(demo).items():
            outcomes[name] = await evaluate_dist(db, crypto, environment.id, checked_by="root")
    assert outcomes[".env.api"].outcome is DistOutcome.PASSED
    assert outcomes[".env.db"].outcome is DistOutcome.PASSED
    blocked = outcomes[".env.db.secrets"]
    assert blocked.outcome is DistOutcome.BLOCKED
    assert blocked.report is not None and blocked.report.missing == ("DEMO_DB_BACKUP_PASSWORD",)
    certificate = outcomes["demo-ca.pem"]
    assert certificate.outcome is DistOutcome.SKIPPED
    assert certificate.report is not None
    assert certificate.report.skipped is DistSkipReason.NOT_APPLICABLE
    assert demo.env_ca.file_type is FileType.CERT and demo.env_api.file_type is FileType.ENV


async def test_demo_baseline_has_one_drifted_identical_and_missing_targets(
    engine: AsyncEngine, demo_data: SeedData, crypto: CryptoService
) -> None:
    """The baseline folder restores a known state against the newest managed versions."""
    demo = _demo(demo_data)
    states = {}
    async with create_session_factory(engine)() as db:
        for name, environment in _environments(demo).items():
            baseline = BASELINE_DIR / name
            if not baseline.exists():
                states[name] = "missing"
                continue
            managed = await _latest_text(db, crypto, environment.id)
            same = baseline.read_text(encoding="utf-8") == managed
            states[name] = "identical" if same else "drift"
    assert states == EXPECTED_BASELINE
    assert sorted(path.name for path in BASELINE_DIR.iterdir()) == sorted(
        name for name, state in EXPECTED_BASELINE.items() if state != "missing"
    )


def test_compose_file_loads_the_managed_env_files_with_pinned_images() -> None:
    """Env files come through env_file, nothing is bind mounted and no image floats."""
    text = (STACK_DIR / "compose.yaml").read_text(encoding="utf-8")
    assert _compose_env_files(text) == {
        "api": ["environments/.env.api"],
        "db": ["environments/.env.db", "environments/.env.db.secrets"],
    }
    env_format = [file for file in DEMO_FILES if parse_env(file.latest).is_env_format]
    loaded = {path for paths in _compose_env_files(text).values() for path in paths}
    assert loaded == {file.target_path for file in env_format}
    images = re.findall(r"^\s+image:\s*(\S+)\s*$", text, re.MULTILINE)
    assert len(images) == 2
    assert all(re.fullmatch(r"(alpine|busybox):\d+\.\d+\.\d+", image) for image in images)
    assert not re.search(r"^\s*volumes:", text, re.MULTILINE)
    assert "cut -d= -f1" in text


def test_demo_values_are_marked_as_fake() -> None:
    """Every managed value and baseline file says it is a fake demo value."""
    for demo_file in DEMO_FILES:
        for content in demo_file.versions:
            assert "Fake values" in content or "not a real certificate" in content
    for baseline in BASELINE_DIR.iterdir():
        assert "Fake values" in baseline.read_text(encoding="utf-8")


async def test_demo_grants_give_scoped_users_part_of_the_project(
    engine: AsyncEngine, demo_data: SeedData
) -> None:
    """The project admin sees all of it, the file scoped admin one file, nobody else any."""
    demo = _demo(demo_data)
    users = demo_data.users
    async with create_session_factory(engine)() as db:
        environment_ids = [environment.id for environment in _environments(demo).values()]
        grants = (
            await db.execute(
                select(AccessGrant).where(
                    (AccessGrant.project_id == demo.project.id)
                    | (AccessGrant.deployment_id == demo.deployment.id)
                    | AccessGrant.environment_id.in_(environment_ids)
                )
            )
        ).scalars()
        assert {(grant.user_id, grant.environment_id) for grant in grants} == {
            (users.project_admin.id, None),
            (users.env_scoped_admin.id, demo.env_db.id),
        }
        deploy = Capability.DEPLOY
        project_admin = await load_user_scope(db, users.project_admin)
        both_files = sorted([demo.env_db.id, demo.env_db_secrets.id])
        assert (
            await permitted_environment_ids(db, project_admin, deploy, service_id=demo.db.id)
            == both_files
        )
        scoped = await load_user_scope(db, users.env_scoped_admin)
        assert await permitted_environment_ids(db, scoped, deploy, service_id=demo.db.id) == [
            demo.env_db.id
        ]
        assert await permitted_environment_ids(db, scoped, deploy, service_id=demo.api.id) == []
        for user in (
            users.admin,
            users.viewer,
            users.deployment_admin,
            users.env_scoped_viewer,
            users.ungranted_admin,
        ):
            visibility = await compute_visibility(db, await load_user_scope(db, user))
            assert not visibility.can_see(ItemLevel.PROJECT, demo.project.id), user.name
            assert not visibility.is_context_only(ItemLevel.PROJECT, demo.project.id), user.name


async def _counts(db: AsyncSession) -> list[int]:
    """Row counts of every table the demo seed writes."""
    return [
        (await db.execute(select(func.count()).select_from(table))).scalar_one()
        for table in (
            Project,
            Deployment,
            Service,
            ServiceHook,
            Environment,
            EnvironmentVersion,
            AccessGrant,
            AllowedPath,
        )
    ]


async def test_ensure_demo_project_is_repeatable_and_follows_a_moved_root(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService, tmp_path: Path
) -> None:
    """A database seeded without the demo gets it once; a new root moves the deployment."""
    async with create_session_factory(seeded_database)() as db:
        before = await _counts(db)
        first = await ensure_demo_project(db, crypto, DEMO_DIR)
        await db.commit()
        assert first is not None
        created = await _counts(db)
        assert created != before
        again = await ensure_demo_project(db, crypto, DEMO_DIR)
        await db.commit()
        assert again is not None and again.deployment.id == first.deployment.id
        assert await _counts(db) == created

        moved = await ensure_demo_project(db, crypto, tmp_path / "moved")
        await db.commit()
        assert moved is not None and moved.deployment.id == first.deployment.id
        assert moved.deployment.root_path == str(tmp_path / "moved" / DEMO_STACK_FOLDER)
        paths = set((await db.execute(select(AllowedPath.path))).scalars())
        assert str(tmp_path / "moved" / DEMO_STACK_FOLDER) in paths


async def test_ensure_demo_project_needs_the_superadmin(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """An empty database gets nothing."""
    async with create_session_factory(engine)() as db:
        assert await ensure_demo_project(db, crypto, DEMO_DIR) is None
        assert await db.scalar(select(func.count()).select_from(User)) == 0


@pytest.fixture
def cli_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at an empty data folder with a generated master key, demo root unset."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(data_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    monkeypatch.delenv("ENVMANAGER_DEMO_ROOT", raising=False)
    get_settings.cache_clear()
    yield data_path
    get_settings.cache_clear()


def _deployment_roots(data_path: Path) -> list[str]:
    """Root paths of the demo deployments in the CLI's database."""

    async def read() -> list[str]:
        engine = create_engine(data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                rows = await db.execute(
                    select(Deployment.root_path)
                    .join(Project, Project.id == Deployment.project_id)
                    .where(Project.name == DEMO_PROJECT_NAME)
                )
                return list(rows.scalars())
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_dev_seed_creates_the_demo_project_only_with_a_demo_root(
    cli_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command seeds the demo project when ENVMANAGER_DEMO_ROOT is set, never otherwise."""
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    assert _deployment_roots(cli_data) == []

    monkeypatch.setenv("ENVMANAGER_DEMO_ROOT", str(DEMO_DIR))
    get_settings.cache_clear()
    again = runner.invoke(app, ["dev", "seed"])
    assert again.exit_code == 1 and "already seeded" in again.output
    assert _deployment_roots(cli_data) == [str(STACK_DIR)]
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1
    assert _deployment_roots(cli_data) == [str(STACK_DIR)]


def test_dev_seed_creates_the_demo_project_on_a_new_database(
    cli_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first run with the demo root set reports the demo deployment root."""
    monkeypatch.setenv("ENVMANAGER_DEMO_ROOT", str(DEMO_DIR))
    get_settings.cache_clear()
    result = runner.invoke(app, ["dev", "seed"])
    assert result.exit_code == 0, result.output
    assert f"demo project rooted at {STACK_DIR}" in result.output
    assert _deployment_roots(cli_data) == [str(STACK_DIR)]


def test_settings_demo_root_must_be_absolute_and_empty_means_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative demo root fails startup configuration; an empty value is unset."""
    monkeypatch.setenv("ENVMANAGER_DEMO_ROOT", "demo")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("ENVMANAGER_DEMO_ROOT", "")
    assert Settings().demo_root is None
    monkeypatch.setenv("ENVMANAGER_DEMO_ROOT", "/srv/demo")
    assert Settings().demo_root == Path("/srv/demo")
