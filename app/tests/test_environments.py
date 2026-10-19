"""Add environment validation and creation on the seeded database."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import AuditEntry, Deployment, Environment, EnvironmentVersion, Service
from app.seeds import SeedData
from app.services.content import MAX_CONTENT_BYTES
from app.services.environments import (
    NEW_SERVICE,
    OUTSIDE_MESSAGE,
    ContentSource,
    EnvironmentForm,
    EnvironmentFormError,
    create_environment,
    target_hint,
    validate_environment,
)
from app.services.paths import AllowedPaths


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A real directory standing in for the allowed host root, with the deployment folder."""
    root = (tmp_path / "host").resolve()
    (root / "deploy" / "environments").mkdir(parents=True)
    return root


@pytest.fixture
async def relocated(seeded_database: AsyncEngine, seed_data: SeedData, host: Path) -> Path:
    """Point the seeded prod deployment at the real folder and return the folder."""
    async with create_session_factory(seeded_database)() as db:
        deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
        assert deployment is not None
        deployment.root_path = str(host / "deploy")
        await db.commit()
    return host / "deploy"


@pytest.fixture
async def db(seeded_database: AsyncEngine, relocated: Path) -> AsyncIterator[AsyncSession]:
    """A session on the seeded database after the deployment moved to the real folder."""
    async with create_session_factory(seeded_database)() as session:
        yield session


def form_for(seed_data: SeedData, **overrides: Any) -> EnvironmentForm:
    """A valid form for a new file of the seeded mysql service."""
    values = {
        "service": str(seed_data.services.mysql.id),
        "name": ".env.cache",
        "target_path": "environments/.env.cache",
        "content": "CACHE_HOST=cache\n",
    }
    return EnvironmentForm(**{**values, **overrides})


async def refused(
    db: AsyncSession, host: Path, seed_data: SeedData, form: EnvironmentForm
) -> dict[str, str]:
    """The field errors a form is refused with."""
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    with pytest.raises(EnvironmentFormError) as caught:
        await validate_environment(db, AllowedPaths([host]), deployment, form)
    return caught.value.errors


async def test_a_valid_form_gives_clean_values(
    db: AsyncSession, host: Path, seed_data: SeedData
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    form = form_for(
        seed_data,
        target_path=" environments/./.env.cache ",
        dist_path="environments/.env.cache-dist",
        file_mode="640",
        file_owner="www-data",
        content="A=1\r\nB=2\r\n",
    )
    clean = await validate_environment(db, AllowedPaths([host]), deployment, form)
    assert clean.service_id == seed_data.services.mysql.id and clean.new_service_name is None
    assert clean.target_path == "environments/.env.cache"
    assert clean.dist_path == "environments/.env.cache-dist"
    assert (clean.file_mode, clean.file_owner, clean.file_group) == ("0640", "www-data", None)
    assert clean.content == "A=1\nB=2\n"
    assert clean.source is ContentSource.PASTE


async def test_empty_attributes_inherit_the_deployment(
    db: AsyncSession, host: Path, seed_data: SeedData
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    clean = await validate_environment(
        db, AllowedPaths([host]), deployment, form_for(seed_data, content="")
    )
    assert (clean.file_mode, clean.file_owner, clean.file_group) == (None, None, None)
    assert clean.content == ""


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Enter a file name."),
        ("..", "Use 1 to 100 letters"),
        ("a b", "Use 1 to 100 letters"),
        ("a/b", "Use 1 to 100 letters"),
        ("x" * 101, "Use 1 to 100 letters"),
        (".env.db", "This service already has a file named .env.db."),
    ],
)
async def test_environment_names(
    db: AsyncSession, host: Path, seed_data: SeedData, name: str, message: str
) -> None:
    errors = await refused(db, host, seed_data, form_for(seed_data, name=name))
    assert errors["name"].startswith(message)


@pytest.mark.parametrize("name", [".env.cache", "web-ca.pem", "cache_1.conf", "a"])
async def test_valid_environment_names(
    db: AsyncSession, host: Path, seed_data: SeedData, name: str
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    await validate_environment(db, AllowedPaths([host]), deployment, form_for(seed_data, name=name))


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("", "Enter a target path."),
        ("../../etc/cache.env", OUTSIDE_MESSAGE),
        ("a/../../b", OUTSIDE_MESSAGE),
        ("/etc/cache.env", "Use a path relative to the deployment root."),
        (".", OUTSIDE_MESSAGE),
        ("environments", "Target path is a directory, not a file."),
        ("environments/.env.db", "Another file of this deployment already uses this target path."),
    ],
)
async def test_target_path_rules(
    db: AsyncSession,
    host: Path,
    seed_data: SeedData,
    target: str,
    message: str,
) -> None:
    errors = await refused(db, host, seed_data, form_for(seed_data, target_path=target))
    assert errors["target_path"] == message


async def test_a_target_through_a_symlink_that_leaves_the_root_is_refused(
    db: AsyncSession, host: Path, seed_data: SeedData, tmp_path: Path
) -> None:
    (host / "deploy" / "out").symlink_to(tmp_path)
    errors = await refused(db, host, seed_data, form_for(seed_data, target_path="out/file"))
    assert errors["target_path"] == OUTSIDE_MESSAGE


async def test_a_deployment_root_outside_the_allowed_paths_refuses_targets(
    db: AsyncSession, host: Path, seed_data: SeedData, tmp_path: Path
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    with pytest.raises(EnvironmentFormError) as caught:
        await validate_environment(
            db, AllowedPaths([tmp_path / "unrelated"]), deployment, form_for(seed_data)
        )
    assert caught.value.errors["target_path"] == "The deployment root is outside the allowed paths."


async def test_dist_path_rules(db: AsyncSession, host: Path, seed_data: SeedData) -> None:
    outside = await refused(db, host, seed_data, form_for(seed_data, dist_path="../x"))
    assert outside["dist_path"] == OUTSIDE_MESSAGE
    same = await refused(
        db, host, seed_data, form_for(seed_data, dist_path="environments/.env.cache")
    )
    assert same["dist_path"] == "The dist file must be a different file from the target."


async def test_attribute_and_service_errors_are_collected_together(
    db: AsyncSession, host: Path, seed_data: SeedData
) -> None:
    errors = await refused(
        db,
        host,
        seed_data,
        form_for(
            seed_data,
            service="999",
            file_mode="9999",
            file_owner="bad name",
            file_group="g" * 65,
            source="elsewhere",
        ),
    )
    assert set(errors) == {"service", "file_mode", "file_owner", "file_group", "source"}


@pytest.mark.parametrize("service", ["", "abc", "1" * 40, "-1"])
async def test_an_unknown_service_is_refused(
    db: AsyncSession, host: Path, seed_data: SeedData, service: str
) -> None:
    errors = await refused(db, host, seed_data, form_for(seed_data, service=service))
    assert errors["service"] == "Choose a service."


async def test_a_service_of_another_deployment_is_refused(
    db: AsyncSession, host: Path, seed_data: SeedData
) -> None:
    errors = await refused(
        db, host, seed_data, form_for(seed_data, service=str(seed_data.services.api.id))
    )
    assert errors["service"] == "Choose a service."


async def test_a_typed_new_service_wins_over_the_selection(
    db: AsyncSession, host: Path, seed_data: SeedData
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    clean = await validate_environment(
        db, AllowedPaths([host]), deployment, form_for(seed_data, new_service="redis")
    )
    assert clean.service_id is None and clean.new_service_name == "redis"


@pytest.mark.parametrize(
    ("new_service", "message"),
    [
        ("", "Enter a service name."),
        ("Redis", "Use 1 to 100 lowercase"),
        ("web", "A service named web already exists in this deployment."),
    ],
)
async def test_new_service_name_rules(
    db: AsyncSession, host: Path, seed_data: SeedData, new_service: str, message: str
) -> None:
    form = form_for(seed_data, service=NEW_SERVICE, new_service=new_service)
    errors = await refused(db, host, seed_data, form)
    assert errors["new_service"].startswith(message)


async def test_paste_is_checked_for_binary_and_size(
    db: AsyncSession, host: Path, seed_data: SeedData
) -> None:
    binary = await refused(db, host, seed_data, form_for(seed_data, content="A\x00B"))
    assert binary["content"] == "Binary content is not supported."
    big = await refused(
        db, host, seed_data, form_for(seed_data, content="A" * (MAX_CONTENT_BYTES + 1))
    )
    assert big["content"] == "Content is larger than 256 KiB."


async def test_upload_rules(db: AsyncSession, host: Path, seed_data: SeedData) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    allowed = AllowedPaths([host])
    ok = await validate_environment(
        db, allowed, deployment, form_for(seed_data, source="upload", upload=b"A=1\r\n")
    )
    assert ok.content == "A=1\r\n" and ok.source is ContentSource.UPLOAD
    for upload, message in (
        (None, "Choose a text file to upload."),
        (b"\xff\xfe", "The file is not valid UTF-8 text."),
        (b"A\x00B", "Binary content is not supported."),
        (b"A" * (MAX_CONTENT_BYTES + 1), "The file is larger than 256 KiB."),
    ):
        errors = await refused(
            db, host, seed_data, form_for(seed_data, source="upload", upload=upload)
        )
        assert errors["upload"] == message


async def test_import_reads_the_target_file(
    db: AsyncSession, host: Path, seed_data: SeedData, relocated: Path
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    (relocated / "environments" / ".env.cache").write_text("CACHE_HOST=live\r\n")
    clean = await validate_environment(
        db, AllowedPaths([host]), deployment, form_for(seed_data, source="import", content="x")
    )
    assert clean.content == "CACHE_HOST=live\r\n" and clean.source is ContentSource.IMPORT


async def test_import_refusals(
    db: AsyncSession, host: Path, seed_data: SeedData, relocated: Path, tmp_path: Path
) -> None:
    form = form_for(seed_data, source="import")
    missing = await refused(db, host, seed_data, form)
    assert missing["source"] == "The target file does not exist or is not a regular file."
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET=1\n")
    (relocated / "environments" / ".env.cache").symlink_to(outside)
    escaped = await refused(db, host, seed_data, form)
    assert escaped["target_path"] == OUTSIDE_MESSAGE
    assert escaped["source"] == "Fix the target path to import the file."
    (relocated / "environments" / ".env.cache").unlink()
    (relocated / "environments" / ".env.cache").write_bytes(b"\xff\xfe")
    binary = await refused(db, host, seed_data, form)
    assert binary["source"] == "The target file is not valid UTF-8 text."
    (relocated / "environments" / ".env.cache").write_bytes(b"A" * (MAX_CONTENT_BYTES + 1))
    big = await refused(db, host, seed_data, form)
    assert big["source"] == "The target file is too large to import."
    bad_form = form_for(seed_data, source="import", target_path="../x")
    bad_target = await refused(db, host, seed_data, bad_form)
    assert bad_target["source"] == "Fix the target path to import the file."


async def test_create_stores_the_environment_version_one_and_audit(
    seeded_database: AsyncEngine,
    db: AsyncSession,
    host: Path,
    seed_data: SeedData,
    crypto: CryptoService,
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    form = form_for(
        seed_data,
        dist_path="environments/.env.cache-dist",
        file_mode="0640",
        content="CACHE_SECRET=hunter2\n",
    )
    clean = await validate_environment(db, AllowedPaths([host]), deployment, form)
    created = await create_environment(
        db, crypto, deployment, clean, actor=seed_data.users.superadmin, ip_address="10.0.0.9"
    )
    async with create_session_factory(seeded_database)() as check:
        stored = await check.get(Environment, created.id)
        assert stored is not None
        assert (stored.name, stored.target_path, stored.file_mode) == (
            ".env.cache",
            "environments/.env.cache",
            "0640",
        )
        assert stored.dist_path == "environments/.env.cache-dist"
        assert stored.file_owner is None and stored.service_id == seed_data.services.mysql.id
        versions = (
            (
                await check.execute(
                    select(EnvironmentVersion).where(
                        EnvironmentVersion.environment_id == created.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [version.number for version in versions] == [1]
        assert crypto.decrypt_text(versions[0].content_encrypted) == "CACHE_SECRET=hunter2\n"
        assert versions[0].author_id == seed_data.users.superadmin.id
        entry = (
            await check.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "environment.create",
                    AuditEntry.environment_id == created.id,
                )
            )
        ).scalar_one()
        assert entry.version_ids == [versions[0].id] and entry.ip_address == "10.0.0.9"
        assert entry.service_id == seed_data.services.mysql.id
        assert entry.details["source"] == "paste" and entry.details["service_created"] is False
        assert "hunter2" not in str(entry.details) and "CACHE_SECRET" not in str(entry.details)


async def test_create_with_a_new_service_is_one_transaction_with_two_audits(
    seeded_database: AsyncEngine,
    db: AsyncSession,
    host: Path,
    seed_data: SeedData,
    crypto: CryptoService,
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    form = form_for(seed_data, service=NEW_SERVICE, new_service="redis", name="redis.conf")
    clean = await validate_environment(db, AllowedPaths([host]), deployment, form)
    created = await create_environment(
        db, crypto, deployment, clean, actor=seed_data.users.superadmin
    )
    async with create_session_factory(seeded_database)() as check:
        service = (
            await check.execute(
                select(Service).where(
                    Service.deployment_id == deployment.id, Service.name == "redis"
                )
            )
        ).scalar_one()
        assert created.service_id == service.id
        actions = set(
            (
                await check.execute(
                    select(AuditEntry.action).where(AuditEntry.service_id == service.id)
                )
            ).scalars()
        )
        assert actions == {"service.create", "environment.create"}


async def test_a_name_taken_between_validation_and_insert_rolls_everything_back(
    seeded_database: AsyncEngine,
    db: AsyncSession,
    host: Path,
    seed_data: SeedData,
    crypto: CryptoService,
) -> None:
    deployment = await db.get(Deployment, seed_data.deployments.fancy_prod.id)
    assert deployment is not None
    clean = await validate_environment(db, AllowedPaths([host]), deployment, form_for(seed_data))
    async with create_session_factory(seeded_database)() as other:
        other.add(
            Environment(
                service_id=seed_data.services.mysql.id,
                name=".env.cache",
                target_path="environments/other",
            )
        )
        await other.commit()
    with pytest.raises(EnvironmentFormError) as caught:
        await create_environment(db, crypto, deployment, clean, actor=seed_data.users.superadmin)
    assert caught.value.errors == {"name": "This service already has a file named .env.cache."}
    async with create_session_factory(seeded_database)() as check:
        count = (
            (
                await check.execute(
                    select(AuditEntry).where(AuditEntry.action == "environment.create")
                )
            )
            .scalars()
            .all()
        )
        assert count == []


def test_target_hint_states(host: Path) -> None:
    allowed = AllowedPaths([host])
    root = str(host / "deploy")
    assert target_hint(allowed, root, "").text == f"Resolves to {root}/"
    good = target_hint(allowed, root, "environments/.env.cache")
    assert good.ok and good.text == f"Resolves to {root}/environments/.env.cache"
    bad = target_hint(allowed, root, "../../etc/cache.env")
    assert not bad.ok and bad.text == OUTSIDE_MESSAGE
