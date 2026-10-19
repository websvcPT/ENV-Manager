"""The typed settings store: defaults, changes, audit entries and secrets."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, DecryptionError
from app.core.db import create_session_factory
from app.models import AppSetting, AuditEntry, AuditResult
from app.models.app_setting import KEY_CHECK_SETTING
from app.services import app_settings
from app.services.app_settings import (
    ACTION_UPDATE,
    GeneralSettings,
    PostDeploySettings,
    SecuritySettings,
    load_stored_settings,
    read_secret,
    save_section,
    write_secret,
)
from app.services.audit import AuditActor
from app.services.audit_details import detail_lines

SECRET_NAME = "test.token"


async def audit_updates(engine: AsyncEngine) -> list[AuditEntry]:
    """Every settings audit entry, oldest first."""
    async with create_session_factory(engine)() as db:
        return list(
            (
                await db.execute(
                    select(AuditEntry)
                    .where(AuditEntry.action == ACTION_UPDATE)
                    .order_by(AuditEntry.id)
                )
            ).scalars()
        )


async def test_an_empty_database_reads_as_the_defaults(engine: AsyncEngine) -> None:
    """A new installation has no rows and behaves like the defaults in code."""
    async with create_session_factory(engine)() as db:
        stored = await load_stored_settings(db)
    assert stored.security == SecuritySettings()
    assert stored.post_deploy == PostDeploySettings()
    assert stored.general == GeneralSettings()
    assert stored.security.idle_timeout_minutes == 30
    assert stored.post_deploy.enabled is True
    assert stored.general.file_mode == "0600"


async def test_a_save_stores_only_the_changed_fields_and_audits_old_and_new(
    engine: AsyncEngine,
) -> None:
    """Two changed fields make two rows and one entry holding both old and new values."""
    factory = create_session_factory(engine)
    async with factory() as db:
        new = replace(SecuritySettings(), idle_timeout_minutes=45, min_password_length=14)
        changes = await save_section(db, "security", new, actor=AuditActor.cli())
        await db.commit()
    assert [change.setting for change in changes] == [
        "security.idle_timeout_minutes",
        "security.min_password_length",
    ]
    async with factory() as db:
        rows = {row.key: row.value for row in (await db.execute(select(AppSetting))).scalars()}
        stored = await load_stored_settings(db)
    assert rows == {"security.idle_timeout_minutes": "45", "security.min_password_length": "14"}
    assert stored.security.idle_timeout_minutes == 45
    assert stored.security.min_password_length == 14
    assert stored.security.user_max_failures == 5
    (entry,) = await audit_updates(engine)
    assert entry.result is AuditResult.SUCCESS
    assert entry.actor_name == "cli"
    assert entry.details == {
        "section": "security",
        "changes": [
            {"setting": "security.idle_timeout_minutes", "old": 30, "new": 45},
            {"setting": "security.min_password_length", "old": 12, "new": 14},
        ],
    }


async def test_a_second_save_is_compared_with_what_is_stored_now(engine: AsyncEngine) -> None:
    """Unchanged values write nothing and no audit entry; a later change shows the stored old."""
    factory = create_session_factory(engine)
    async with factory() as db:
        first = replace(GeneralSettings(), file_owner="www-data")
        await save_section(db, "general", first, actor=AuditActor.cli())
        await db.commit()
    async with factory() as db:
        assert await save_section(db, "general", first, actor=AuditActor.cli()) == []
        await db.commit()
        second = replace(first, file_owner="deploy")
        changes = await save_section(db, "general", second, actor=AuditActor.cli())
        await db.commit()
    assert [(change.old, change.new) for change in changes] == [("www-data", "deploy")]
    assert len(await audit_updates(engine)) == 2


async def test_the_audit_details_survive_the_redaction_of_the_audit_screens(
    engine: AsyncEngine,
) -> None:
    """The password length is a number, not a secret: its old and new value stay readable."""
    async with create_session_factory(engine)() as db:
        new = replace(SecuritySettings(), min_password_length=16)
        await save_section(db, "security", new, actor=AuditActor.cli())
        await db.commit()
    (entry,) = await audit_updates(engine)
    text = " ".join(line.text for line in detail_lines(entry.details))
    assert "min_password_length" in text and "old 12" in text and "new 16" in text
    assert "hidden" not in text


async def test_a_stored_value_of_the_wrong_type_reads_as_the_default(
    engine: AsyncEngine,
) -> None:
    """A damaged row never fails a request: text, a bool for a number and bad JSON are ignored."""
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all(
            [
                AppSetting(key="security.idle_timeout_minutes", value='"soon"'),
                AppSetting(key="security.absolute_timeout_hours", value="true"),
                AppSetting(key="security.user_max_failures", value="not json"),
                AppSetting(key="post_deploy.enabled", value="1"),
                AppSetting(key="general.time_zone", value="12"),
                AppSetting(key="security.lockout_minutes", value="20"),
            ]
        )
        await db.commit()
        stored = await load_stored_settings(db)
    assert stored.security.idle_timeout_minutes == 30
    assert stored.security.absolute_timeout_hours == 12
    assert stored.security.user_max_failures == 5
    assert stored.post_deploy.enabled is True
    assert stored.general.time_zone == "UTC"
    assert stored.security.lockout_minutes == 20


async def test_the_key_check_record_is_not_a_setting(engine: AsyncEngine) -> None:
    """The key check row shares the table and is never read as a setting or a secret."""
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add(AppSetting(key=KEY_CHECK_SETTING, value="ciphertext"))
        await db.commit()
        stored = await load_stored_settings(db)
    assert stored.security == SecuritySettings()
    async with factory() as db:
        with pytest.raises(ValueError, match="not a secret setting"):
            await read_secret(db, CryptoService(b"k" * 32), KEY_CHECK_SETTING)


@pytest.fixture
def declared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare a test secret the way a feature that stores one would."""
    monkeypatch.setattr(app_settings, "SECRET_KEYS", frozenset({SECRET_NAME, "test.other"}))


async def test_a_secret_is_encrypted_and_never_audited(
    engine: AsyncEngine, crypto: CryptoService, declared_secret: None
) -> None:
    """The stored value is ciphertext, the audit entry names the setting and nothing else."""
    factory = create_session_factory(engine)
    async with factory() as db:
        await write_secret(db, crypto, SECRET_NAME, "hunter2-plain", actor=AuditActor.cli())
        await db.commit()
    async with factory() as db:
        row = await db.get(AppSetting, SECRET_NAME)
        assert row is not None
        raw = row.value
        assert "hunter2-plain" not in raw
        assert await read_secret(db, crypto, SECRET_NAME) == "hunter2-plain"
        assert await read_secret(db, crypto, "test.other") is None
    (entry,) = await audit_updates(engine)
    assert entry.details == {
        "section": "test",
        "changes": [{"setting": SECRET_NAME, "kind": "secret"}],
    }
    assert "hunter2-plain" not in str(entry.details) and raw not in str(entry.details)


async def test_a_secret_cannot_be_moved_to_another_setting_name(
    engine: AsyncEngine, crypto: CryptoService, declared_secret: None
) -> None:
    """The ciphertext is bound to its name, so copying the row to another name fails to decrypt."""
    factory = create_session_factory(engine)
    async with factory() as db:
        await write_secret(db, crypto, SECRET_NAME, "bound", actor=AuditActor.cli())
        row = await db.get(AppSetting, SECRET_NAME)
        assert row is not None
        db.add(AppSetting(key="test.other", value=row.value))
        await db.commit()
    async with factory() as db:
        assert await read_secret(db, crypto, SECRET_NAME) == "bound"
        with pytest.raises(DecryptionError):
            await read_secret(db, crypto, "test.other")


async def test_only_declared_secret_names_are_accepted(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """A name that no feature declared as secret can neither be written nor read as one."""
    async with create_session_factory(engine)() as db:
        with pytest.raises(ValueError, match="not a secret setting"):
            await write_secret(db, crypto, "test.undeclared", "x", actor=AuditActor.cli())
        with pytest.raises(ValueError, match="not a secret setting"):
            await read_secret(db, crypto, "test.undeclared")
        assert (await db.execute(select(AppSetting))).first() is None


async def test_a_secret_row_is_not_read_into_a_typed_section(
    engine: AsyncEngine, crypto: CryptoService, declared_secret: None
) -> None:
    """Typed sections only read their own fields, so a secret row never leaks into them."""
    async with create_session_factory(engine)() as db:
        await write_secret(db, crypto, SECRET_NAME, "s", actor=AuditActor.cli())
        await db.commit()
        stored = await load_stored_settings(db)
    assert stored == type(stored)()
