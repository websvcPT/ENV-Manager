"""Tests for the dist comparison: report content, dist file reading and the seeded flow."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import Deployment, Environment, EnvironmentVersion
from app.seeds import SeedData
from app.services.dist import (
    MAX_DIST_BYTES,
    DistError,
    DistSkipReason,
    check_environment_dist,
    compare_text_to_dist,
    read_dist_file,
)
from app.services.env_parser import parse_env
from app.services.paths import AllowedPaths


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A real directory standing in for the allowed host root."""
    root = (tmp_path / "host").resolve()
    (root / "deploy" / "environments").mkdir(parents=True)
    return root


def test_report_lists_missing_extra_and_empty_keys() -> None:
    report = compare_text_to_dist(
        "A=1\nEXTRA=2\nEMPTY=\nB=3\n",
        "# template\nA=\nMISSING_ONE=\nB=\nMISSING_TWO=x\nEMPTY=\n",
    )
    assert report.skipped is None
    assert report.missing == ("MISSING_ONE", "MISSING_TWO")
    assert report.extra == ("EXTRA",)
    assert report.empty == ("EMPTY",)
    assert not report.clean


def test_identical_keys_are_clean() -> None:
    report = compare_text_to_dist("A=1\nB=2\n", "A=\nB=\n")
    assert report.clean
    assert report.missing == report.extra == report.empty == ()


def test_dist_empty_values_are_not_reported_only_the_environment_ones() -> None:
    report = compare_text_to_dist("A=1\n", "A=\nB=\n")
    assert report.empty == ()


def test_environment_that_is_not_env_format_skips_comparison() -> None:
    report = compare_text_to_dist("-----BEGIN CERTIFICATE-----\nMIIB==\n", "A=\n")
    assert report.skipped is DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT
    assert not report.clean
    assert report.missing == ()


def test_dist_that_is_not_env_format_skips_comparison() -> None:
    report = compare_text_to_dist("A=1\n", "just words\n")
    assert report.skipped is DistSkipReason.DIST_NOT_ENV_FORMAT


def test_duplicate_keys_count_once() -> None:
    report = compare_text_to_dist("A=1\nA=2\n", "A=\nA=\nB=\n")
    assert report.missing == ("B",)
    assert parse_env("A=1\nA=2\n").duplicate_keys == ("A",)


def test_read_dist_file_reads_inside_the_deployment(host: Path) -> None:
    (host / "deploy" / "environments" / ".env.db-dist").write_text("A=\n")
    text = read_dist_file(AllowedPaths([host]), str(host / "deploy"), "environments/.env.db-dist")
    assert text == "A=\n"


@pytest.mark.parametrize("dist_path", ["", "  ", "/etc/passwd", "../outside", "a/../../b"])
def test_read_dist_file_refuses_unsafe_relative_paths(host: Path, dist_path: str) -> None:
    with pytest.raises(DistError, match="relative path"):
        read_dist_file(AllowedPaths([host]), str(host / "deploy"), dist_path)


def test_read_dist_file_refuses_a_symlink_leaving_the_deployment(host: Path) -> None:
    (host / "other").mkdir()
    (host / "other" / "secret").write_text("A=\n")
    (host / "deploy" / "link").symlink_to(host / "other" / "secret")
    with pytest.raises(DistError, match="leaves the deployment root"):
        read_dist_file(AllowedPaths([host]), str(host / "deploy"), "link")


def test_read_dist_file_refuses_a_deployment_outside_the_allowed_paths(
    host: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env-dist").write_text("A=\n")
    with pytest.raises(DistError, match="outside the allowed paths"):
        read_dist_file(AllowedPaths([host]), str(elsewhere), ".env-dist")


def test_read_dist_file_refuses_a_missing_file_and_a_directory(host: Path) -> None:
    allowed = AllowedPaths([host])
    with pytest.raises(DistError, match="does not exist"):
        read_dist_file(allowed, str(host / "deploy"), "environments/nope")
    with pytest.raises(DistError, match="does not exist"):
        read_dist_file(allowed, str(host / "deploy"), "environments")


def test_read_dist_file_refuses_binary_and_oversized_files(host: Path) -> None:
    allowed = AllowedPaths([host])
    (host / "deploy" / "binary").write_bytes(b"\xff\xfe\x00A=1")
    (host / "deploy" / "big").write_bytes(b"A=1\n" * (MAX_DIST_BYTES // 4 + 1))
    with pytest.raises(DistError, match="UTF-8"):
        read_dist_file(allowed, str(host / "deploy"), "binary")
    with pytest.raises(DistError, match="too large"):
        read_dist_file(allowed, str(host / "deploy"), "big")


@pytest.fixture
async def relocated(seeded_database: AsyncEngine, seed_data: SeedData, host: Path) -> Path:
    """Point the seeded rentals prod deployment at a real folder and return the folder."""
    async with create_session_factory(seeded_database)() as db:
        deployment = await db.get(Deployment, seed_data.deployments.rentals_prod.id)
        assert deployment is not None
        deployment.root_path = str(host / "deploy")
        await db.commit()
    return host / "deploy"


async def test_check_environment_dist_on_the_seeded_environment(
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    crypto: CryptoService,
    relocated: Path,
    host: Path,
) -> None:
    (relocated / "environments" / ".env.db-dist").write_text("DB_HOST=\nDB_PORT=\nDB_NAME=\n")
    async with create_session_factory(seeded_database)() as db:
        report = await check_environment_dist(
            db, crypto, AllowedPaths([host]), seed_data.envs.db.id
        )
    assert report.skipped is None
    assert report.missing == ("DB_NAME",)
    assert report.extra == ()
    assert report.empty == ()


async def test_check_uses_the_latest_version(
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    crypto: CryptoService,
    relocated: Path,
    host: Path,
) -> None:
    (relocated / "environments" / ".env.db-dist").write_text("DB_HOST=\nDB_PORT=\n")
    content = "DB_HOST=db\nDB_PORT=\nNEW=1\n"
    async with create_session_factory(seeded_database)() as db:
        db.add(
            EnvironmentVersion(
                environment_id=seed_data.envs.db.id,
                number=3,
                content_encrypted=crypto.encrypt(content),
                content_hash=crypto.content_hash(content),
            )
        )
        await db.commit()
        report = await check_environment_dist(
            db, crypto, AllowedPaths([host]), seed_data.envs.db.id
        )
    assert report.extra == ("NEW",)
    assert report.empty == ("DB_PORT",)


async def test_check_skips_a_non_env_environment(
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    crypto: CryptoService,
    relocated: Path,
    host: Path,
) -> None:
    (relocated / "environments" / ".env.db-dist").write_text("DB_HOST=\n")
    certificate = "-----BEGIN CERTIFICATE-----\nMIIBabc+/==\n-----END CERTIFICATE-----\n"
    async with create_session_factory(seeded_database)() as db:
        db.add(
            EnvironmentVersion(
                environment_id=seed_data.envs.db.id,
                number=3,
                content_encrypted=crypto.encrypt(certificate),
                content_hash=crypto.content_hash(certificate),
            )
        )
        await db.commit()
        report = await check_environment_dist(
            db, crypto, AllowedPaths([host]), seed_data.envs.db.id
        )
    assert report.skipped is DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT


async def test_check_refuses_environments_without_a_dist_link_or_missing_ones(
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    crypto: CryptoService,
    relocated: Path,
    host: Path,
) -> None:
    allowed = AllowedPaths([host])
    async with create_session_factory(seeded_database)() as db:
        with pytest.raises(DistError, match="no dist file linked"):
            await check_environment_dist(db, crypto, allowed, seed_data.envs.db_secrets.id)
        with pytest.raises(DistError, match="does not exist"):
            await check_environment_dist(db, crypto, allowed, 9999)
        with pytest.raises(DistError, match="does not exist or is not a regular file"):
            await check_environment_dist(db, crypto, allowed, seed_data.envs.db.id)


async def test_check_refuses_when_the_deployment_left_the_allowed_paths(
    seeded_database: AsyncEngine,
    seed_data: SeedData,
    crypto: CryptoService,
    relocated: Path,
    tmp_path: Path,
) -> None:
    (relocated / "environments" / ".env.db-dist").write_text("DB_HOST=\n")
    other_root = tmp_path / "unrelated"
    other_root.mkdir()
    async with create_session_factory(seeded_database)() as db:
        with pytest.raises(DistError, match="outside the allowed paths"):
            await check_environment_dist(
                db, crypto, AllowedPaths([other_root]), seed_data.envs.db.id
            )


async def test_seed_links_dist_files(seeded_database: AsyncEngine, seed_data: SeedData) -> None:
    async with create_session_factory(seeded_database)() as db:
        db_env = await db.get(Environment, seed_data.envs.db.id)
        secrets = await db.get(Environment, seed_data.envs.db_secrets.id)
    assert db_env is not None
    assert db_env.dist_path == "environments/.env.db-dist"
    assert secrets is not None
    assert secrets.dist_path is None
