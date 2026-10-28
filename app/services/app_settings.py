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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import urlsplit

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

SMTP_PASSWORD_KEY = "smtp.password"  # noqa: S105
WEBHOOK_SECRET_KEY = "notifications.webhook_secret"  # noqa: S105
WEBHOOK_URL_KEY = "notifications.webhook_url"
GIT_DEPLOY_KEY_KEY = "git.deploy_key"
GIT_TOKEN_KEY = "git.token"  # noqa: S105
SSO_CLIENT_SECRET_KEY = "sso.client_secret"  # noqa: S105

# Names of settings whose value is a secret. Each feature that stores one adds its name here.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        SMTP_PASSWORD_KEY,
        WEBHOOK_SECRET_KEY,
        WEBHOOK_URL_KEY,
        GIT_DEPLOY_KEY_KEY,
        GIT_TOKEN_KEY,
        SSO_CLIENT_SECRET_KEY,
    }
)


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
    """Time zone and date format for audit times, how long audit entries are kept in days (zero
    keeps every entry) and the defaults of a new deployment."""

    time_zone: str = "UTC"
    date_format: str = "iso"
    audit_retention_days: int = 90
    dist_missing_policy: str = "block"
    drift_policy: str = "halt"
    missing_target_policy: str = "create"
    file_mode: str = "0600"
    file_owner: str = ""
    file_group: str = ""


@dataclass(frozen=True)
class BackupSettings:
    """When backups run, how many are kept and the optional extra folder they are copied to.

    ``schedule`` is ``off``, ``daily``, ``six_hours`` or ``weekly``. A retention of zero for
    ``retention_count`` or ``retention_days`` means no limit of that kind.
    """

    schedule: str = "daily"
    retention_count: int = 14
    retention_days: int = 0
    extra_destination: str = ""


@dataclass(frozen=True)
class SmtpSettings:
    """The mail server notifications go through. The password is a secret kept apart.

    ``tls_mode`` is ``none``, ``starttls`` or ``tls`` (implicit TLS). An empty username means
    the server takes mail without signing in. Mail is not sent until a host and a from address
    are set.
    """

    host: str = ""
    port: int = 587
    tls_mode: str = "starttls"
    username: str = ""
    from_address: str = ""

    @property
    def configured(self) -> bool:
        """Whether there is enough to send mail: a host and a from address."""
        return bool(self.host and self.from_address)


@dataclass(frozen=True)
class NotificationSettings:
    """Which events send mail and, for events about a project, who receives them.

    Each event has a switch named after it. An event about a project also has a recipients
    choice (``superadmins``, ``project_admins`` or ``both``) in the field of the same name with
    ``_to`` added; events about the whole system always go to the superadmins. Successful
    deploys and file changes are off by default, everything that needs a person's attention is
    on.

    The outbound webhook reports every real deploy while ``webhook_enabled`` is on. Its address
    and its shared secret are kept apart as secrets (``WEBHOOK_URL_KEY``, ``WEBHOOK_SECRET_KEY``),
    because chat tools put the credential in the address.
    """

    webhook_enabled: bool = False
    login_new_address: bool = True
    lockout: bool = True
    deploy_succeeded: bool = False
    deploy_succeeded_to: str = "project_admins"
    deploy_problem: bool = True
    deploy_problem_to: str = "project_admins"
    file_changed: bool = False
    file_changed_to: str = "project_admins"
    user_changed: bool = True
    backup_failed: bool = True
    git_push_failed: bool = True
    key_rotated: bool = True


@dataclass(frozen=True)
class GitSettings:
    """The private git repository the encrypted export is pushed to.

    ``url`` is an ssh or https address; an ssh address uses the stored deploy key and an https
    address the stored access token (both are secrets kept apart, ``GIT_DEPLOY_KEY_KEY`` and
    ``GIT_TOKEN_KEY``). ``export_format`` is ``per_version`` (one encrypted file for each
    version of each file) or ``snapshot`` (the encrypted database). While ``push_enabled`` is on
    and an address is set, a push happens at most once an hour and only when something changed.
    """

    url: str = ""
    branch: str = "main"
    author_name: str = "ENV Manager"
    export_format: str = "per_version"
    push_enabled: bool = True

    @property
    def configured(self) -> bool:
        """Whether there is a repository to push to."""
        return bool(self.url)


SSO_GROUPS = ("admin", "viewer")


@dataclass(frozen=True)
class SsoSettings:
    """Sign in through an OpenID Connect provider. The client secret is a secret kept apart.

    ``mappings`` are rows of (role value, group): the first row whose role the person has at the
    provider decides the group, ``admin`` or ``viewer``. The superadmin group is never a choice.
    An empty ``roles_claim`` reads the client roles of this client (``resource_access`` then the
    client id then ``roles``) and an empty ``account_url`` is the ``/account`` page of the issuer;
    both are dotted paths and addresses that follow the other settings, so they are derived here
    and never stored.
    """

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    roles_claim: str = ""
    account_url: str = ""
    create_users: bool = True
    link_by_email: bool = True
    mappings: tuple[tuple[str, str], ...] = ()

    @property
    def configured(self) -> bool:
        """Whether sign in can start: switched on, with an issuer and a client id."""
        return bool(self.enabled and self.issuer and self.client_id)

    @property
    def effective_roles_claim(self) -> str:
        """The dotted path the roles are read from."""
        return self.roles_claim or f"resource_access.{self.client_id}.roles"

    @property
    def effective_account_url(self) -> str:
        """Where a person manages their account at the provider."""
        return self.account_url or f"{self.issuer.rstrip('/')}/account"


@dataclass(frozen=True)
class StoredSettings:
    """Every typed section, as stored now, and the names of the secrets that are stored.

    ``secrets`` holds names only (never a value, not even sealed), so a screen can say whether a
    secret is set without reading its row again; see ``has_secret_in``.
    """

    security: SecuritySettings = field(default_factory=SecuritySettings)
    post_deploy: PostDeploySettings = field(default_factory=PostDeploySettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    git: GitSettings = field(default_factory=GitSettings)
    sso: SsoSettings = field(default_factory=SsoSettings)
    secrets: frozenset[str] = frozenset()

    @property
    def sso_on(self) -> bool:
        """Whether sign in through the provider is on and can work: switched on, with an issuer,
        a client id and a stored client secret. The sign-in card, the password restriction and
        the account screens all follow this one answer."""
        return self.sso.configured and SSO_CLIENT_SECRET_KEY in self.secrets


# Settings whose value is an address that can carry a token in its path or query.
URL_SETTINGS: frozenset[str] = frozenset({"git.url"})
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):(?!//)")


def url_origin(value: object) -> str:
    """The scheme, host and port of an address, without the path, query or anything after them.

    An scp-like git address (``git@host:path``) reads as an ssh address of that host.
    """
    if not isinstance(value, str) or not value:
        return ""
    if "://" not in value:
        scp = _SCP_LIKE.match(value)
        return f"ssh://{scp.group('host')}" if scp else ""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}{port}" if host else ""


@dataclass(frozen=True)
class SettingChange:
    """One setting that changed: its name and, except for a secret, the old and new value."""

    setting: str
    old: object = None
    new: object = None
    secret: bool = False

    def audit_detail(self) -> dict[str, object]:
        """The change as an audit detail: values for plain settings, only the name for secrets.

        An address that may carry a token in its path (a webhook URL) is recorded as its scheme,
        host and port only.
        """
        if self.secret:
            return {"setting": self.setting, "kind": "secret"}
        if self.setting in URL_SETTINGS:
            return {
                "setting": self.setting,
                "kind": "url",
                "old": url_origin(self.old),
                "new": url_origin(self.new),
            }
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
    if expected is tuple:
        return _decode_rows(value)
    if expected is bool:
        return value if isinstance(value, bool) else None
    if expected is int:
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    return value if isinstance(value, expected) else None


def _decode_rows(value: object) -> tuple[tuple[str, str], ...] | None:
    """A stored list of two-text rows as a tuple of pairs, None when it is not one."""
    if not isinstance(value, list):
        return None
    rows: list[tuple[str, str]] = []
    for row in value:
        if not (isinstance(row, list) and len(row) == 2 and all(isinstance(t, str) for t in row)):
            return None
        rows.append((row[0], row[1]))
    return tuple(rows)


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
        backup=read_section("backup", BackupSettings, stored),
        smtp=read_section("smtp", SmtpSettings, stored),
        notifications=read_section("notifications", NotificationSettings, stored),
        git=read_section("git", GitSettings, stored),
        sso=read_section("sso", SsoSettings, stored),
        secrets=frozenset(key for key in stored if key in SECRET_KEYS),
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


def secret_context(key: str) -> bytes:
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
    await _store(db, key, crypto.encrypt(value, context=secret_context(key)))
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
    return crypto.decrypt_text(row.value, context=secret_context(key))


async def has_secret(db: AsyncSession, key: str) -> bool:
    """Whether a secret is stored, without decrypting it. Reads the row now."""
    _require_secret_key(key)
    return await db.get(AppSetting, key) is not None


def has_secret_in(stored: StoredSettings, key: str) -> bool:
    """Whether a secret was stored when ``stored`` was read, without another query.

    For a screen that changed nothing in its request; after a write use ``has_secret``.
    """
    _require_secret_key(key)
    return key in stored.secrets


async def clear_secret(db: AsyncSession, key: str, *, actor: AuditActor) -> bool:
    """Remove a stored secret and audit that it was removed. Returns whether one was stored.

    The caller commits.
    """
    _require_secret_key(key)
    row = await db.get(AppSetting, key)
    if row is None:
        return False
    await db.delete(row)
    section = key.partition(".")[0]
    await record_actor_audit(
        db,
        actor,
        action=ACTION_UPDATE,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_name=section,
        details={
            "section": section,
            "changes": [{"setting": key, "kind": "secret", "removed": True}],
        },
    )
    await db.flush()
    return True
