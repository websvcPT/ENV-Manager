"""Typed application settings kept in the ``app_settings`` table.

Every setting has a name of the form ``section.field`` and a default in code, so a database with
no rows behaves like a new installation. A value is stored as JSON text; a stored value of the
wrong type reads as the default instead of failing a request. Writers validate a whole section
before they call ``save_section``, which stores only the fields that changed and writes one audit
entry naming each old and new value. A secret (an SMTP password, a git token) never goes through
the typed sections: ``write_secret`` encrypts it with the crypto service, bound to its own name,
and the audit entry records only that it changed.

Settings never replace startup configuration that stays in environment variables: the master
key, base URL, CSRF secret, trusted proxies and allowed sources have no setting here.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.log import get_logger
from app.models import AppSetting, AuditResult
from app.models.app_setting import KEY_CHECK_SETTING
from app.services.audit import AuditActor, record_actor_audit

log = get_logger("app_settings")

ACTION_UPDATE = "settings.update"
TARGET_TYPE = "settings"

# Names of settings whose value is a secret. Each feature that stores one adds its name here.
SECRET_KEYS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SecuritySettings:
    """Session timeouts, lockout limits and the password rule."""

    idle_timeout_minutes: int = 30
    absolute_timeout_hours: int = 12
    user_max_failures: int = 5
    ip_max_failures: int = 20
    api_ip_max_failures: int = 10
    lockout_minutes: int = 15
    min_password_length: int = 12


@dataclass(frozen=True)
class PostDeploySettings:
    """The switch for post-deploy commands. Docker must also be usable for them to run."""

    enabled: bool = True


@dataclass(frozen=True)
class GeneralSettings:
    """Time zone and date format for audit times, and the defaults of a new deployment."""

    time_zone: str = "UTC"
    date_format: str = "iso"
    dist_missing_policy: str = "block"
    drift_policy: str = "halt"
    missing_target_policy: str = "create"
    file_mode: str = "0600"
    file_owner: str = ""
    file_group: str = ""


@dataclass(frozen=True)
class StoredSettings:
    """Every typed section, as stored now."""

    security: SecuritySettings = field(default_factory=SecuritySettings)
    post_deploy: PostDeploySettings = field(default_factory=PostDeploySettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)


@dataclass(frozen=True)
class SettingChange:
    """One setting that changed: its name and, except for a secret, the old and new value."""

    setting: str
    old: object = None
    new: object = None
    secret: bool = False

    def audit_detail(self) -> dict[str, object]:
        """The change as an audit detail: values for plain settings, only the name for secrets."""
        if self.secret:
            return {"setting": self.setting, "kind": "secret"}
        return {"setting": self.setting, "old": self.old, "new": self.new}


def setting_key(section: str, name: str) -> str:
    """The stored name of a setting."""
    return f"{section}.{name}"


def _decode(raw: str, default: object) -> object | None:
    """The stored JSON text as a value of the default's type, None when it is not one."""
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    expected = type(default)
    if expected is bool:
        return value if isinstance(value, bool) else None
    if expected is int:
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    return value if isinstance(value, expected) else None


def read_section[SectionT](
    section: str, kind: type[SectionT], stored: Mapping[str, str]
) -> SectionT:
    """One section from raw stored values; a missing or malformed value reads as the default."""
    values: dict[str, Any] = {}
    for item in fields(kind):  # type: ignore[arg-type]
        raw = stored.get(setting_key(section, item.name))
        if raw is None:
            continue
        decoded = _decode(raw, item.default)
        if decoded is None:
            log.warning("setting %s holds a value of the wrong type and is ignored", item.name)
            continue
        values[item.name] = decoded
    return kind(**values)


async def _stored_values(db: AsyncSession) -> dict[str, str]:
    """Every stored setting as raw text, without the key check record."""
    rows = (await db.execute(select(AppSetting.key, AppSetting.value))).all()
    return {key: value for key, value in rows if key != KEY_CHECK_SETTING}


async def load_stored_settings(db: AsyncSession) -> StoredSettings:
    """Read every typed section in one query. Call it again where a fresh value matters."""
    stored = await _stored_values(db)
    return StoredSettings(
        security=read_section("security", SecuritySettings, stored),
        post_deploy=read_section("post_deploy", PostDeploySettings, stored),
        general=read_section("general", GeneralSettings, stored),
    )


async def _store(db: AsyncSession, key: str, text: str) -> None:
    """Insert or replace one row."""
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=text))
    else:
        row.value = text


async def save_section[SectionT](
    db: AsyncSession, section: str, new: SectionT, *, actor: AuditActor
) -> list[SettingChange]:
    """Store the fields of ``new`` that differ from what is stored and audit them together.

    The section is read again inside this call, so two saves never compare against a stale copy.
    Nothing is written when nothing changed. The caller commits, so the change and its audit
    entry are stored together with whatever else the caller does.
    """
    kind = type(new)
    current = read_section(section, kind, await _stored_values(db))
    changes = [
        SettingChange(
            setting_key(section, item.name), getattr(current, item.name), getattr(new, item.name)
        )
        for item in fields(kind)  # type: ignore[arg-type]
        if getattr(current, item.name) != getattr(new, item.name)
    ]
    if not changes:
        return []
    for change in changes:
        await _store(db, change.setting, json.dumps(change.new))
    await record_actor_audit(
        db,
        actor,
        action=ACTION_UPDATE,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_name=section,
        details={"section": section, "changes": [change.audit_detail() for change in changes]},
    )
    await db.flush()
    return changes


def _secret_context(key: str) -> bytes:
    """The authenticated context that binds a secret to its name."""
    return f"setting:{key}".encode()


def _require_secret_key(key: str) -> None:
    """Refuse a name that is not declared as secret."""
    if key not in SECRET_KEYS:
        raise ValueError("not a secret setting")


async def write_secret(
    db: AsyncSession, crypto: CryptoService, key: str, value: str, *, actor: AuditActor
) -> None:
    """Encrypt and store a secret and audit that it changed, never its value.

    The ciphertext is bound to the setting name, so it cannot be copied to another name. The
    caller commits.
    """
    _require_secret_key(key)
    await _store(db, key, crypto.encrypt(value, context=_secret_context(key)))
    await record_actor_audit(
        db,
        actor,
        action=ACTION_UPDATE,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_name=key.partition(".")[0],
        details={
            "section": key.partition(".")[0],
            "changes": [SettingChange(key, secret=True).audit_detail()],
        },
    )
    await db.flush()


async def read_secret(db: AsyncSession, crypto: CryptoService, key: str) -> str | None:
    """The decrypted secret, None when it was never set. It lives in memory only."""
    _require_secret_key(key)
    row = await db.get(AppSetting, key)
    if row is None:
        return None
    return crypto.decrypt_text(row.value, context=_secret_context(key))
