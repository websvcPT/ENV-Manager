"""Dist checks on the seed: outcomes under the policies, skipped and failed states, audit entries
and adding the missing keys through the editor save."""

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import (
    AllowedPath,
    AuditResult,
    DistMissingPolicy,
    DistOutcome,
    EnvironmentVersion,
    FileType,
)
from app.seeds import SeedData
from app.services.dist import DistError, DistProblem, DistSkipReason
from app.services.dist_check import (
    ADDED_COMMENT,
    NOTHING_TO_ADD_MESSAGE,
    STALE_MESSAGE,
    add_missing_keys,
    evaluate_dist,
    run_deployment_dist_checks,
    run_dist_check,
)
from app.services.editor import EditorError, EditorHeader, load_editor_header, save_version
from app.services.env_parser import parse_env
from app.services.permissions import load_user_scope
from app.tests.audit_helpers import audit_entries
from app.tests.dist_helpers import (
    link_dist,
    relocate_deployments,
    set_deployment_policy,
    write_dist,
)
from app.tests.file_type_helpers import set_file_type

DIST_DEFAULT_VALUE = "dist-default-Qw83"
DB_DIST = "environments/.env.db-dist"


@pytest.fixture
async def roots(db: AsyncSession, tmp_path: Path) -> dict[str, Path]:
    """The seeded deployments moved below a real folder that is the only allowed path."""
    return await relocate_deployments(db, tmp_path / "host")


async def header_for(db: AsyncSession, seed_data: SeedData, environment_id: int) -> EditorHeader:
    """The environment as the superadmin sees it."""
    scope = await load_user_scope(db, seed_data.users.superadmin)
    header = await load_editor_header(db, scope, environment_id)
    assert header is not None
    return header


async def texts(db: AsyncSession, crypto: CryptoService, environment_id: int) -> dict[int, str]:
    """Decrypted content of every version of an environment by number."""
    rows = (
        await db.execute(
            select(EnvironmentVersion).where(EnvironmentVersion.environment_id == environment_id)
        )
    ).scalars()
    return {row.number: crypto.decrypt_text(row.content_encrypted) for row in rows}


async def test_missing_keys_under_the_default_policy_block(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\nDB_NAME=\nDB_USER=\n")
    check = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert check.outcome is DistOutcome.BLOCKED
    assert check.policy is DistMissingPolicy.BLOCK
    assert check.report is not None
    assert check.report.missing == ("DB_NAME", "DB_USER")
    assert check.report.missing_lines == (3, 4)
    assert check.version == 2 and check.problem is None
    assert (check.subject.name, check.subject.service_name) == (".env.db", "mysql")
    assert check.checked_by == "root"


async def test_the_deployment_policy_warn_turns_blocked_into_a_warning(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\nDB_NAME=\n")
    await set_deployment_policy(db, seed_data, "prod", DistMissingPolicy.WARN)
    check = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert check.outcome is DistOutcome.WARNING
    assert check.policy is DistMissingPolicy.WARN


async def test_the_environment_policy_overrides_the_deployment_policy(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    await set_deployment_policy(db, seed_data, "prod", DistMissingPolicy.WARN)
    await link_dist(db, seed_data.envs.db, DB_DIST, policy=DistMissingPolicy.BLOCK)
    blocked = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert blocked.outcome is DistOutcome.BLOCKED and blocked.policy is DistMissingPolicy.BLOCK
    await set_deployment_policy(db, seed_data, "prod", DistMissingPolicy.BLOCK)
    await link_dist(db, seed_data.envs.db, DB_DIST, policy=DistMissingPolicy.WARN)
    warned = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert warned.outcome is DistOutcome.WARNING and warned.policy is DistMissingPolicy.WARN


async def test_extra_and_empty_keys_do_not_hold_a_deploy_back(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\n")
    check = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert check.outcome is DistOutcome.PASSED
    assert check.report is not None
    assert check.report.extra == ("DB_PORT",) and check.report.missing == ()


async def test_the_latest_version_is_the_one_compared(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\n")
    first = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert first.outcome is DistOutcome.PASSED and first.version == 2
    header = await header_for(db, seed_data, seed_data.envs.db.id)
    await save_version(
        db,
        crypto,
        header,
        actor=seed_data.users.superadmin,
        ip_address=None,
        base_number=2,
        comment="",
        raw_content="DB_HOST=db\n",
    )
    second = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert second.version == 3 and second.outcome is DistOutcome.BLOCKED
    assert second.report is not None and second.report.missing == ("DB_PORT",)


async def test_a_file_that_is_not_env_format_is_skipped_without_reading_the_host(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    await set_file_type(db, seed_data.figures.ca.id, FileType.ENV)
    await link_dist(db, seed_data.figures.ca, "environments/web-ca.pem-dist")
    await db.execute(delete(AllowedPath))
    await db.commit()
    check = await evaluate_dist(db, crypto, seed_data.figures.ca.id, checked_by="root")
    assert check.outcome is DistOutcome.SKIPPED and check.problem is None
    assert check.report is not None
    assert check.report.skipped is DistSkipReason.ENVIRONMENT_NOT_ENV_FORMAT
    assert check.skip_message is not None and "certificate" in check.skip_message


async def test_a_certificate_is_not_applicable_whatever_dist_path_it_holds(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    """Nothing is read from the host and the stored path is ignored, even one outside the roots."""
    ca = seed_data.figures.ca
    await link_dist(db, ca, "environments/web-ca.pem-dist")
    await db.execute(delete(AllowedPath))
    await db.commit()
    check = await evaluate_dist(db, crypto, ca.id, checked_by="root")
    assert check.outcome is DistOutcome.SKIPPED and check.problem is None
    assert check.report is not None and check.report.skipped is DistSkipReason.NOT_APPLICABLE
    assert check.skip_message is not None and "no dist file" in check.skip_message
    assert check.subject.file_type is FileType.CERT
    await run_dist_check(db, crypto, ca.id, actor=seed_data.users.superadmin, ip_address=None)
    entry = (await audit_entries(db, "environment.dist_check"))[-1]
    assert entry.details["outcome"] == "skipped" and entry.details["skipped"] == "not_applicable"


async def test_the_deployment_overview_lists_a_certificate_as_skipped(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    scope = await load_user_scope(db, seed_data.users.superadmin)
    checks = await run_deployment_dist_checks(
        db,
        crypto,
        scope,
        seed_data.deployments.fancy_staging.id,
        actor=seed_data.users.superadmin,
        ip_address=None,
    )
    by_name = {check.subject.name: check for check in checks}
    ca = by_name["web-ca.pem"]
    assert ca.outcome is DistOutcome.SKIPPED
    assert ca.report is not None and ca.report.skipped is DistSkipReason.NOT_APPLICABLE


async def test_a_file_without_a_dist_link_is_skipped(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    check = await evaluate_dist(db, crypto, seed_data.envs.db_secrets.id, checked_by="root")
    assert check.outcome is DistOutcome.SKIPPED
    assert check.report is not None and check.report.skipped is DistSkipReason.NO_DIST_LINKED
    assert check.subject.dist_path is None


async def test_a_dist_file_that_is_not_env_format_is_skipped(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    certificate = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    write_dist(roots["prod"], DB_DIST, certificate)
    check = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert check.outcome is DistOutcome.SKIPPED
    assert check.report is not None and check.report.skipped is DistSkipReason.DIST_NOT_ENV_FORMAT


async def test_a_missing_dist_file_fails_the_check_without_naming_a_path(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    check = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert check.outcome is DistOutcome.FAILED and check.report is None
    assert check.problem is DistProblem.NOT_A_FILE
    assert check.problem_message == "The dist file does not exist or is not a regular file."
    assert str(roots["prod"]) not in repr(check)


async def test_a_symlinked_dist_file_leaving_the_deployment_fails_the_check(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["staging"], "outside-dist", "DB_HOST=\n")
    (roots["prod"] / DB_DIST).symlink_to(roots["staging"] / "outside-dist")
    check = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert check.outcome is DistOutcome.FAILED
    assert check.problem is DistProblem.LEAVES_ROOT
    assert str(roots["staging"]) not in repr(check)


async def test_allowed_paths_are_loaded_fresh_for_every_check(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_PORT=\n")
    before = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert before.outcome is DistOutcome.PASSED
    await db.execute(delete(AllowedPath))
    await db.commit()
    after = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="root")
    assert after.outcome is DistOutcome.FAILED and after.problem is DistProblem.ROOT_OUTSIDE
    assert after.problem_message is not None
    assert str(roots["prod"]) not in after.problem_message


async def test_an_unknown_environment_is_refused(
    db: AsyncSession, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    with pytest.raises(DistError) as raised:
        await evaluate_dist(db, crypto, 999_999, checked_by="root")
    assert raised.value.problem is DistProblem.ENVIRONMENT_MISSING


@pytest.mark.parametrize(
    ("dist_text", "outcome", "result"),
    [
        (f"DB_HOST=\nDB_PORT=\nDB_NAME={DIST_DEFAULT_VALUE}\n", "blocked", AuditResult.BLOCKED),
        (f"DB_HOST={DIST_DEFAULT_VALUE}\nDB_PORT=\n", "passed", AuditResult.SUCCESS),
    ],
)
async def test_a_check_is_audited_with_counts_and_never_a_value(
    db: AsyncSession,
    seed_data: SeedData,
    crypto: CryptoService,
    roots: dict[str, Path],
    dist_text: str,
    outcome: str,
    result: AuditResult,
) -> None:
    write_dist(roots["prod"], DB_DIST, dist_text)
    await run_dist_check(
        db,
        crypto,
        seed_data.envs.db.id,
        actor=seed_data.users.admin,
        ip_address="10.0.0.9",
    )
    (entry,) = await audit_entries(db, "environment.dist_check")
    assert entry.result is result
    assert entry.actor_name == "admin" and entry.ip_address == "10.0.0.9"
    assert (entry.project_id, entry.deployment_id, entry.service_id, entry.environment_id) == (
        seed_data.projects.fancy.id,
        seed_data.deployments.fancy_prod.id,
        seed_data.services.mysql.id,
        seed_data.envs.db.id,
    )
    assert entry.target_name == ".env.db"
    assert entry.details["outcome"] == outcome
    assert entry.details["policy"] == "block" and entry.details["version"] == 2
    assert {"missing", "extra", "empty"} <= entry.details.keys()
    assert len(entry.version_ids or []) == 1
    assert DIST_DEFAULT_VALUE not in json.dumps(entry.details)


async def test_skipped_and_failed_checks_are_audited_with_their_reason(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    actor = seed_data.users.admin
    await run_dist_check(db, crypto, seed_data.envs.db_secrets.id, actor=actor, ip_address=None)
    await run_dist_check(db, crypto, seed_data.envs.db.id, actor=actor, ip_address=None)
    skipped, failed = await audit_entries(db, "environment.dist_check")
    assert skipped.result is AuditResult.SUCCESS
    assert skipped.details["outcome"] == "skipped"
    assert skipped.details["skipped"] == "no_dist_linked"
    assert "missing" not in skipped.details
    assert failed.result is AuditResult.FAILURE
    assert failed.details["outcome"] == "failed" and failed.details["problem"] == "not_a_file"


async def test_adding_the_missing_keys_saves_an_audited_version_with_empty_values(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(
        roots["prod"],
        DB_DIST,
        f"# template\nDB_HOST=\nDB_PORT=\nDB_NAME={DIST_DEFAULT_VALUE}\nDB_USER=\n",
    )
    header = await header_for(db, seed_data, seed_data.envs.db.id)
    added = await add_missing_keys(
        db,
        crypto,
        header,
        actor=seed_data.users.admin,
        ip_address="10.0.0.9",
        base_number=2,
    )
    assert added.keys == ("DB_NAME", "DB_USER") and added.version.number == 3
    assert added.version.comment == ADDED_COMMENT
    saved = (await texts(db, crypto, seed_data.envs.db.id))[3]
    assert saved == "DB_HOST=db\nDB_PORT=3306\nDB_NAME=\nDB_USER=\n"
    parsed = parse_env(saved)
    assert parsed.empty_keys == ("DB_NAME", "DB_USER")
    assert DIST_DEFAULT_VALUE not in saved
    (entry,) = await audit_entries(db, "environment.save")
    assert entry.details == {"number": 3, "mode": "key_value", "keys": ["DB_NAME", "DB_USER"]}
    assert entry.version_ids == [added.version.id] and entry.actor_name == "admin"
    after = await evaluate_dist(db, crypto, seed_data.envs.db.id, checked_by="admin")
    assert after.outcome is DistOutcome.PASSED
    assert after.report is not None and after.report.empty == ("DB_NAME", "DB_USER")


async def test_adding_the_missing_keys_keeps_comments_and_order(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], "environments/.env.db.app-dist", "MYSQL_HOST=\nNEW_FLAG=\n")
    await link_dist(db, seed_data.editor, "environments/.env.db.app-dist")
    header = await header_for(db, seed_data, seed_data.editor.id)
    before = (await texts(db, crypto, seed_data.editor.id))[3]
    await add_missing_keys(
        db,
        crypto,
        header,
        actor=seed_data.users.superadmin,
        ip_address=None,
        base_number=3,
    )
    after = (await texts(db, crypto, seed_data.editor.id))[4]
    assert after.startswith(before) and after.endswith("NEW_FLAG=\n")


@pytest.mark.parametrize("base_number", [1, 3])
async def test_adding_refuses_a_report_built_from_another_version(
    db: AsyncSession,
    seed_data: SeedData,
    crypto: CryptoService,
    roots: dict[str, Path],
    base_number: int,
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    header = await header_for(db, seed_data, seed_data.envs.db.id)
    with pytest.raises(EditorError, match=STALE_MESSAGE):
        await add_missing_keys(
            db,
            crypto,
            header,
            actor=seed_data.users.admin,
            ip_address=None,
            base_number=base_number,
        )
    assert set(await texts(db, crypto, seed_data.envs.db.id)) == {1, 2}
    assert await audit_entries(db, "environment.save") == []


async def test_adding_refuses_when_nothing_is_missing_skipped_or_unreadable(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    actor = seed_data.users.admin
    header = await header_for(db, seed_data, seed_data.envs.db.id)
    with pytest.raises(EditorError, match="does not exist"):
        await add_missing_keys(db, crypto, header, actor=actor, ip_address=None, base_number=2)
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\n")
    with pytest.raises(EditorError, match=NOTHING_TO_ADD_MESSAGE):
        await add_missing_keys(db, crypto, header, actor=actor, ip_address=None, base_number=2)
    write_dist(roots["prod"], DB_DIST, "just words\n")
    with pytest.raises(EditorError, match="not env format"):
        await add_missing_keys(db, crypto, header, actor=actor, ip_address=None, base_number=2)
    assert set(await texts(db, crypto, seed_data.envs.db.id)) == {1, 2}


async def test_deployment_checks_cover_only_the_files_in_scope_and_audit_each(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService, roots: dict[str, Path]
) -> None:
    write_dist(roots["prod"], DB_DIST, "DB_HOST=\nDB_NAME=\n")
    write_dist(roots["prod"], "environments/.env.web-dist", "APP_ENV=\n")
    await link_dist(db, seed_data.envs.web, "environments/.env.web-dist")
    users = seed_data.users
    deployment = seed_data.deployments.fancy_prod.id
    scope = await load_user_scope(db, users.deployment_admin)
    checks = await run_deployment_dist_checks(
        db, crypto, scope, deployment, actor=users.deployment_admin, ip_address=None
    )
    assert [(check.subject.service_name, check.subject.name) for check in checks] == [
        ("mysql", ".env.db"),
        ("mysql", ".env.db.app"),
        ("mysql", ".env.db.secrets"),
        ("web", ".env.web"),
    ]
    outcomes = {check.subject.name: check.outcome for check in checks}
    assert outcomes[".env.db"] is DistOutcome.BLOCKED
    assert outcomes[".env.web"] is DistOutcome.PASSED
    assert outcomes[".env.db.secrets"] is DistOutcome.SKIPPED
    assert len(await audit_entries(db, "environment.dist_check")) == 4

    scoped = await load_user_scope(db, users.env_scoped_admin)
    only = await run_deployment_dist_checks(
        db, crypto, scoped, deployment, actor=users.env_scoped_admin, ip_address=None
    )
    assert [check.subject.name for check in only] == [".env.db"]
    viewer = await load_user_scope(db, users.viewer)
    assert (
        await run_deployment_dist_checks(
            db, crypto, viewer, deployment, actor=users.viewer, ip_address=None
        )
        == ()
    )
