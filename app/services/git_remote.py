"""The address of the git repository: which transport it uses and the host behind it.

Only an ssh address (``ssh://git@host:2222/path.git`` or ``git@host:path.git``) and an https
address are accepted from a person. A token is never sent over http, a password in the address is
refused (the token has its own field), an address may not start with ``-`` (it would read as an
option) and nothing that names another transport helper (``ext::``) gets through. A local path
or ``file://`` address exists for the tests of the service layer only: it is accepted when the
caller asks for it, never from the settings form, because a typed path would write outside the
allowed paths.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "git"
_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)$")
_USER = re.compile(r"^[A-Za-z0-9._~-]+$")
_BRACKETS = re.compile(r"\[[0-9A-Fa-f:.]*\]")
_SCP_LIKE = re.compile(
    r"^(?:(?P<user>[^@/:\s]+)@)?(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):(?P<path>(?!//).+)$"
)

ADDRESS_HELP = "Use an ssh:// or https:// address, or the form git@host:path."


class RemoteKind(StrEnum):
    """The transport an address uses."""

    SSH = "ssh"
    HTTPS = "https"
    FILE = "file"


class RemoteError(ValueError):
    """The address is not acceptable; the message says why and holds no part of the address."""


@dataclass(frozen=True)
class GitRemote:
    """A parsed repository address.

    ``host`` is empty for a local path. ``user`` is the user name given in the address, if any;
    ``port`` is None when the address names none.
    """

    kind: RemoteKind
    url: str
    host: str
    port: int | None
    user: str | None

    @property
    def known_hosts_name(self) -> str:
        """The name the SSH client looks a host key up by: the host, or ``[host]:port``."""
        bare = self.host.strip("[]")
        if self.port is None or self.port == DEFAULT_SSH_PORT:
            return bare
        return f"[{bare}]:{self.port}"

    @property
    def label(self) -> str:
        """The host (and port) for audit entries and messages, without user or path."""
        if self.kind is RemoteKind.FILE:
            return "local path"
        return f"{self.host}:{self.port}" if self.port is not None else self.host

    @property
    def token_user(self) -> str:
        """The user name sent with an access token: the one in the address, else a neutral one."""
        return self.user or "x-access-token"


def _valid_host(host: str) -> bool:
    """Whether the text is a host name, an IPv4 address or a bracketed IPv6 address."""
    return len(host) <= MAX_HOST_LENGTH and bool(_HOST.fullmatch(host))


def _scheme_address(text: str) -> GitRemote:
    """Parse an address with a scheme (``ssh://`` or ``https://``)."""
    scheme = text.partition("://")[0].lower()
    if scheme == "http":
        raise RemoteError("Use https. An access token is never sent over http.")
    if scheme not in {RemoteKind.SSH, RemoteKind.HTTPS}:
        raise RemoteError(ADDRESS_HELP)
    try:
        parts = urlsplit(text)
        port = parts.port
        host = parts.hostname or ""
    except ValueError as error:
        raise RemoteError("The address has an invalid host or port.") from error
    if ":" in host:
        host = f"[{host}]"
    if parts.password is not None:
        raise RemoteError(
            "Do not put a password or token in the address. Use the credential field."
        )
    if parts.query or parts.fragment:
        raise RemoteError("The address must not have a query or a fragment.")
    if not host or not _valid_host(host):
        raise RemoteError("The address needs a host name.")
    if parts.username is not None and not _USER.fullmatch(parts.username):
        raise RemoteError("The user name in the address has characters that are not allowed.")
    if port is not None and not 1 <= port <= 65535:
        raise RemoteError("The port in the address is not valid.")
    if len(parts.path.strip("/")) == 0:
        raise RemoteError("The address needs a repository path.")
    kind = RemoteKind(scheme)
    user = parts.username or (DEFAULT_SSH_USER if kind is RemoteKind.SSH else None)
    return GitRemote(kind, text, host, port, user)


def _scp_address(text: str) -> GitRemote:
    """Parse the short ssh form ``[user@]host:path``."""
    found = _SCP_LIKE.match(text)
    if found is None:
        raise RemoteError(ADDRESS_HELP)
    host = found.group("host")
    user = found.group("user") or DEFAULT_SSH_USER
    if not _valid_host(host):
        raise RemoteError("The address needs a host name.")
    if not _USER.fullmatch(user):
        raise RemoteError("The user name in the address has characters that are not allowed.")
    return GitRemote(RemoteKind.SSH, text, host, None, user)


def parse_remote(address: str, *, allow_local: bool = False) -> GitRemote:
    """The parsed address, or ``RemoteError`` saying what is wrong with it.

    ``allow_local`` accepts an absolute path or a ``file://`` address (tests only).
    """
    text = address.strip()
    if not text:
        raise RemoteError("Enter the address of the repository.")
    if len(text) > MAX_URL_LENGTH:
        raise RemoteError(f"Use at most {MAX_URL_LENGTH} characters.")
    if text.startswith("-") or any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise RemoteError(ADDRESS_HELP)
    if "::" in _BRACKETS.sub("", text.split("/", 1)[0]):
        # A transport helper such as ext:: or fd::; an IPv6 literal is bracketed and was removed.
        raise RemoteError(ADDRESS_HELP)
    if allow_local and (text.startswith("/") or text.startswith("file://")):
        return GitRemote(RemoteKind.FILE, text, "", None, None)
    if "://" in text:
        return _scheme_address(text)
    return _scp_address(text)


_USERINFO_PASSWORD = re.compile(r"(://[^/@:\s]*):[^/@\s]*@")


def without_password(address: str) -> str:
    """The address with the password part of its user information removed.

    A refused address that carries a password is shown back to the person who typed it, and a
    token must not be put back into a page.
    """
    return _USERINFO_PASSWORD.sub(r"\1@", address)
