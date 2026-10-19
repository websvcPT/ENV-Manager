"""Credentials for one git call: the deploy key or the access token, in files that live briefly.

Neither credential ever appears in an argument list, an environment or the remote
configuration. For ssh, the private key and the pinned host key are written to files of mode
0600 in a private folder, and ``GIT_SSH_COMMAND`` names those files with strict host key checking
against only the pinned key, no agent and no other identity. For https, the token is written to a
file in the format of git's own ``store`` credential helper, which git is told to use for this
call; the address stays free of it. The folder is removed when the call ends, whatever happened.
"""

import base64
import os
import shlex
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.services.git_errors import GitProblem, GitSyncError
from app.services.git_hostkey import HostKey
from app.services.git_locations import GitLocations
from app.services.git_remote import GitRemote, RemoteKind

FILE_MODE = 0o600
CONNECT_TIMEOUT_SECONDS = 20
KEEPALIVE_SECONDS = 15
KEEPALIVE_COUNT = 3
MAX_KEY_LENGTH = 16384
KEY_BEGIN = "-----BEGIN "
KEY_END = "PRIVATE KEY-----"
OPENSSH_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
OPENSSH_MAGIC = b"openssh-key-v1\x00"


@dataclass(frozen=True)
class GitSecrets:
    """The stored credentials, decrypted for the length of one operation."""

    deploy_key: str | None = None
    token: str | None = None


@dataclass(frozen=True)
class GitAccess:
    """What a git call needs to authenticate: extra environment entries and leading options."""

    env: dict[str, str]
    args: tuple[str, ...]


NO_ACCESS = GitAccess({}, ())


def normalize_deploy_key(text: str) -> str:
    """The key with LF line ends and one final newline, which ssh requires.

    A browser sends line breaks as CRLF and an editor may drop the last one; ssh then reports an
    invalid format.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n \t").split("\n")
    return "\n".join(line.rstrip() for line in lines) + "\n"


def deploy_key_problem(text: str) -> str | None:
    """Why the text is not a usable private key, or None when it looks like one."""
    if len(text) > MAX_KEY_LENGTH or "\x00" in text:
        return f"Use printable text of at most {MAX_KEY_LENGTH} characters."
    first = text.strip().splitlines()[0] if text.strip() else ""
    if first.startswith(("ssh-", "ecdsa-")):
        return "That is a public key. Paste the private key."
    if not first.startswith(KEY_BEGIN) or KEY_END not in first:
        return "Paste the whole private key, starting with -----BEGIN ... PRIVATE KEY-----."
    if "ENCRYPTED" in first or "Proc-Type: 4,ENCRYPTED" in text:
        return "The key must not have a passphrase."
    if _openssh_cipher(text) not in (None, "none"):
        return "The key must not have a passphrase."
    return None


def _openssh_cipher(text: str) -> str | None:
    """The cipher name an OpenSSH format key declares, None for any other or unreadable text.

    The body of an ``openssh-key-v1`` key starts with a magic string and the name of the cipher
    that seals the private part: ``none`` for a key without a passphrase. Only a body that
    decodes and carries the magic is judged; everything else is left to the other checks.
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines or lines[0] != OPENSSH_BEGIN:
        return None
    body = "".join(line for line in lines[1:] if not line.startswith("-----"))
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError:
        return None
    if not raw.startswith(OPENSSH_MAGIC) or len(raw) < len(OPENSSH_MAGIC) + 4:
        return None
    start = len(OPENSSH_MAGIC)
    length = int.from_bytes(raw[start : start + 4], "big")
    name = raw[start + 4 : start + 4 + length]
    if len(name) != length:
        return None
    return name.decode("ascii", errors="replace")


def ssh_command(key_path: Path, known_hosts_path: Path, key_type: str) -> str:
    """The ``GIT_SSH_COMMAND`` for one call.

    git runs it through a shell, so every path is quoted. The options leave no room for another
    identity or a prompt: only the given key, no agent or forwarding, and a host key that must
    be the pinned one, of the pinned type.
    """
    options = [
        "-F",
        "/dev/null",
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"HostKeyAlgorithms={key_type}",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        "-o",
        f"ServerAliveInterval={KEEPALIVE_SECONDS}",
        "-o",
        f"ServerAliveCountMax={KEEPALIVE_COUNT}",
    ]
    return " ".join(["ssh", *(shlex.quote(option) for option in options)])


def token_store_line(remote: GitRemote, token: str) -> str:
    """The line of git's ``store`` credential file that holds the token for this address."""
    user = quote(remote.token_user, safe="")
    host = remote.host if remote.port is None else f"{remote.host}:{remote.port}"
    return f"https://{user}:{quote(token, safe='')}@{host}\n"


def _write_private(path: Path, text: str) -> None:
    """Create a new file with mode 0600 from the first byte and write the text."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _ssh_access(folder: Path, secrets: GitSecrets, pin: HostKey | None) -> GitAccess:
    """The access for an ssh address: key and pin files and the command that uses them."""
    if secrets.deploy_key is None:
        raise GitSyncError(GitProblem.NO_DEPLOY_KEY)
    if pin is None:
        raise GitSyncError(GitProblem.NOT_PINNED)
    key_path = folder / "key"
    known_hosts = folder / "known_hosts"
    _write_private(key_path, normalize_deploy_key(secrets.deploy_key))
    _write_private(known_hosts, pin.entry + "\n")
    return GitAccess(
        {
            "GIT_SSH_COMMAND": ssh_command(key_path, known_hosts, pin.key_type),
            "GIT_SSH_VARIANT": "ssh",
        },
        (),
    )


def _https_access(folder: Path, remote: GitRemote, secrets: GitSecrets) -> GitAccess:
    """The access for an https address: a credential file for git's own store helper."""
    if secrets.token is None:
        raise GitSyncError(GitProblem.NO_TOKEN)
    store = folder / "credentials"
    _write_private(store, token_store_line(remote, secrets.token))
    helper = f"store --file={shlex.quote(str(store))}"
    return GitAccess({}, ("-c", "credential.helper=", "-c", f"credential.helper={helper}"))


@contextmanager
def git_access(
    remote: GitRemote, locations: GitLocations, secrets: GitSecrets, pin: HostKey | None
) -> Iterator[GitAccess]:
    """The access for one call, with its files in a private folder removed afterwards.

    Raises ``GitSyncError`` when the credential the address needs is missing, or for an ssh
    address while no host key is pinned.
    """
    if remote.kind is RemoteKind.FILE:
        yield NO_ACCESS
        return
    locations.ensure()
    try:
        folder = Path(tempfile.mkdtemp(prefix="call-", dir=locations.scratch))
    except OSError as error:
        raise GitSyncError(GitProblem.WORKDIR) from error
    try:
        try:
            if remote.kind is RemoteKind.SSH:
                access = _ssh_access(folder, secrets, pin)
            else:
                access = _https_access(folder, remote, secrets)
        except OSError as error:
            raise GitSyncError(GitProblem.WORKDIR) from error
        yield access
    finally:
        shutil.rmtree(folder, ignore_errors=True)
