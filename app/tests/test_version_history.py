"""The version history service on the seed: the table, key level diffs, reveals and restore."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditEntry, EnvironmentVersion, FileType, User
from app.seeds import EDITOR_SECRET_VALUES, SeedData
from app.services.editor import STALE_MESSAGE, EditorHeader, load_editor_header
from app.services.permissions import load_user_scope
from app.services.version_history import (
    WHOLE_FILE_KEY,
    DiffChange,
    RestoreError,
    VersionNotFoundError,
    default_pair,
    load_diff,
    load_versions,
    pick_pair,
    restore_version,
    reveal_diff,
)
from app.services.versions import create_version
from app.tests.file_type_helpers import set_file_type

CERTIFICATE_BODY = "MIIDdzCCAl7sentinelCertBody"


async def header_for(db: AsyncSession, user: User, environment_id: int) -> EditorHeader:
    """The environment as a user sees it."""
    header = await load_editor_header(db, await load_user_scope(db, user), environment_id)
    assert header is not None
    return header


async def add_version(
    db: AsyncSession, crypto: CryptoService, environment_id: int, text: str, author: User
) -> EnvironmentVersion:
    """Save a text as the next version of an environment, as the seed does."""
    version = await create_version(db, crypto, environment_id, text, author=author)
    await db.commit()
    return version


async def reveal_entries(db: AsyncSession) -> list[AuditEntry]:
    """The reveal audit entries written so far, oldest first."""
    return list(
        (
            await db.execute(
                select(AuditEntry)
                .where(AuditEntry.action == "environment.reveal")
                .order_by(AuditEntry.id)
            )
        ).scalars()
    )


def test_default_pair_is_the_two_newest_versions_older_first() -> None:
    assert default_pair([3, 1, 2]) == (2, 3)
    assert default_pair([1]) is None
    assert default_pair([]) is None


def test_pick_pair_needs_exactly_two_existing_distinct_versions() -> None:
    assert pick_pair([3, 1], [1, 2, 3]) == (1, 3)
    assert pick_pair([1, 1], [1, 2, 3]) is None
    assert pick_pair([1], [1, 2, 3]) is None
    assert pick_pair([1, 2, 3], [1, 2, 3]) is None
    assert pick_pair([1, 9], [1, 2, 3]) is None
    assert pick_pair([], [1, 2, 3]) is None


async def test_versions_are_listed_newest_first_without_content(
    db: AsyncSession, seed_data: SeedData
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.editor.id)
    versions = await load_versions(db, header)
    assert [version.number for version in versions] == [3, 2, 1]
    assert all(version.author == "root" for version in versions)
    assert [version.comment for version in versions] == [
        "seed version 3",
        "seed version 2",
        "seed version 1",
    ]
    assert all(len(version.short_hash) == 7 for version in versions)
    assert len({version.short_hash for version in versions}) == 3
    assert not any(version.deployed for version in versions)
    assert not any(secret in repr(versions) for secret in EDITOR_SECRET_VALUES)


async def test_the_deployed_marker_sits_on_the_version_that_was_written(
    db: AsyncSession, seed_data: SeedData
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.figures.tuning.id)
    versions = await load_versions(db, header)
    assert [(version.number, version.deployed) for version in versions] == [
        (2, False),
        (1, True),
    ]


async def test_a_deleted_author_is_listed_without_a_name(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    version = await create_version(
        db, crypto, seed_data.figures.replica.id, "REPLICA=off\n", author=None
    )
    await db.commit()
    header = await header_for(db, seed_data.users.admin, seed_data.figures.replica.id)
    versions = await load_versions(db, header)
    assert (versions[0].number, versions[0].author) == (version.number, None)


@pytest.mark.parametrize(
    ("pair", "changed", "added", "unchanged"),
    [
        ((1, 2), ["MYSQL_PASSWORD"], ["MAX_CONNECTIONS"], 2),
        ((2, 3), ["MAX_CONNECTIONS"], ["TLS_MIN_VERSION", "TLS_CA_CERT"], 3),
        ((1, 3), ["MYSQL_PASSWORD"], ["MAX_CONNECTIONS", "TLS_MIN_VERSION", "TLS_CA_CERT"], 2),
    ],
)
async def test_diff_lists_changed_then_added_keys_and_counts_the_rest(
    db: AsyncSession,
    seed_data: SeedData,
    crypto: CryptoService,
    pair: tuple[int, int],
    changed: list[str],
    added: list[str],
    unchanged: int,
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.editor.id)
    diff = await load_diff(db, crypto, header, *pair)
    assert (diff.left, diff.right) == pair
    assert [(row.key, row.change) for row in diff.rows] == [
        *((key, DiffChange.CHANGED) for key in changed),
        *((key, DiffChange.ADDED) for key in added),
    ]
    assert [row.index for row in diff.rows] == list(range(len(diff.rows)))
    assert diff.unchanged == unchanged
    assert not diff.whole_file and not diff.identical
    shown = repr(diff)
    assert not any(secret in shown for secret in (*EDITOR_SECRET_VALUES, "rotated by hand"))
    assert "db.internal" not in shown


async def test_diff_orders_the_pair_older_first(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.editor.id)
    assert await load_diff(db, crypto, header, 3, 1) == await load_diff(db, crypto, header, 1, 3)


async def test_diff_marks_a_key_that_is_gone_as_removed_after_the_others(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    editor = seed_data.editor.id
    header = await header_for(db, seed_data.users.admin, editor)
    await add_version(
        db,
        crypto,
        editor,
        "MYSQL_HOST=db.internal.example.com\nMYSQL_PASSWORD=Changed7\nNEW_KEY=1\n",
        seed_data.users.admin,
    )
    diff = await load_diff(db, crypto, header, 3, 4)
    assert [(row.key, row.change) for row in diff.rows] == [
        ("MYSQL_PASSWORD", DiffChange.CHANGED),
        ("NEW_KEY", DiffChange.ADDED),
        ("MYSQL_PORT", DiffChange.REMOVED),
        ("MAX_CONNECTIONS", DiffChange.REMOVED),
        ("TLS_MIN_VERSION", DiffChange.REMOVED),
        ("TLS_CA_CERT", DiffChange.REMOVED),
    ]
    assert diff.unchanged == 1


async def test_versions_that_differ_only_in_comments_have_no_key_rows(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    editor = seed_data.editor.id
    header = await header_for(db, seed_data.users.admin, editor)
    await add_version(
        db,
        crypto,
        editor,
        "# another comment\nMYSQL_HOST=db.internal.example.com\n",
        seed_data.users.admin,
    )
    await add_version(
        db,
        crypto,
        editor,
        "# a third comment\nMYSQL_HOST=db.internal.example.com\n",
        seed_data.users.admin,
    )
    diff = await load_diff(db, crypto, header, 4, 5)
    assert diff.rows == () and diff.unchanged == 1
    assert not diff.identical and not diff.whole_file


async def test_identical_versions_are_flagged(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    replica = seed_data.figures.replica
    header = await header_for(db, seed_data.users.admin, replica.id)
    await add_version(db, crypto, replica.id, "REPLICA=on\n", seed_data.users.admin)
    diff = await load_diff(db, crypto, header, 1, 2)
    assert diff.rows == () and diff.identical


async def test_a_file_that_is_not_env_format_is_compared_as_a_whole(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    ca = seed_data.figures.ca
    await set_file_type(db, ca.id, FileType.ENV)
    header = await header_for(db, seed_data.users.admin, ca.id)
    await add_version(
        db,
        crypto,
        ca.id,
        "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\n-----END CERTIFICATE-----\n",
        seed_data.users.admin,
    )
    diff = await load_diff(db, crypto, header, 1, 2)
    assert diff.whole_file and diff.unchanged == 0
    assert [(row.key, row.change) for row in diff.rows] == [(WHOLE_FILE_KEY, DiffChange.CHANGED)]
    assert "bmV3Ym9keQ" not in repr(diff) and "c2VlZA" not in repr(diff)


async def test_an_unknown_version_cannot_be_compared(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.editor.id)
    with pytest.raises(VersionNotFoundError):
        await load_diff(db, crypto, header, 1, 9)
    with pytest.raises(VersionNotFoundError):
        await load_diff(db, crypto, header, 2, 2)


async def test_reveal_returns_only_the_values_of_the_listed_keys(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    header = await header_for(db, admin, seed_data.editor.id)
    revealed = await reveal_diff(db, crypto, header, 2, 3, actor=admin, ip_address="10.0.0.9")
    assert revealed.values[0] == ("150", "200")
    assert revealed.values[1] == (None, "TLSv1.2")
    left, right = revealed.values[2]
    assert left is None and right is not None and CERTIFICATE_BODY in right
    everything = repr(revealed.values)
    assert "Ke3rH6vNa5" not in everything and "db.internal.example.com" not in everything
    assert "rotated by hand" not in everything


async def test_reveal_audits_each_side_before_returning_without_a_value(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    header = await header_for(db, admin, seed_data.editor.id)
    await reveal_diff(db, crypto, header, 3, 2, actor=admin, ip_address="10.0.0.9")
    entries = await reveal_entries(db)
    assert [entry.details for entry in entries] == [
        {"version": 2, "scope": "diff", "compared_with": 3, "keys": ["MAX_CONNECTIONS"]},
        {
            "version": 3,
            "scope": "diff",
            "compared_with": 2,
            "keys": ["MAX_CONNECTIONS", "TLS_MIN_VERSION", "TLS_CA_CERT"],
        },
    ]
    assert all(entry.actor_name == "admin" and entry.ip_address == "10.0.0.9" for entry in entries)
    assert all(entry.environment_id == seed_data.editor.id for entry in entries)
    assert all(len(entry.version_ids) == 1 for entry in entries)
    stored = repr([(entry.details, entry.target_name, entry.version_ids) for entry in entries])
    assert not any(secret in stored for secret in (*EDITOR_SECRET_VALUES, "150", "200", "TLSv1.2"))


async def test_a_side_without_values_writes_no_entry(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    editor = seed_data.editor.id
    header = await header_for(db, admin, editor)
    await add_version(db, crypto, editor, "MYSQL_HOST=db.internal.example.com\n", admin)
    await reveal_diff(db, crypto, header, 3, 4, actor=admin, ip_address=None)
    entries = await reveal_entries(db)
    assert [entry.details["version"] for entry in entries] == [3]
    await add_version(db, crypto, editor, "MYSQL_HOST=db.internal.example.com\nPLUS=1\n", admin)
    await reveal_diff(db, crypto, header, 4, 5, actor=admin, ip_address=None)
    entries = await reveal_entries(db)
    assert [entry.details["version"] for entry in entries[1:]] == [5]


async def test_reveal_of_a_whole_file_audits_a_file_scope(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    ca = seed_data.figures.ca
    await set_file_type(db, ca.id, FileType.ENV)
    header = await header_for(db, admin, ca.id)
    await add_version(
        db, crypto, ca.id, "-----BEGIN CERTIFICATE-----\nbmV3\n-----END CERTIFICATE-----\n", admin
    )
    revealed = await reveal_diff(db, crypto, header, 1, 2, actor=admin, ip_address=None)
    left, right = revealed.values[0]
    assert left is not None and "c2VlZA==" in left
    assert right is not None and "bmV3" in right
    entries = await reveal_entries(db)
    assert [entry.details for entry in entries] == [
        {"version": 1, "scope": "file", "compared_with": 2},
        {"version": 2, "scope": "file", "compared_with": 1},
    ]


async def test_reveal_of_an_unknown_version_is_not_audited(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    header = await header_for(db, admin, seed_data.editor.id)
    with pytest.raises(VersionNotFoundError):
        await reveal_diff(db, crypto, header, 1, 7, actor=admin, ip_address=None)
    assert await reveal_entries(db) == []


async def test_restore_saves_the_chosen_content_as_the_next_version(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    editor = seed_data.editor.id
    header = await header_for(db, admin, editor)
    version = await restore_version(
        db, crypto, header, 1, base_number=3, actor=admin, ip_address="10.0.0.9"
    )
    rows = {
        row.number: row
        for row in (
            await db.execute(
                select(EnvironmentVersion).where(EnvironmentVersion.environment_id == editor)
            )
        ).scalars()
    }
    assert sorted(rows) == [1, 2, 3, 4] and version.number == 4
    assert crypto.decrypt_text(rows[4].content_encrypted) == crypto.decrypt_text(
        rows[1].content_encrypted
    )
    assert rows[4].content_hash == rows[1].content_hash
    assert rows[4].content_encrypted != rows[1].content_encrypted
    assert rows[4].comment == "Restored from version 1" and rows[4].author_id == admin.id
    entry = (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.restore"))
    ).scalar_one()
    assert entry.details == {"number": 4, "restored": 1}
    assert entry.version_ids == [rows[1].id, rows[4].id]
    assert entry.actor_name == "admin" and entry.ip_address == "10.0.0.9"
    assert entry.environment_id == editor
    assert not any(secret in repr(entry.details) for secret in EDITOR_SECRET_VALUES)


async def test_restore_keeps_the_stored_bytes_exactly(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    replica = seed_data.figures.replica
    header = await header_for(db, admin, replica.id)
    crlf = "# note\r\nA=1\r\nB='two words'\r\n"
    await add_version(db, crypto, replica.id, crlf, admin)
    await add_version(db, crypto, replica.id, "A=2\n", admin)
    version = await restore_version(
        db, crypto, header, 2, base_number=3, actor=admin, ip_address=None
    )
    assert crypto.decrypt_text(version.content_encrypted) == crlf


async def test_restore_is_refused_for_a_stale_page(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    header = await header_for(db, admin, seed_data.editor.id)
    with pytest.raises(RestoreError, match=STALE_MESSAGE):
        await restore_version(db, crypto, header, 1, base_number=2, actor=admin, ip_address=None)
    assert await _version_count(db, seed_data.editor.id) == 3


async def test_restore_is_refused_when_the_content_is_the_current_one(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    header = await header_for(db, admin, seed_data.editor.id)
    await restore_version(db, crypto, header, 1, base_number=3, actor=admin, ip_address=None)
    with pytest.raises(RestoreError, match="same content as the current version"):
        await restore_version(db, crypto, header, 1, base_number=4, actor=admin, ip_address=None)
    with pytest.raises(RestoreError, match="same content as the current version"):
        await restore_version(db, crypto, header, 4, base_number=4, actor=admin, ip_address=None)
    assert await _version_count(db, seed_data.editor.id) == 4


async def test_restore_of_an_unknown_version_changes_nothing(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    header = await header_for(db, admin, seed_data.editor.id)
    with pytest.raises(VersionNotFoundError):
        await restore_version(db, crypto, header, 9, base_number=3, actor=admin, ip_address=None)
    assert await _version_count(db, seed_data.editor.id) == 3
    assert (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.restore"))
    ).first() is None


async def _version_count(db: AsyncSession, environment_id: int) -> int:
    """How many versions an environment has."""
    return len(
        (
            await db.execute(
                select(EnvironmentVersion.id).where(
                    EnvironmentVersion.environment_id == environment_id
                )
            )
        ).all()
    )


CERT_V1 = "-----BEGIN CERTIFICATE-----\nc2VlZA==\n-----END CERTIFICATE-----\n"
CERT_V2 = "-----BEGIN CERTIFICATE-----\nbmV3Ym9keQ==\nZXh0cmE=\n-----END CERTIFICATE-----\n"


async def test_a_certificate_is_compared_line_by_line_and_never_by_key(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    ca = seed_data.figures.ca
    header = await header_for(db, seed_data.users.admin, ca.id)
    assert header.file_type is FileType.CERT
    await add_version(db, crypto, ca.id, CERT_V2, seed_data.users.admin)
    diff = await load_diff(db, crypto, header, 1, 2)
    assert diff.by_line and not diff.whole_file and not diff.identical
    assert [(row.key, row.change) for row in diff.rows] == [
        ("Line 2", DiffChange.CHANGED),
        ("Line 3", DiffChange.CHANGED),
        ("Line 4", DiffChange.ADDED),
    ]
    assert diff.unchanged == 1
    assert "bmV3Ym9keQ" not in repr(diff) and "c2VlZA" not in repr(diff)


async def test_a_certificate_diff_reveals_the_differing_lines_and_audits_line_names(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    ca = seed_data.figures.ca
    header = await header_for(db, admin, ca.id)
    await add_version(db, crypto, ca.id, CERT_V2, admin)
    revealed = await reveal_diff(db, crypto, header, 1, 2, actor=admin, ip_address=None)
    assert revealed.values[0] == ("c2VlZA==", "bmV3Ym9keQ==")
    assert revealed.values[1] == ("-----END CERTIFICATE-----", "ZXh0cmE=")
    assert revealed.values[2] == (None, "-----END CERTIFICATE-----")
    entries = await reveal_entries(db)
    assert [entry.details for entry in entries] == [
        {"version": 1, "scope": "diff", "compared_with": 2, "keys": ["Line 2", "Line 3"]},
        {
            "version": 2,
            "scope": "diff",
            "compared_with": 1,
            "keys": ["Line 2", "Line 3", "Line 4"],
        },
    ]
    stored = repr([entry.details for entry in entries])
    assert "bmV3Ym9keQ" not in stored and "c2VlZA" not in stored


async def test_two_certificate_versions_that_differ_only_in_line_breaks_list_no_line(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    ca = seed_data.figures.ca
    header = await header_for(db, seed_data.users.admin, ca.id)
    await add_version(db, crypto, ca.id, CERT_V1.rstrip("\n"), seed_data.users.admin)
    diff = await load_diff(db, crypto, header, 1, 2)
    assert diff.by_line and diff.rows == () and not diff.identical and diff.unchanged == 3


async def test_a_certificate_version_without_a_pem_block_cannot_be_restored(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    admin = seed_data.users.admin
    ca = seed_data.figures.ca
    header = await header_for(db, admin, ca.id)
    await add_version(db, crypto, ca.id, "just some words\n", admin)
    await add_version(db, crypto, ca.id, CERT_V2, admin)
    with pytest.raises(RestoreError, match="PEM block"):
        await restore_version(db, crypto, header, 2, base_number=3, actor=admin, ip_address=None)
    restored = await restore_version(
        db, crypto, header, 1, base_number=3, actor=admin, ip_address=None
    )
    assert restored.number == 4
