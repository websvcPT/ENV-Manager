"""What the Git tab shows about the sync: the last push, the last failure and the pinned key."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.display_time import DisplayTime
from app.models import SYNC_STATE_ID, GitSyncState
from app.services.app_settings import GitSettings
from app.services.git_hostkey import parse_entry
from app.services.git_remote import RemoteError, RemoteKind, parse_remote
from app.services.git_state import read_state


@dataclass(frozen=True)
class PinnedKeyView:
    """The pinned host key as shown: the host it is for, its type and its fingerprint."""

    host: str
    key_type: str
    fingerprint: str


@dataclass(frozen=True)
class GitStatus:
    """The state of git sync for the settings tab.

    ``ssh`` says whether the saved address is an ssh one (the only kind with a host key). The
    times are already written in the display zone. ``failing`` is true while the last run failed.
    """

    configured: bool
    ssh: bool
    last_push: str | None
    last_failure: str | None
    last_error: str | None
    failing: bool
    pinned: PinnedKeyView | None


async def load_git_status(db: AsyncSession, git: GitSettings, display: DisplayTime) -> GitStatus:
    """The sync state for the tab. Nothing here reads a secret or writes a row."""
    state = await read_state(db) or GitSyncState(id=SYNC_STATE_ID, failures=0)
    ssh = False
    pinned = None
    if git.configured:
        try:
            remote = parse_remote(git.url)
        except RemoteError:
            remote = None
        ssh = remote is not None and remote.kind is RemoteKind.SSH
        if remote is not None and ssh and state.host_key and state.host_key_fingerprint:
            key = parse_entry(state.host_key)
            if key is not None and state.host_key_host == remote.known_hosts_name:
                pinned = PinnedKeyView(remote.label, key.key_type, state.host_key_fingerprint)
    failing = state.failures > 0
    return GitStatus(
        configured=git.configured,
        ssh=ssh,
        last_push=display.text(state.last_success_at) if state.last_success_at else None,
        last_failure=(
            display.text(state.last_attempt_at) if failing and state.last_attempt_at else None
        ),
        last_error=state.last_error if failing else None,
        failing=failing,
        pinned=pinned,
    )
