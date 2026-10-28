"""The single sign-on settings: checking the form and saving it.

The client secret is a secret. It is never shown back: the form carries it only when someone
types a new one, an empty field keeps the stored secret, and a separate choice removes it.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.services.app_settings import (
    SSO_CLIENT_SECRET_KEY,
    SSO_GROUPS,
    SettingChange,
    SsoSettings,
    clear_secret,
    save_section,
    write_secret,
)
from app.services.audit import AuditActor

MAX_ADDRESS_LENGTH = 2048
MAX_TEXT_LENGTH = 255
MAX_SECRET_LENGTH = 1024
MAX_MAPPINGS = 50
_CLAIM_PATH = re.compile(r"^[^\s.]+(?:\.[^\s.]+)*$")


@dataclass(frozen=True)
class SsoForm:
    """The single sign-on settings as typed. ``client_secret`` is empty to keep the stored one
    and ``remove_secret`` asks to delete it. ``mappings`` are rows of (role, group) as typed."""

    enabled: bool
    issuer: str
    client_id: str
    roles_claim: str
    account_url: str
    create_users: bool
    link_by_email: bool
    mappings: tuple[tuple[str, str], ...]
    client_secret: str = ""
    remove_secret: bool = False

    @classmethod
    def from_settings(cls, settings: SsoSettings) -> SsoForm:
        """The form showing the stored settings, with the secret field empty."""
        return cls(
            settings.enabled,
            settings.issuer,
            settings.client_id,
            settings.roles_claim,
            settings.account_url,
            settings.create_users,
            settings.link_by_email,
            settings.mappings,
        )


@dataclass(frozen=True)
class CleanSso:
    """A checked form: the settings to store and what to do with the secret."""

    settings: SsoSettings
    client_secret: str
    remove_secret: bool


class SsoFormError(Exception):
    """The form has refused values; ``errors`` holds one message per field name."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the messages by field name."""
        super().__init__("the single sign-on settings are invalid")
        self.errors = errors


def address_problem(text: str, *, plain: bool) -> str | None:
    """Why the text is not an absolute http or https address with a host, or None when it is.

    ``plain`` also refuses a query and a fragment, which an issuer never has.
    """
    if len(text) > MAX_ADDRESS_LENGTH:
        return f"Use at most {MAX_ADDRESS_LENGTH} characters."
    if not text.isascii() or not text.isprintable() or any(char.isspace() for char in text):
        return "Use a plain address with no spaces or special characters."
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError:
        return "Enter a valid address such as https://sso.example.com/realms/main."
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "Enter an http or https address that names a host."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "Do not put a user name or password in the address."
    if plain and (parts.query or parts.fragment or "?" in text or "#" in text):
        return "Leave out the query and the fragment."
    return None


def _mapping_rows(
    rows: tuple[tuple[str, str], ...], errors: dict[str, str]
) -> tuple[tuple[str, str], ...]:
    """The rows trimmed, or no rows with a message in ``errors`` naming the first bad one."""
    if len(rows) > MAX_MAPPINGS:
        errors["mappings"] = f"Use at most {MAX_MAPPINGS} rows."
        return ()
    clean: list[tuple[str, str]] = []
    for number, (role, group) in enumerate(rows, start=1):
        role = role.strip()
        if not role or len(role) > MAX_TEXT_LENGTH or not role.isprintable():
            errors["mappings"] = f"Row {number}: enter the role, up to {MAX_TEXT_LENGTH} characters"
            return ()
        if group not in SSO_GROUPS:
            errors["mappings"] = f"Row {number}: choose admin or viewer."
            return ()
        clean.append((role, group))
    return tuple(clean)


def validate_sso(form: SsoForm) -> CleanSso:
    """The settings the form stands for, or ``SsoFormError`` with a message per field.

    The issuer is an absolute http or https address with no query or fragment, and with sign in
    switched on it and the client id are required. The roles claim is a dotted path. The mapping
    rows each have a role value and the group admin or viewer; the superadmin group is not a
    choice.
    """
    errors: dict[str, str] = {}
    issuer = form.issuer.strip()
    if issuer:
        if problem := address_problem(issuer, plain=True):
            errors["issuer"] = problem
    elif form.enabled:
        errors["issuer"] = "Enter the issuer address of the provider."
    client_id = form.client_id.strip()
    if len(client_id) > MAX_TEXT_LENGTH or not client_id.isprintable():
        errors["client_id"] = f"Use printable characters, at most {MAX_TEXT_LENGTH}."
    elif form.enabled and not client_id:
        errors["client_id"] = "Enter the client id."
    roles_claim = form.roles_claim.strip()
    if roles_claim and (
        len(roles_claim) > MAX_TEXT_LENGTH or not _CLAIM_PATH.fullmatch(roles_claim)
    ):
        errors["roles_claim"] = "Enter a dotted path such as resource_access.env-manager.roles."
    account_url = form.account_url.strip()
    if account_url and (problem := address_problem(account_url, plain=False)):
        errors["account_url"] = problem
    mappings = _mapping_rows(form.mappings, errors)
    secret = form.client_secret
    if len(secret) > MAX_SECRET_LENGTH or not secret.isprintable():
        errors["client_secret"] = f"Use printable characters, at most {MAX_SECRET_LENGTH}."
    elif secret and form.remove_secret:
        errors["client_secret"] = "Type a new secret or remove the stored one, not both."  # noqa: S105
    if errors:
        raise SsoFormError(errors)
    return CleanSso(
        SsoSettings(
            form.enabled,
            issuer,
            client_id,
            roles_claim,
            account_url,
            form.create_users,
            form.link_by_email,
            mappings,
        ),
        secret,
        form.remove_secret,
    )


async def save_sso(
    db: AsyncSession, crypto: CryptoService, clean: CleanSso, *, actor: AuditActor
) -> bool:
    """Store the settings and the secret change, audit them and commit.

    Returns whether anything changed.
    """
    changes: list[SettingChange] = await save_section(db, "sso", clean.settings, actor=actor)
    secret_changed = False
    if clean.client_secret:
        await write_secret(db, crypto, SSO_CLIENT_SECRET_KEY, clean.client_secret, actor=actor)
        secret_changed = True
    elif clean.remove_secret:
        secret_changed = await clear_secret(db, SSO_CLIENT_SECRET_KEY, actor=actor)
    await db.commit()
    return bool(changes) or secret_changed
