"""The editor service: header by scope, audited reveals and saving versions on the seed."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditEntry, EnvironmentVersion, User
from app.seeds import EDITOR_SECRET_VALUES, SeedData
from app.services.editor import (
    NO_CHANGES_MESSAGE,
    STALE_MESSAGE,
    EditorError,
    EditorHeader,
    KeyValueSubmission,
    VariableNotFoundError,
    load_editor_content,
    load_editor_header,
    reveal_content,
    reveal_value,
    save_version,
)
from app.services.permissions import load_user_scope
from app.services.versions import create_version


async def header_for(db: AsyncSession, user: User, environment_id: int) -> EditorHeader | None:
    """The environment as a user sees it."""
    return await load_editor_header(db, await load_user_scope(db, user), environment_id)


async def stored_texts(
    db: AsyncSession, crypto: CryptoService, environment_id: int
) -> dict[int, str]:
    """Decrypted content of every version by number."""
    rows = (
        (
            await db.execute(
                select(EnvironmentVersion).where(
                    EnvironmentVersion.environment_id == environment_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.number: crypto.decrypt_text(row.content_encrypted) for row in rows}


async def test_header_follows_scope_and_group(db: AsyncSession, seed_data: SeedData) -> None:
    users = seed_data.users
    editor = seed_data.editor.id
    admin_header = await header_for(db, users.admin, editor)
    viewer_header = await header_for(db, users.viewer, editor)
    assert admin_header is not None and viewer_header is not None
    assert (admin_header.can_edit, admin_header.can_reveal) == (True, True)
    assert (viewer_header.can_edit, viewer_header.can_reveal) == (False, False)
    assert viewer_header.can_check_dist is False and admin_header.can_check_dist is True
    assert admin_header.current == 3 and admin_header.deployed is None
    assert admin_header.status == "never" and admin_header.current_author == "root"
    assert (admin_header.service_name, admin_header.deployment_name) == ("mysql", "prod")
    assert (admin_header.file_mode, admin_header.file_owner, admin_header.file_group) == (
        "0600",
        "www-data",
        "www-data",
    )
    assert await header_for(db, users.env_scoped_admin, editor) is None
    assert await header_for(db, users.ungranted_admin, editor) is None
    assert await header_for(db, users.admin, 999_999) is None
    assert await header_for(db, users.env_scoped_admin, seed_data.envs.db.id) is not None


async def test_masked_content_holds_names_and_sizes_only(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    content = await load_editor_content(db, crypto, seed_data.editor.id)
    assert content is not None and content.env_format and content.number == 3
    assert [row.key for row in content.rows] == [
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_PASSWORD",
        "MAX_CONNECTIONS",
        "TLS_MIN_VERSION",
        "TLS_CA_CERT",
    ]
    certificate = content.rows[-1]
    assert certificate.multiline and certificate.lines == 4
    assert not content.rows[0].multiline
    assert repr(content).count("db.internal") == 0
    certificate_content = await load_editor_content(db, crypto, seed_data.figures.ca.id)
    assert certificate_content is not None
    assert not certificate_content.env_format and certificate_content.rows == ()
    assert certificate_content.line_count == 3


async def test_reveal_returns_one_value_and_audits_without_it(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.editor.id)
    assert header is not None
    value = await reveal_value(
        db, crypto, header, "MYSQL_PASSWORD", actor=seed_data.users.admin, ip_address="10.0.0.9"
    )
    assert value == "Ke3rH6vNa5"
    old = await reveal_value(
        db,
        crypto,
        header,
        "MYSQL_PASSWORD",
        actor=seed_data.users.admin,
        ip_address=None,
        version=1,
    )
    assert old == "Zq7vK2mPw9"
    entries = (
        (await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.reveal")))
        .scalars()
        .all()
    )
    assert len(entries) == 2
    first = entries[0]
    assert first.details == {"version": 3, "key": "MYSQL_PASSWORD"}
    assert first.actor_name == "admin" and first.ip_address == "10.0.0.9"
    assert first.environment_id == seed_data.editor.id and len(first.version_ids) == 1
    stored = repr([(entry.details, entry.target_name, entry.version_ids) for entry in entries])
    assert not any(secret in stored for secret in EDITOR_SECRET_VALUES)


async def test_reveal_of_an_unknown_key_or_version_is_not_audited(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    header = await header_for(db, seed_data.users.admin, seed_data.editor.id)
    assert header is not None
    actor = seed_data.users.admin
    with pytest.raises(VariableNotFoundError):
        await reveal_value(db, crypto, header, "NOPE", actor=actor, ip_address=None)
    with pytest.raises(VariableNotFoundError):
        await reveal_value(
            db, crypto, header, "MYSQL_HOST", actor=actor, ip_address=None, version=9
        )
    with pytest.raises(VariableNotFoundError):
        await reveal_content(db, crypto, header, actor=actor, ip_address=None, version=9)
    assert (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.reveal"))
    ).first() is None


async def test_reveal_content_returns_the_whole_text_and_audits_it(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    header = await header_for(db, seed_data.users.superadmin, seed_data.figures.ca.id)
    assert header is not None
    text = await reveal_content(
        db, crypto, header, actor=seed_data.users.superadmin, ip_address=None
    )
    assert text.startswith("-----BEGIN CERTIFICATE-----")
    entry = (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.reveal"))
    ).scalar_one()
    assert entry.details == {"version": 1, "scope": "file"}
    assert "BEGIN" not in repr(entry.details)


async def test_key_value_and_raw_saves_store_the_same_content(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    actor = seed_data.users.admin
    editor = seed_data.editor.id
    header = await header_for(db, actor, editor)
    assert header is not None
    kv = await save_version(
        db,
        crypto,
        header,
        actor=actor,
        ip_address=None,
        base_number=3,
        comment="  via key/value  ",
        key_values=KeyValueSubmission(
            replaced=[("MYSQL_PORT", "3307")],
            removed=["TLS_MIN_VERSION"],
            added=[("EXTRA", "two words")],
        ),
    )
    texts = await stored_texts(db, crypto, editor)
    expected = (
        texts[3]
        .replace("MYSQL_PORT=3306", "MYSQL_PORT=3307")
        .replace("TLS_MIN_VERSION=TLSv1.2\n", "")
        + "EXTRA='two words'\n"
    )
    assert texts[4] == expected and kv.number == 4 and kv.comment == "via key/value"
    raw = await save_version(
        db,
        crypto,
        header,
        actor=actor,
        ip_address="10.1.1.1",
        base_number=4,
        comment="",
        raw_content=expected.replace("\n", "\r\n") + "EXTRA2=1",
    )
    again = await save_version(
        db,
        crypto,
        header,
        actor=actor,
        ip_address=None,
        base_number=5,
        comment="",
        key_values=KeyValueSubmission(removed=["EXTRA2"]),
    )
    texts = await stored_texts(db, crypto, editor)
    assert texts[5] == expected + "EXTRA2=1" and raw.comment is None
    assert texts[6] == expected
    assert again.content_hash == kv.content_hash == crypto.content_hash(expected)


async def test_save_audits_names_and_never_values(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    actor = seed_data.users.admin
    header = await header_for(db, actor, seed_data.editor.id)
    assert header is not None
    version = await save_version(
        db,
        crypto,
        header,
        actor=actor,
        ip_address="10.0.0.4",
        base_number=3,
        comment="rotate",
        key_values=KeyValueSubmission(replaced=[("MYSQL_PASSWORD", "BrandNewSecret77")]),
    )
    entry = (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.save"))
    ).scalar_one()
    assert entry.details == {"number": 4, "mode": "key_value", "keys": ["MYSQL_PASSWORD"]}
    assert entry.version_ids == [version.id] and entry.ip_address == "10.0.0.4"
    assert "BrandNewSecret77" not in repr(entry.details)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_number": 2, "raw_content": "A=1\n"}, STALE_MESSAGE),
        ({"base_number": 3, "key_values": KeyValueSubmission()}, NO_CHANGES_MESSAGE),
        (
            {"base_number": 3, "key_values": KeyValueSubmission(replaced=[("MYSQL_PORT", "")])},
            NO_CHANGES_MESSAGE,
        ),
        (
            {"base_number": 3, "key_values": KeyValueSubmission(replaced=[("MYSQL_PORT", "3306")])},
            NO_CHANGES_MESSAGE,
        ),
        ({"base_number": 3, "raw_content": ""}, None),
        ({"base_number": 3}, "Reveal the raw text to edit it."),
        ({"base_number": 3, "raw_content": "A=\x00\n"}, "Binary content is not supported."),
        (
            {"base_number": 3, "raw_content": "A=" + "x" * 262_144},
            "Content is larger than 256 KiB.",
        ),
        (
            {"base_number": 3, "key_values": KeyValueSubmission(added=[("bad name", "1")])},
            "Use letters, numbers, dots, dashes and underscores in a name.",
        ),
        (
            {"base_number": 3, "key_values": KeyValueSubmission(added=[("MYSQL_HOST", "1")])},
            "MYSQL_HOST already exists.",
        ),
        (
            {
                "base_number": 3,
                "key_values": KeyValueSubmission(added=[("TWICE", "1"), ("TWICE", "2")]),
            },
            "TWICE already exists.",
        ),
        (
            {"base_number": 3, "key_values": KeyValueSubmission(replaced=[("GONE", "1")])},
            "A variable you edited no longer exists. Reload the page.",
        ),
        (
            {"base_number": 3, "key_values": KeyValueSubmission(removed=["GONE"])},
            "A variable you removed no longer exists. Reload the page.",
        ),
        (
            {"base_number": 3, "raw_content": "A=1\n", "comment": "c" * 501},
            "The comment is longer than 500 characters.",
        ),
    ],
)
async def test_refused_saves_store_nothing(
    db: AsyncSession,
    seed_data: SeedData,
    crypto: CryptoService,
    kwargs: dict[str, object],
    message: str | None,
) -> None:
    actor = seed_data.users.admin
    header = await header_for(db, actor, seed_data.editor.id)
    assert header is not None
    arguments: dict[str, object] = {"comment": "", **kwargs}
    if message is None:
        version = await save_version(db, crypto, header, actor=actor, ip_address=None, **arguments)  # type: ignore[arg-type]
        assert version.number == 4
        return
    with pytest.raises(EditorError) as error:
        await save_version(db, crypto, header, actor=actor, ip_address=None, **arguments)  # type: ignore[arg-type]
    assert str(error.value) == message
    assert sorted(await stored_texts(db, crypto, seed_data.editor.id)) == [1, 2, 3]
    assert (
        await db.execute(select(AuditEntry).where(AuditEntry.action == "environment.save"))
    ).first() is None


async def test_key_value_save_is_refused_for_a_file_that_is_not_env_format(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    actor = seed_data.users.superadmin
    header = await header_for(db, actor, seed_data.figures.ca.id)
    assert header is not None
    with pytest.raises(EditorError) as error:
        await save_version(
            db,
            crypto,
            header,
            actor=actor,
            ip_address=None,
            base_number=1,
            comment="",
            key_values=KeyValueSubmission(added=[("A", "1")]),
        )
    assert "raw text" in str(error.value)
    saved = await save_version(
        db,
        crypto,
        header,
        actor=actor,
        ip_address=None,
        base_number=1,
        comment="",
        raw_content="-----BEGIN CERTIFICATE-----\nZm9v\n-----END CERTIFICATE-----\n",
    )
    assert saved.number == 2


async def test_stored_crlf_does_not_make_key_value_and_raw_saves_differ(
    db: AsyncSession, seed_data: SeedData, crypto: CryptoService
) -> None:
    """An imported file may hold CRLF; both editors still store the same LF text and hash."""
    actor = seed_data.users.admin
    crlf = "# note\r\nA=1\r\nB=2\r\n"
    for environment in (seed_data.envs.api, seed_data.envs.worker):
        await create_version(db, crypto, environment.id, crlf, author=actor)
    await db.commit()
    api = await header_for(db, actor, seed_data.envs.api.id)
    worker = await header_for(db, actor, seed_data.envs.worker.id)
    assert api is not None and worker is not None
    via_key_value = await save_version(
        db,
        crypto,
        api,
        actor=actor,
        ip_address=None,
        base_number=2,
        comment="",
        key_values=KeyValueSubmission(replaced=[("B", "3")]),
    )
    via_raw = await save_version(
        db,
        crypto,
        worker,
        actor=actor,
        ip_address=None,
        base_number=2,
        comment="",
        raw_content="# note\r\nA=1\r\nB=3\r\n",
    )
    texts = await stored_texts(db, crypto, seed_data.envs.api.id)
    assert texts[3] == "# note\nA=1\nB=3\n"
    assert via_key_value.content_hash == via_raw.content_hash == crypto.content_hash(texts[3])
    with pytest.raises(EditorError) as error:
        await save_version(
            db,
            crypto,
            worker,
            actor=actor,
            ip_address=None,
            base_number=3,
            comment="",
            key_values=KeyValueSubmission(replaced=[("B", "3")]),
        )
    assert str(error.value) == NO_CHANGES_MESSAGE
