"""Pushing the encrypted export: when a push is due, the attempts, and what a run records.

A push happens at most once an hour and only when the export changed since the last one that
worked, which the watermark (the change mark of that export) tells. A due run tries up to four
times, waiting 5, 30 and 120 seconds, but only for faults that can pass (an unreachable server, a
timeout); a refused credential or a changed host key stops at once. A run that still fails is
recorded (one more failure, a longer wait before the next run, the fixed reason), audited with
the host only, announced as a failed push and shown in the banner until a push works or the
settings change. Nothing here keeps or logs text printed by git.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.log import get_logger
from app.models import AuditResult
from app.services.app_settings import GitSettings, load_stored_settings
from app.services.audit import AuditActor, record_actor_audit
from app.services.git_auth import git_access
from app.services.git_errors import GitProblem, GitSyncError
from app.services.git_export import ExportStats, compute_mark, export_tree
from app.services.git_hostkey import pinned_key
from app.services.git_repo import GitRepository
from app.services.git_services import GitServices, load_target, parse_saved_address
from app.services.git_state import load_state, record_failure, record_success
from app.services.notify_events import GitPushFailed, announce_event

log = get_logger("git_push")

ACTION_PUSH = "git.push"
TARGET_TYPE = "git"
SCHEDULER_ACTOR = "scheduler"
RETRY_DELAYS = (5.0, 30.0, 120.0)
# The scheduler fires every hour; a push that ended a few minutes after the hour must not make
# the next tick skip, so "an hour since the last push" is read with this much slack.
MIN_PUSH_GAP_SECONDS = 55 * 60


class PushStatus(StrEnum):
    """How a run ended."""

    SKIPPED = "skipped"
    PUSHED = "pushed"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class SkipReason(StrEnum):
    """Why a run did nothing."""

    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    WAITING = "waiting"
    RECENT = "recent"
    NO_CHANGE = "no_change"
    SETTINGS_CHANGED = "settings_changed"


@dataclass(frozen=True)
class PushResult:
    """What a run did. ``reason`` is the fixed sentence of a failure, ``skip`` says why a run
    did nothing, and ``attempts`` counts the tries made."""

    status: PushStatus
    attempts: int = 0
    reason: str | None = None
    skip: SkipReason | None = None


@dataclass(frozen=True)
class _Pushed:
    """The outcome of one successful attempt."""

    committed: bool
    stats: ExportStats


def _skipped(reason: SkipReason) -> PushResult:
    """A result for a run that did nothing."""
    return PushResult(PushStatus.SKIPPED, skip=reason)


async def _attempt(db: AsyncSession, services: GitServices, git: GitSettings) -> _Pushed:
    """One try: look at the remote, match the clone, write the export, commit and push.

    Raises ``GitSyncError`` for any failure. The database transaction of the export reads is
    ended before the clone is committed and pushed, so a slow server never holds it open.
    """
    loaded = await load_target(db, services, git)
    remote = loaded.target.remote
    state = await load_state(db)
    pin = pinned_key(state, remote)
    await db.commit()
    with git_access(remote, services.locations, loaded.secrets, pin) as access:
        repository = GitRepository(
            services.runner,
            services.locations,
            loaded.target,
            access,
            protocols=services.protocols,
        )
        exists = await repository.remote_has_branch()
        work = await repository.prepare_work(branch_exists=exists)
        stats = await export_tree(
            db,
            services.crypto,
            work,
            git,
            database_path=services.database_path,
            scratch=services.locations.scratch,
        )
        await db.commit()
        stamp = services.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        committed = await repository.commit_all(f"Encrypted export {stamp}")
        if committed:
            await repository.push()
    return _Pushed(committed, stats)


async def _audit_push(
    db: AsyncSession,
    actor: AuditActor,
    *,
    host: str,
    result: AuditResult,
    details: dict[str, object],
) -> None:
    """Write the audit entry of a run. It names the host only, never the address or a secret."""
    await record_actor_audit(
        db,
        actor,
        action=ACTION_PUSH,
        result=result,
        target_type=TARGET_TYPE,
        target_name=host or None,
        details={"host": host, **details},
    )


async def _record_success(
    db: AsyncSession,
    services: GitServices,
    git: GitSettings,
    mark: str,
    pushed: _Pushed,
    attempts: int,
    actor: AuditActor,
) -> PushResult:
    """Store the new watermark and, for a real push, audit it."""
    state = await load_state(db)
    record_success(state, mark, services.clock(), pushed=pushed.committed)
    if pushed.committed:
        host = parse_saved_address(services, git).label
        await _audit_push(
            db,
            actor,
            host=host,
            result=AuditResult.SUCCESS,
            details={
                "format": git.export_format,
                "files_written": pushed.stats.files_written,
                "rebuilt": pushed.stats.rebuilt,
                "attempts": attempts,
            },
        )
    await db.commit()
    return PushResult(PushStatus.PUSHED if pushed.committed else PushStatus.UNCHANGED, attempts)


async def _record_failure(
    db: AsyncSession,
    services: GitServices,
    git: GitSettings,
    failure: GitSyncError,
    attempts: int,
    actor: AuditActor,
) -> PushResult:
    """Store the failure, audit it, and announce it once it is committed.

    A run whose settings were changed while it ran records nothing: the save already gave the
    new settings a fresh start, and a failure of the old ones says nothing about them.
    """
    await db.rollback()
    if (await load_stored_settings(db)).git != git:
        return _skipped(SkipReason.SETTINGS_CHANGED)
    state = await load_state(db)
    record_failure(state, failure.message, services.clock())
    try:
        host = parse_saved_address(services, git).label
    except GitSyncError:
        host = ""
    await _audit_push(
        db,
        actor,
        host=host,
        result=AuditResult.FAILURE,
        details={
            "format": git.export_format,
            "problem": failure.problem.value,
            "reason": failure.message,
            "attempts": attempts,
        },
    )
    await db.commit()
    await announce_event(services.notifier(), GitPushFailed(failure.message, attempts))
    return PushResult(PushStatus.FAILED, attempts, failure.message)


async def push_if_due(db: AsyncSession, services: GitServices, *, actor: AuditActor) -> PushResult:
    """Push the export when it is due: configured, on, past the wait, changed since the last push.

    Settings, state and the change mark are read here, so a change is effective for the next
    run. A failure is a recorded result, never an exception.
    """
    git = (await load_stored_settings(db)).git
    if not git.configured:
        return _skipped(SkipReason.NOT_CONFIGURED)
    if not git.push_enabled:
        return _skipped(SkipReason.DISABLED)
    state = await load_state(db)
    now = services.clock()
    if state.retry_at is not None and now < state.retry_at:
        return _skipped(SkipReason.WAITING)
    if (
        state.last_success_at is not None
        and (now - state.last_success_at).total_seconds() < MIN_PUSH_GAP_SECONDS
    ):
        return _skipped(SkipReason.RECENT)
    mark = await compute_mark(db, services.crypto, git)
    if mark == state.watermark:
        await db.commit()
        return _skipped(SkipReason.NO_CHANGE)
    await db.commit()
    attempts = 0
    failure = GitSyncError(GitProblem.FAILED)
    for delay in (0.0, *RETRY_DELAYS):
        if delay:
            await services.sleep(delay)
        attempts += 1
        try:
            pushed = await _attempt(db, services, git)
        except GitSyncError as error:
            await db.rollback()
            failure = error
            if not error.transient:
                break
            continue
        except Exception as error:
            log.error("a git push attempt failed unexpectedly: %s", type(error).__name__)
            await db.rollback()
            failure = GitSyncError(GitProblem.FAILED)
            continue
        return await _record_success(db, services, git, mark, pushed, attempts, actor)
    return await _record_failure(db, services, git, failure, attempts, actor)


async def run_scheduled_push(
    session_factory: async_sessionmaker[AsyncSession], services: GitServices
) -> None:
    """The scheduled job: a push if one is due, by the scheduler, never an exception."""
    try:
        async with session_factory() as db:
            await push_if_due(db, services, actor=AuditActor.system(SCHEDULER_ACTOR))
    except Exception as error:
        log.error("the scheduled git push failed unexpectedly: %s", type(error).__name__)
