"""The Docker registry logins: one app-wide list, validated, sealed and audited.

A post-deploy command that pulls an image from a private registry needs credentials, and the
``docker`` command reads them from its config folder. The application keeps one list of logins
for every deployment (a registry host, a username and a password or token). Each token is sealed
with the master key under a context bound to the row id, so a ciphertext copied to another row
does not open, and it is never written anywhere in plain text: not in the audit log, not in an
error, not in a ``repr``. Only the superadmin manages the list (the web layer guards it with
``Capability.MANAGE_SETTINGS``); the functions here take the acting user for the audit.

The tokens are opened only to build the Docker config of a command run (``load_registry_logins``
and ``docker_config``). The runner writes that config into a private folder of its own for each
run and removes it afterwards, so nothing about a login lives on disk between runs. The config
holds every login, and Docker sends a registry only the entry that names it, so one run can pull
from several private registries.

A host is stored normalized: lowercase, no scheme, no trailing slash, ``host`` or ``host:port``.
There is one login per registry, as in Docker's own model: the Docker Hub names ``docker.io``,
``index.docker.io`` and ``registry-1.docker.io`` are one registry and share one entry.

Every change is audited (``registry.add``, ``registry.update``, ``registry.remove``) with the
host and username, and an update says which of host, username and token changed, never the
token. The functions commit.
"""

import base64
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.models import AuditResult, DockerRegistry, User
from app.services.audit import AuditActor, record_actor_audit

ACTION_ADD = "registry.add"
ACTION_UPDATE = "registry.update"
ACTION_REMOVE = "registry.remove"
TARGET_TYPE = "registry"

HOST_KEY = "host"
USERNAME_KEY = "username"
TOKEN_KEY = "token"  # noqa: S105
FORM_KEY = "form"

MAX_HOST_LENGTH = 253
MAX_USERNAME_LENGTH = 255
MAX_TOKEN_LENGTH = 4096
MAX_PORT = 65535

HOST_EMPTY_MESSAGE = "Enter the registry host."
HOST_PATH_MESSAGE = "Enter the host only, with no path after it."
HOST_INVALID_MESSAGE = (
    "Enter a host name such as registry.example.com, with an optional port such as :5000."
)
HOST_PORT_MESSAGE = f"The port must be a whole number from 1 to {MAX_PORT}."
USERNAME_EMPTY_MESSAGE = "Enter the username."
USERNAME_LENGTH_MESSAGE = f"The username can have at most {MAX_USERNAME_LENGTH} characters."
USERNAME_CHARACTERS_MESSAGE = (
    "The username must be printable text, with no line breaks or other control characters."
)
USERNAME_COLON_MESSAGE = (
    "The username cannot contain a colon, which Docker uses to separate it from the token."
)
TOKEN_REQUIRED_MESSAGE = "Enter the token or password."  # noqa: S105
TOKEN_LENGTH_MESSAGE = f"The token can have at most {MAX_TOKEN_LENGTH} characters."
TOKEN_CHARACTERS_MESSAGE = (
    "The token cannot contain line breaks or other control characters. "  # noqa: S105
    "Check for a stray line break after pasting it."
)
NOT_FOUND_MESSAGE = "This registry is no longer in the list."

DOCKER_HUB_HOSTS = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
DOCKER_HUB_KEY = "https://index.docker.io/v1/"

_SCHEMES = ("https://", "http://")
_HOST_CHARACTERS = re.compile(r"[a-z0-9._:-]+")
_HOST_NAME = re.compile(r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)*")
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class RegistryFormError(Exception):
    """The values were refused; ``errors`` maps a field name (or ``form``) to its message."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the safe messages by field."""
        super().__init__("; ".join(errors.values()))
        self.errors = errors


class RegistryNotFoundError(RegistryFormError):
    """The registry is not in the list (anymore)."""

    def __init__(self) -> None:
        """Refuse with the fixed message under the ``form`` key."""
        super().__init__({FORM_KEY: NOT_FOUND_MESSAGE})


class RegistryTokenError(Exception):
    """A stored token cannot be opened with the current master key. The message is safe to show."""

    def __init__(self, host: str) -> None:
        """Name the registry, never a value."""
        super().__init__(
            f"The stored token of the registry {host} cannot be opened with the current key. "
            "Enter it again in Settings, Registries."
        )
        self.host = host


@dataclass(frozen=True)
class RegistryLogin:
    """One opened login, for building a Docker config. The token never shows in a ``repr``."""

    host: str
    username: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class _Clean:
    """Validated values ready to store; ``token`` is None when none was given."""

    host: str
    username: str
    token: str | None


def registry_context(registry_id: int) -> bytes:
    """The authenticated context that binds a sealed token to its row."""
    return b"registry:" + str(registry_id).encode()


def auth_key(host: str) -> str:
    """The key of a host in the ``auths`` of a Docker config.

    The Docker Hub names all use the one key Docker itself looks Docker Hub up by.
    """
    return DOCKER_HUB_KEY if host.lower() in DOCKER_HUB_HOSTS else host


def normalize_host(raw: str) -> str:
    """The stored form of a typed registry host, or a ``RegistryFormError`` on the ``host`` field.

    Surrounding spaces, the case, an ``http://`` or ``https://`` prefix and one trailing slash
    are dropped. What is left must be a host name with an optional port; a path, a space, a
    user name or any other scheme is refused.
    """
    text = raw.strip().lower()
    for scheme in _SCHEMES:
        if text.startswith(scheme):
            text = text.removeprefix(scheme)
            break
    text = text.removesuffix("/")
    message = _host_refusal(text)
    if message is not None:
        raise RegistryFormError({HOST_KEY: message})
    return text


def _is_port(text: str) -> bool:
    """Whether text is a port number from 1 to 65535 written without leading zeros."""
    return (
        text.isdigit()
        and text[0] != "0"
        and len(text) <= len(str(MAX_PORT))
        and int(text) <= MAX_PORT
    )


def _host_refusal(text: str) -> str | None:
    """Why a host without scheme and trailing slash is refused, None when it is acceptable."""
    if not text:
        return HOST_EMPTY_MESSAGE
    if "://" in text:
        return HOST_INVALID_MESSAGE
    if "/" in text:
        return HOST_PATH_MESSAGE
    if not _HOST_CHARACTERS.fullmatch(text):
        return HOST_INVALID_MESSAGE
    name, separator, port = text.partition(":")
    if separator and not _is_port(port):
        return HOST_PORT_MESSAGE
    if len(name) > MAX_HOST_LENGTH or not _HOST_NAME.fullmatch(name):
        return HOST_INVALID_MESSAGE
    return None


def _username_refusal(username: str) -> str | None:
    """Why a trimmed username is refused, None when it is acceptable."""
    if not username:
        return USERNAME_EMPTY_MESSAGE
    if len(username) > MAX_USERNAME_LENGTH:
        return USERNAME_LENGTH_MESSAGE
    if not username.isprintable():
        return USERNAME_CHARACTERS_MESSAGE
    if ":" in username:
        return USERNAME_COLON_MESSAGE
    return None


def _token_refusal(token: str) -> str | None:
    """Why a non-empty token is refused, None when it is acceptable."""
    if len(token) > MAX_TOKEN_LENGTH:
        return TOKEN_LENGTH_MESSAGE
    if any(unicodedata.category(character) in _HIDDEN_CATEGORIES for character in token):
        return TOKEN_CHARACTERS_MESSAGE
    return None


def _clean(host: str, username: str, token: str, *, require_token: bool) -> _Clean:
    """Validate every field and raise one error that holds the message of each refused one.

    A blank token is no token: it is refused when ``require_token`` and means "keep the stored
    one" otherwise.
    """
    errors: dict[str, str] = {}
    clean_host = ""
    try:
        clean_host = normalize_host(host)
    except RegistryFormError as error:
        errors.update(error.errors)
    clean_username = username.strip()
    message = _username_refusal(clean_username)
    if message is not None:
        errors[USERNAME_KEY] = message
    given = token if token.strip() else None
    if given is None and require_token:
        errors[TOKEN_KEY] = TOKEN_REQUIRED_MESSAGE
    elif given is not None:
        message = _token_refusal(given)
        if message is not None:
            errors[TOKEN_KEY] = message
    if errors:
        raise RegistryFormError(errors)
    return _Clean(clean_host, clean_username, given)


def _taken_message(host: str, other_host: str) -> str:
    """The refusal for a host that the list already holds, as itself or as the same registry."""
    if host == other_host:
        return f"The registry {host} is already in the list."
    return f"The registry {host} is the same as {other_host}, which is already in the list."


async def _require_free(db: AsyncSession, host: str, *, ignore_id: int | None = None) -> None:
    """Refuse a host the list already holds, ignoring case and the Docker Hub aliases.

    ``ignore_id`` leaves out the row being edited.
    """
    key = auth_key(host)
    rows = (await db.execute(select(DockerRegistry.id, DockerRegistry.host))).all()
    for other_id, other_host in rows:
        if other_id != ignore_id and auth_key(other_host) == key:
            raise RegistryFormError({HOST_KEY: _taken_message(host, other_host.lower())})


async def _flush_or_refuse(db: AsyncSession, host: str) -> None:
    """Flush, turning a duplicate that slipped in between the check and now into a refusal."""
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise RegistryFormError({HOST_KEY: _taken_message(host, host)}) from error


async def list_registries(db: AsyncSession) -> list[DockerRegistry]:
    """Every registry, in host order."""
    return list((await db.execute(select(DockerRegistry).order_by(DockerRegistry.host))).scalars())


async def add_registry(
    db: AsyncSession,
    crypto: CryptoService,
    host: str,
    username: str,
    token: str,
    *,
    actor: User,
    ip_address: str | None = None,
) -> DockerRegistry:
    """Validate and store a registry with its sealed token, audit it and commit.

    Raises ``RegistryFormError`` with a message for each refused field, and for a host the list
    already holds.
    """
    clean = _clean(host, username, token, require_token=True)
    await _require_free(db, clean.host)
    registry = DockerRegistry(host=clean.host, username=clean.username, token_encrypted="")
    db.add(registry)
    await _flush_or_refuse(db, clean.host)
    registry.token_encrypted = crypto.encrypt(token, registry_context(registry.id))
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action=ACTION_ADD,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_id=registry.id,
        target_name=registry.host,
        details={"host": registry.host, "username": registry.username},
    )
    await db.commit()
    return registry


async def update_registry(
    db: AsyncSession,
    crypto: CryptoService,
    registry_id: int,
    host: str,
    username: str,
    token: str,
    *,
    actor: User,
    ip_address: str | None = None,
) -> DockerRegistry:
    """Change a registry and commit; a blank token keeps the stored one.

    Nothing is written, and nothing audited, when no value differs. The audit entry names the
    new host and username, whether each of host, username and token changed and the previous
    host and username when they did, never the token.
    Raises ``RegistryNotFoundError`` when the registry is gone and ``RegistryFormError`` for
    refused values.
    """
    registry = await db.get(DockerRegistry, registry_id)
    if registry is None:
        raise RegistryNotFoundError
    clean = _clean(host, username, token, require_token=False)
    await _require_free(db, clean.host, ignore_id=registry.id)
    host_changed = clean.host != registry.host
    username_changed = clean.username != registry.username
    token_changed = clean.token is not None
    if not (host_changed or username_changed or token_changed):
        return registry
    details: dict[str, object] = {
        "host": clean.host,
        "username": clean.username,
        "host_changed": host_changed,
        "username_changed": username_changed,
        "token_changed": token_changed,
    }
    if host_changed:
        details["previous_host"] = registry.host
    if username_changed:
        details["previous_username"] = registry.username
    registry.host = clean.host
    registry.username = clean.username
    if clean.token is not None:
        registry.token_encrypted = crypto.encrypt(clean.token, registry_context(registry.id))
    await _flush_or_refuse(db, clean.host)
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action=ACTION_UPDATE,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_id=registry.id,
        target_name=registry.host,
        details=details,
    )
    await db.commit()
    return registry


async def remove_registry(
    db: AsyncSession, registry_id: int, *, actor: User, ip_address: str | None = None
) -> DockerRegistry:
    """Remove a registry and its sealed token, audit it and commit.

    Raises ``RegistryNotFoundError`` when the registry is already gone.
    """
    registry = await db.get(DockerRegistry, registry_id)
    if registry is None:
        raise RegistryNotFoundError
    await db.delete(registry)
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action=ACTION_REMOVE,
        result=AuditResult.SUCCESS,
        target_type=TARGET_TYPE,
        target_id=registry.id,
        target_name=registry.host,
        details={"host": registry.host, "username": registry.username},
    )
    await db.commit()
    return registry


async def load_registry_logins(
    db: AsyncSession, crypto: CryptoService
) -> tuple[RegistryLogin, ...]:
    """Every login with its token opened, in host order.

    A token that cannot be opened (another key, a value moved to another row) raises
    ``RegistryTokenError`` naming the registry; nothing is skipped silently.
    """
    logins = []
    for registry in await list_registries(db):
        try:
            token = crypto.decrypt_text(registry.token_encrypted, registry_context(registry.id))
        except CryptoError as error:
            raise RegistryTokenError(registry.host) from error
        logins.append(RegistryLogin(registry.host, registry.username, token))
    return tuple(logins)


def docker_config(logins: Iterable[RegistryLogin]) -> dict[str, dict[str, dict[str, str]]]:
    """The ``config.json`` content that gives ``docker`` these logins.

    Each entry is the base64 of ``username:token`` under the host (the Docker Hub names share
    ``DOCKER_HUB_KEY``). Should two logins share a key, the first one is kept.
    """
    auths: dict[str, dict[str, str]] = {}
    for login in logins:
        credentials = base64.b64encode(f"{login.username}:{login.token}".encode()).decode("ascii")
        auths.setdefault(auth_key(login.host), {"auth": credentials})
    return {"auths": auths}
