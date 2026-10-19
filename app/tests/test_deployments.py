"""Deployment root checks, validation, create and update on the seeded database."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import (
    AuditEntry,
    Deployment,
    DistMissingPolicy,
    DriftPolicy,
    MissingTargetPolicy,
    Project,
)
from app.seeds import SeedData
from app.services import deployments as deployment_service
from app.services.deployments import (
    DeploymentForm,
    DeploymentFormError,
    check_root_path,
    create_deployment,
    update_deployment,
    validate_deployment,
)
from app.services.paths import AllowedPaths, PathStatus


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """An allowed host directory with two deployments, a file and an escaping symlink."""
    root = (tmp_path / "vhosts").resolve()
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    (root / "note.txt").write_text("not a folder")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "escape").symlink_to(elsewhere)
    (root / "loop").symlink_to(root / "loop")
    return root


@pytest.fixture
def allowed(host: Path) -> AllowedPaths:
    """The allowed path list holding only the host directory."""
    return AllowedPaths([host])


def lines(allowed: AllowedPaths, raw: str, host: Path) -> list[tuple[bool, str]]:
    """Check lines of a root path as (ok, text) pairs, the host directory counting as mounted."""
    check = check_root_path(allowed, raw, mount_points=[host])
    return [(line.ok, line.text) for line in check.lines]


def test_empty_input_has_no_lines(allowed: AllowedPaths, host: Path) -> None:
    assert lines(allowed, "   ", host) == []


def test_an_empty_allowed_list_asks_for_a_setting_first(host: Path) -> None:
    check = check_root_path(AllowedPaths([]), str(host / "alpha"))
    assert [line.ok for line in check.lines] == [False]
    assert "No allowed paths" in check.lines[0].text
    assert not check.ok


@pytest.mark.parametrize(
    ("raw", "message"),
    [("relative/dir", "Path must be absolute."), ("/x/../y", "Path must not contain '..'")],
)
def test_malformed_paths_are_refused_without_the_filesystem(
    allowed: AllowedPaths, host: Path, raw: str, message: str
) -> None:
    result = lines(allowed, raw, host)
    assert len(result) == 1 and not result[0][0]
    assert result[0][1].startswith(message)


def test_outside_path_reports_only_that_reason(
    allowed: AllowedPaths, host: Path, tmp_path: Path
) -> None:
    """An outside path gets one error whether or not anything exists there."""
    present = tmp_path / "elsewhere"
    answers = {
        str(path): lines(allowed, str(path), host)
        for path in (present, tmp_path / "missing", Path("/etc"), Path("/etc/passwd"))
    }
    for path, answer in answers.items():
        assert len(answer) == 1, path
        ok, text = answer[0]
        assert not ok
        assert text == f"Path is outside the allowed paths ({host})."
        assert "exist" not in text


def test_symlinks_that_leave_the_list_report_only_the_outside_reason(
    allowed: AllowedPaths, host: Path
) -> None:
    for raw in (host / "escape", host / "loop", host / "escape" / "deeper"):
        result = lines(allowed, str(raw), host)
        assert len(result) == 1 and "outside the allowed paths" in result[0][1], raw


def test_a_symlink_from_outside_into_the_list_is_typed_outside(
    allowed: AllowedPaths, host: Path, tmp_path: Path
) -> None:
    """The typed path decides before the disk is read, so outside stays outside."""
    link = tmp_path / "shortcut"
    link.symlink_to(host / "alpha")
    result = lines(allowed, str(link), host)
    assert len(result) == 1 and "outside the allowed paths" in result[0][1]


def test_directory_inside_passes_every_check_and_gives_the_real_path(
    allowed: AllowedPaths, host: Path
) -> None:
    check = check_root_path(allowed, f"  {host}/alpha/ ", mount_points=[host])
    assert check.ok and check.real_path == host / "alpha"
    assert [(line.ok, line.text) for line in check.lines] == [
        (True, f"Inside allowed path {host}"),
        (True, "Exists, mounted and writable"),
    ]


def test_inside_missing_and_file_paths_have_their_own_error(
    allowed: AllowedPaths, host: Path
) -> None:
    missing = check_root_path(allowed, str(host / "nope"), mount_points=[host])
    assert [line.text for line in missing.lines][1:] == ["Path does not exist."]
    assert not missing.ok and missing.message == "Path does not exist."
    plain = check_root_path(allowed, str(host / "note.txt"), mount_points=[host])
    assert [line.text for line in plain.lines][1:] == ["Path is not a directory."]


def test_inside_directory_must_be_mounted_and_writable(
    allowed: AllowedPaths, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unmounted = check_root_path(allowed, str(host / "alpha"), mount_points=[])
    assert [line.text for line in unmounted.lines][1:] == [
        "Path is not inside a mounted host directory."
    ]
    monkeypatch.setattr(
        deployment_service,
        "path_status",
        lambda path, mount_points=None: PathStatus(path=path, mounted=True, writable=False),
    )
    read_only = check_root_path(allowed, str(host / "alpha"), mount_points=[host])
    assert [line.text for line in read_only.lines][1:] == ["Path is not writable by the app."]
    assert not read_only.ok


async def test_validation_collects_every_problem(
    seeded_database: AsyncEngine, seed_data: SeedData, allowed: AllowedPaths, host: Path
) -> None:
    form = DeploymentForm(
        name="Bad Name",
        root="/etc",
        dist_missing_policy="maybe",
        file_mode="9999",
        file_owner="bad owner",
        file_group="x" * 65,
    )
    async with create_session_factory(seeded_database)() as db:
        project = await db.get(Project, seed_data.projects.fancy.id)
        assert project is not None
        with pytest.raises(DeploymentFormError) as caught:
            await validate_deployment(db, allowed, project, form, mount_points=[host])
    assert set(caught.value.errors) == {
        "name",
        "root",
        "policies",
        "file_mode",
        "file_owner",
        "file_group",
    }
    assert "outside the allowed paths" in caught.value.errors["root"]
    assert caught.value.root_check is not None and len(caught.value.root_check.lines) == 1


async def test_validation_requires_a_name_and_a_unique_one_per_project(
    seeded_database: AsyncEngine, seed_data: SeedData, allowed: AllowedPaths, host: Path
) -> None:
    async with create_session_factory(seeded_database)() as db:
        fancy = await db.get(Project, seed_data.projects.fancy.id)
        billing = await db.get(Project, seed_data.projects.billing.id)
        assert fancy is not None and billing is not None
        base = DeploymentForm(root=str(host / "alpha"))
        with pytest.raises(DeploymentFormError) as empty:
            await validate_deployment(db, allowed, fancy, base, mount_points=[host])
        assert empty.value.errors == {"name": "Enter a deployment name."}
        taken = DeploymentForm(name="prod", root=str(host / "alpha"))
        with pytest.raises(DeploymentFormError) as duplicate:
            await validate_deployment(db, allowed, fancy, taken, mount_points=[host])
        assert duplicate.value.errors == {
            "name": "A deployment named prod already exists in this project."
        }
        same_in_other_project = await validate_deployment(
            db,
            allowed,
            billing,
            DeploymentForm(name="prod", root=str(host / "alpha")),
            mount_points=[host],
        )
        assert same_in_other_project.name == "prod"
        keeps_own_name = await validate_deployment(
            db,
            allowed,
            fancy,
            taken,
            ignore_id=seed_data.deployments.fancy_prod.id,
            mount_points=[host],
        )
        assert keeps_own_name.name == "prod"


async def test_validation_normalizes_values(
    seeded_database: AsyncEngine, seed_data: SeedData, allowed: AllowedPaths, host: Path
) -> None:
    form = DeploymentForm(
        name="  alpha-1 ",
        root=str(host / "alpha"),
        dist_missing_policy="warn",
        drift_policy="backup_overwrite",
        missing_target_policy="halt",
        file_mode="640",
        file_owner=" www-data ",
        file_group="1000",
    )
    async with create_session_factory(seeded_database)() as db:
        project = await db.get(Project, seed_data.projects.fancy.id)
        assert project is not None
        clean = await validate_deployment(db, allowed, project, form, mount_points=[host])
    assert clean.name == "alpha-1" and clean.root_path == str(host / "alpha")
    assert clean.file_mode == "0640"
    assert (clean.file_owner, clean.file_group) == ("www-data", "1000")
    assert clean.dist_missing_policy is DistMissingPolicy.WARN
    assert clean.drift_policy is DriftPolicy.BACKUP_OVERWRITE
    assert clean.missing_target_policy is MissingTargetPolicy.HALT


async def test_create_stores_the_real_path_and_audits(
    seeded_database: AsyncEngine, seed_data: SeedData, allowed: AllowedPaths, host: Path
) -> None:
    (host / "gamma").mkdir()
    (host / "link-to-gamma").symlink_to(host / "gamma")
    form = DeploymentForm(name="gamma", root=str(host / "link-to-gamma"))
    actor = seed_data.users.superadmin
    async with create_session_factory(seeded_database)() as db:
        project = await db.get(Project, seed_data.projects.fancy.id)
        assert project is not None
        clean = await validate_deployment(db, allowed, project, form, mount_points=[host])
        created = await create_deployment(db, project, clean, actor=actor, ip_address="203.0.113.5")
    async with create_session_factory(seeded_database)() as db:
        stored = await db.get(Deployment, created.id)
        assert stored is not None
        assert stored.root_path == str(host / "gamma")
        assert (stored.file_mode, stored.file_owner, stored.file_group) == ("0600", None, None)
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "deployment.create"))
        ).scalar_one()
    assert entry.actor_name == actor.name and entry.ip_address == "203.0.113.5"
    assert (entry.target_type, entry.target_id, entry.target_name) == (
        "deployment",
        created.id,
        "gamma",
    )
    assert (entry.project_id, entry.deployment_id) == (project.id, created.id)
    assert entry.details["root_path"] == str(host / "gamma")
    assert entry.details["drift_policy"] == "halt"


async def test_update_changes_values_and_audits_only_what_changed(
    seeded_database: AsyncEngine, seed_data: SeedData, allowed: AllowedPaths, host: Path
) -> None:
    actor = seed_data.users.superadmin
    async with create_session_factory(seeded_database)() as db:
        project = await db.get(Project, seed_data.projects.fancy.id)
        deployment = await db.get(Deployment, seed_data.deployments.fancy_staging.id)
        assert project is not None and deployment is not None
        previous_root = deployment.root_path
        form = DeploymentForm.from_deployment(deployment)
        assert form.root == previous_root
        edited = DeploymentForm(
            name="staging-2",
            root=str(host / "beta"),
            dist_missing_policy="warn",
            drift_policy=form.drift_policy,
            missing_target_policy=form.missing_target_policy,
            file_mode="0640",
            file_owner="",
            file_group="",
        )
        clean = await validate_deployment(
            db, allowed, project, edited, ignore_id=deployment.id, mount_points=[host]
        )
        await update_deployment(db, deployment, clean, actor=actor, ip_address=None)
    async with create_session_factory(seeded_database)() as db:
        stored = await db.get(Deployment, seed_data.deployments.fancy_staging.id)
        assert stored is not None
        assert (stored.name, stored.root_path, stored.file_mode) == (
            "staging-2",
            str(host / "beta"),
            "0640",
        )
        assert stored.dist_missing_policy is DistMissingPolicy.WARN
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "deployment.update"))
        ).scalar_one()
    assert entry.details == {
        "changed": ["name", "root_path", "dist_missing_policy", "file_mode"],
        "previous_name": "staging",
        "previous_root_path": previous_root,
    }
    assert entry.target_name == "staging-2"
