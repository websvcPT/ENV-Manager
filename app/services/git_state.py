"""Reading and changing the single row that tracks git sync.

The row holds the watermark, the outcome of the last attempts, when the next attempt may start
and the pinned host key. It is created on first use. Callers commit.
"""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SYNC_STATE_ID, GitSyncState
from app.services.git_hostkey import HostKey

BACKOFF_BASE = timedelta(hours=1)
BACKOFF_CAP = timedelta(hours=24)


async def load_state(db: AsyncSession) -> GitSyncState:
    """The state row, created empty when it does not exist yet."""
    state = await db.get(GitSyncState, SYNC_STATE_ID)
    if state is None:
        state = GitSyncState(id=SYNC_STATE_ID, failures=0)
        db.add(state)
        await db.flush()
    return state


def backoff_after(failures: int) -> timedelta:
    """How long to wait after the given number of failed runs in a row.

    An hour after the first, then twice as long each time, up to a day.
    """
    if failures < 1:
        return timedelta(0)
    return min(BACKOFF_BASE * (1 << (failures - 1)), BACKOFF_CAP)


def record_failure(state: GitSyncState, reason: str, now: datetime) -> None:
    """Note a failed run: the reason, one more failure and when the next attempt may start."""
    state.failures += 1
    state.last_attempt_at = now
    state.last_error = reason
    state.retry_at = now + backoff_after(state.failures)


def record_success(state: GitSyncState, mark: str, now: datetime, *, pushed: bool) -> None:
    """Note a run that worked: the mark is the new watermark and the failures are forgotten.

    A run that found nothing to commit (the repository already holds this export) moves the
    watermark but does not count as a push.
    """
    state.watermark = mark
    state.last_attempt_at = now
    state.failures = 0
    state.last_error = None
    state.retry_at = None
    if pushed:
        state.last_success_at = now


def forget_failures(state: GitSyncState) -> None:
    """Forget the failures and the wait, so the next scheduled run tries again at once."""
    state.failures = 0
    state.last_error = None
    state.retry_at = None


def reset_progress(state: GitSyncState) -> None:
    """Forget what was pushed, so the next run pushes the whole export again."""
    state.watermark = None
    state.last_success_at = None
    forget_failures(state)


def pin_host_key(state: GitSyncState, key: HostKey, now: datetime) -> None:
    """Store a host key as the pinned one."""
    state.host_key_host = key.host
    state.host_key = key.entry
    state.host_key_fingerprint = key.fingerprint
    state.host_key_pinned_at = now


def clear_host_key(state: GitSyncState) -> None:
    """Forget the pinned host key."""
    state.host_key_host = None
    state.host_key = None
    state.host_key_fingerprint = None
    state.host_key_pinned_at = None
