"""Audit details rendering and the CSV export: redaction, bounds, scope and formula safety."""

import csv
import io
import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import create_session_factory
from app.models import AuditActorType, AuditEntry, AuditResult, User
from app.seeds import BULK_ACTION, BULK_COUNT, SeedData, ensure_audit_figures
from app.services import audit_export
from app.services.audit import record_audit
from app.services.audit_details import (
    HIDDEN_TEXT,
    MAX_TEXT_LENGTH,
    detail_lines,
    details_json,
    redact,
)
from app.services.audit_export import CSV_COLUMNS, csv_cell, export_chunks, record_export
from app.services.audit_filters import AuditFilters
from app.services.audit_log import list_page
from app.services.permissions import Visibility, compute_visibility, load_user_scope

SECRETS = ("hunter2", "s3cr3t-value", "BEGIN PRIVATE KEY")


@pytest.fixture
async def session_factory(seeded_database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory on the seeded database with the audit figures added."""
    factory = create_session_factory(seeded_database)
    async with factory() as session:
        await ensure_audit_figures(session)
        await session.commit()
    return factory


@pytest.fixture
async def db(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session on the seeded database with the audit figures added."""
    async with session_factory() as session:
        yield session


async def visibility_of(db: AsyncSession, user: User) -> Visibility:
    """What a user sees, as the audit screen computes it."""
    return await compute_visibility(db, await load_user_scope(db, user))


async def export_rows(
    session_factory: async_sessionmaker[AsyncSession],
    visibility: Visibility,
    filters: AuditFilters | None = None,
) -> list[list[str]]:
    """The exported CSV parsed back into rows, header included."""
    chunks = export_chunks(session_factory, visibility, filters or AuditFilters())
    text = "".join([chunk async for chunk in chunks])
    return list(csv.reader(io.StringIO(text)))


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        ("=SUM(1,1)", "'=SUM(1,1)"),
        ("+1+1", "'+1+1"),
        ("-2+3", "'-2+3"),
        ("@cmd", "'@cmd"),
        ("\tcmd", "'\tcmd"),
        ("\rcmd", "'\rcmd"),
        ("  =1+1", "'  =1+1"),
        ("plain", "plain"),
        ("a=b", "a=b"),
        ("", ""),
        (None, ""),
        (42, "42"),
    ],
)
def test_cells_that_a_spreadsheet_would_run_get_a_text_marker(value: object, cell: str) -> None:
    """A cell starting with = + - @ tab or CR (also after spaces) is neutralized."""
    assert csv_cell(value) == cell


def test_values_under_secret_looking_keys_are_replaced_at_every_depth() -> None:
    """Redaction is by key name and reaches into lists and nested mappings."""
    details = {
        "version": 3,
        "value": SECRETS[0],
        "Password": SECRETS[1],
        "nested": {"api-key": SECRETS[2], "keys": ["MYSQL_PASSWORD"], "deeper": [{"secret": "x"}]},
    }
    safe = redact(details)
    assert safe == {
        "version": 3,
        "value": HIDDEN_TEXT,
        "Password": HIDDEN_TEXT,
        "nested": {
            "api-key": HIDDEN_TEXT,
            "keys": ["MYSQL_PASSWORD"],
            "deeper": [{"secret": HIDDEN_TEXT}],
        },
    }
    shown = repr(detail_lines(details)) + details_json(details)
    assert not any(secret in shown for secret in SECRETS)


def test_details_become_one_line_per_key_without_empty_values() -> None:
    """Scalars, lists and lists of mappings read as plain text; empty values are left out."""
    lines = detail_lines(
        {
            "dry_run": False,
            "force": True,
            "exit": None,
            "note": "",
            "changed_keys": ["A", "B"],
            "environments": [
                {"environment": ".env.db", "version": 2, "status": "written", "drift": None},
                {"environment": ".env.web", "version": 1, "status": "unchanged"},
            ],
        }
    )
    assert [(line.key, line.text, line.code) for line in lines] == [
        ("dry run", "no", False),
        ("force", "yes", False),
        ("changed keys", "A, B", False),
        (
            "environments",
            "environment .env.db · version 2 · status written\n"
            "environment .env.web · version 1 · status unchanged",
            False,
        ),
    ]


def test_output_is_code_and_long_text_is_bounded() -> None:
    """Multi-line or long strings are marked as code, and nothing exceeds the display limit."""
    lines = detail_lines(
        {"post_deploy_output": "line one\nline two", "reason": "x" * (MAX_TEXT_LENGTH + 500)}
    )
    assert lines[0].code is True
    assert lines[1].code is True and len(lines[1].text) == MAX_TEXT_LENGTH + 3
    assert lines[1].text.endswith("...")


def test_details_that_are_not_a_mapping_still_render_and_empty_ones_do_not() -> None:
    """The column holds JSON, so a list or a string must not break the screen."""
    assert [(line.key, line.text) for line in detail_lines(["a", "b"])] == [("details", "a, b")]
    assert [(line.key, line.text) for line in detail_lines("odd")] == [("details", "odd")]
    assert detail_lines({}) == () and detail_lines(None) == ()
    assert details_json({}) == "" and details_json(None) == ""
    assert json.loads(details_json({"a": [1, {"b": "é"}]})) == {"a": [1, {"b": "é"}]}


async def test_the_export_holds_the_entries_of_the_scope_with_a_header(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession], seed_data: SeedData
) -> None:
    """The superadmin gets every entry, an admin only its own, both newest first."""
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    everything = await export_rows(session_factory, superadmin)
    assert tuple(everything[0]) == CSV_COLUMNS
    assert len(everything) - 1 == 4 + 9 + BULK_COUNT
    admin = await export_rows(session_factory, await visibility_of(db, seed_data.users.admin))
    assert len(admin) - 1 == 10
    assert BULK_ACTION not in {row[5] for row in admin[1:]}
    assert "new.hire" not in {row[7] for row in admin[1:]}
    scoped = await export_rows(
        session_factory, await visibility_of(db, seed_data.users.env_scoped_admin)
    )
    assert [(row[5], row[7]) for row in scoped[1:]] == [
        ("environment.save", ".env.db"),
        ("file_save", ".env.db"),
    ]
    nobody = await export_rows(
        session_factory, await visibility_of(db, seed_data.users.ungranted_admin)
    )
    assert len(nobody) == 1


async def test_the_export_follows_the_filters_and_reads_in_batches(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: SeedData,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter narrows the file, and many batches still give each entry once."""
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    halted = await export_rows(session_factory, superadmin, AuditFilters(result=AuditResult.HALTED))
    assert [(row[2], row[3], row[4], row[5], row[8]) for row in halted[1:]] == [
        ("billing-ci", "token", "203.0.113.9", "service.deploy", "halted")
    ]
    assert halted[1][9].isdigit() and halted[1][1].endswith("Z")
    monkeypatch.setattr(audit_export, "EXPORT_BATCH", 7)
    bulk = await export_rows(session_factory, superadmin, AuditFilters(action=BULK_ACTION))
    ids = [row[0] for row in bulk[1:]]
    assert len(ids) == BULK_COUNT == len(set(ids))


async def test_cells_that_would_be_formulas_are_neutralized_in_the_file(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession], seed_data: SeedData
) -> None:
    """Names, objects and details that start with a formula character are stored text."""
    entry = await record_audit(
        db,
        action='=HYPERLINK("http://evil")',
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_name="+cmd|' /C calc'!A0",
        ip_address="@10.0.0.1",
        target_type="-thing",
        target_name="=1+1",
        details={"note": "=1+1", "text": '"quoted, with comma"\nand a line'},
    )
    await db.commit()
    rows = await export_rows(session_factory, await visibility_of(db, seed_data.users.superadmin))
    mine = next(row for row in rows[1:] if row[0] == str(entry.id))
    assert mine[2] == "'+cmd|' /C calc'!A0"
    assert mine[4] == "'@10.0.0.1"
    assert mine[5] == '\'=HYPERLINK("http://evil")'
    assert mine[6] == "'-thing" and mine[7] == "'=1+1"
    assert json.loads(mine[10]) == {"note": "=1+1", "text": '"quoted, with comma"\nand a line'}
    for row in rows[1:]:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@", "\t", "\r"))


async def test_secrets_in_details_never_reach_the_file(
    db: AsyncSession, session_factory: async_sessionmaker[AsyncSession], seed_data: SeedData
) -> None:
    """Even a stored value under a secret-looking key is replaced in the export."""
    await record_audit(
        db,
        action="secret.test",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.SYSTEM,
        details={"value": SECRETS[0], "inner": {"password": SECRETS[1]}},
    )
    await db.commit()
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    rows = await export_rows(session_factory, superadmin, AuditFilters(action="secret.test"))
    flat = "\n".join(",".join(row) for row in rows)
    assert not any(secret in flat for secret in SECRETS)
    assert HIDDEN_TEXT in flat


async def test_an_export_is_audited_with_filters_and_count_and_seen_by_the_superadmin_only(
    db: AsyncSession, seed_data: SeedData
) -> None:
    """The entry has no item scope: the superadmin reads it, an admin does not."""
    admin = seed_data.users.admin
    visibility = await visibility_of(db, admin)
    filters = AuditFilters(result=AuditResult.SUCCESS)
    plan = await record_export(db, visibility, admin, filters, "10.9.9.9")
    await db.commit()
    assert plan.rows == 7
    entry = (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "audit.export"))
    ).scalar_one()
    assert entry.actor_name == "admin" and entry.ip_address == "10.9.9.9"
    assert entry.result is AuditResult.SUCCESS
    assert entry.details == {"filters": {"result": "success"}, "rows": 7}
    assert (entry.project_id, entry.deployment_id, entry.service_id, entry.environment_id) == (
        None,
        None,
        None,
        None,
    )
    assert not any(
        row.action == "audit.export"
        for row in (await list_page(db, visibility, AuditFilters(), limit=500)).rows
    )
    superadmin = await visibility_of(db, seed_data.users.superadmin)
    shown = await list_page(db, superadmin, AuditFilters(action="audit.export"))
    assert [row.object_name for row in shown.rows] == ["audit log"]
