"""SSH host keys: scanning a server, fingerprints and the pinned key.

A host key is pinned once, by the superadmin, after seeing its fingerprint. From then on every
ssh call runs with strict host key checking against only that key, so a changed key is refused
before anything is sent. The scan only reads what the server offers; it is never trusted by
itself.
"""

import base64
import binascii
import hashlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from app.models import GitSyncState
from app.services.git_errors import GitProblem, GitSyncError
from app.services.git_locations import GitLocations
from app.services.git_process import LOCAL_TIMEOUT, GitRunner, git_environment
from app.services.git_remote import GitRemote, RemoteKind

SCAN_TYPES = "ed25519,ecdsa,rsa"
SCAN_CONNECT_SECONDS = 10
# The key a pin is taken from when a server offers several: the strongest known type first.
KEY_PREFERENCE = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


@dataclass(frozen=True)
class HostKey:
    """A public host key of a server: the name it is looked up by, its type and its blob."""

    host: str
    key_type: str
    key: str

    @property
    def fingerprint(self) -> str:
        """The SHA256 fingerprint in the form ssh and ssh-keygen print."""
        digest = hashlib.sha256(base64.b64decode(self.key)).digest()
        return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")

    @property
    def entry(self) -> str:
        """The key as one known_hosts line."""
        return f"{self.host} {self.key_type} {self.key}"

    def same_key(self, other: HostKey) -> bool:
        """Whether both are the same key (type and blob), whatever the host name."""
        return self.key_type == other.key_type and self.key == other.key


def _well_formed(key_type: str, key: str) -> bool:
    """Whether the blob is valid base64 and names the declared key type inside."""
    try:
        blob = base64.b64decode(key, validate=True)
    except binascii.Error, ValueError:
        return False
    if len(blob) < 4:
        return False
    (length,) = struct.unpack(">I", blob[:4])
    return blob[4 : 4 + length] == key_type.encode("ascii")


def parse_entry(entry: str) -> HostKey | None:
    """The key in a known_hosts line (``host type blob``), None when the line is not one."""
    fields = entry.split()
    if len(fields) < 3 or fields[1] not in KEY_PREFERENCE or not _well_formed(fields[1], fields[2]):
        return None
    return HostKey(fields[0], fields[1], fields[2])


def parse_scan_output(output: str, host: str) -> list[HostKey]:
    """The keys in the output of ssh-keyscan, filed under ``host``.

    Comment lines and anything that is not a well-formed key line are dropped, and the host
    name the server side printed is replaced by the one this address is looked up by.
    """
    keys: list[HostKey] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3 or line.startswith("#"):
            continue
        key = parse_entry(f"{host} {fields[1]} {fields[2]}")
        if key is not None and not any(key.same_key(known) for known in keys):
            keys.append(key)
    return keys


def preferred_key(keys: Sequence[HostKey]) -> HostKey | None:
    """The key to offer for pinning: the first of the preferred types the server has."""
    for key_type in KEY_PREFERENCE:
        for key in keys:
            if key.key_type == key_type:
                return key
    return None


def pinned_key(state: GitSyncState, remote: GitRemote) -> HostKey | None:
    """The key pinned for this address, None when none is pinned or the pin is for another host."""
    if remote.kind is not RemoteKind.SSH:
        return None
    if not state.host_key or state.host_key_host != remote.known_hosts_name:
        return None
    return parse_entry(state.host_key)


async def scan_host_keys(
    runner: GitRunner, locations: GitLocations, remote: GitRemote
) -> list[HostKey]:
    """Ask the server of an ssh address for its host keys.

    Raises ``GitSyncError`` when the server cannot be reached or offers no usable key.
    """
    locations.ensure()
    argv = ["ssh-keyscan", "-T", str(SCAN_CONNECT_SECONDS), "-t", SCAN_TYPES]
    if remote.port is not None:
        argv += ["-p", str(remote.port)]
    argv += ["--", remote.host.strip("[]")]
    result = await runner.run(
        argv,
        cwd=locations.home,
        env=git_environment(locations.home),
        timeout=LOCAL_TIMEOUT,
    )
    if result.timed_out:
        raise GitSyncError(GitProblem.TIMEOUT)
    keys = parse_scan_output(result.text, remote.known_hosts_name)
    if not keys:
        raise GitSyncError(GitProblem.NETWORK)
    return keys
