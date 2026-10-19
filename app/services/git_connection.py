"""Testing the connection to the repository, and pinning the SSH host key it presents.

A test reads from the repository (``ls-remote``) and sends nothing, so it proves that the
address, the credential and the host key work for reading; writing is only proved by a push. For
an ssh address the test first asks the server for its host keys. With no key pinned it stops and
shows the fingerprint for the superadmin to accept; accepting stores exactly that key, found
again at that moment, so a key that changed in between is refused. With a key pinned, a server
that does not present it is refused, naming both fingerprints, and nothing is sent. Changing the
repository address is what clears a pin.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditResult
from app.services.app_settings import GitSettings, load_stored_settings
from app.services.audit import AuditActor, record_actor_audit
from app.services.git_auth import git_access
from app.services.git_errors import PROBLEMS, GitProblem, GitSyncError
from app.services.git_hostkey import HostKey, pinned_key, preferred_key, scan_host_keys
from app.services.git_remote import RemoteKind
from app.services.git_repo import GitRepository
from app.services.git_services import GitServices, load_target, parse_saved_address
from app.services.git_state import load_state, pin_host_key

ACTION_TEST = "git.test"
ACTION_PIN = "git.host_key_pin"
TARGET_TYPE = "git"
CHANGED_AFTER_SHOWN = "The server's host key changed since it was shown. Test the connection again."
PINNED_ALREADY = "A different host key is pinned. Change the repository address to clear it."


class CheckState(StrEnum):
    """The outcome of a connection test."""

    OK = "ok"
    FAILED = "failed"
    NEEDS_PIN = "needs_pin"
    KEY_CHANGED = "key_changed"


@dataclass(frozen=True)
class ConnectionCheck:
    """The result of a test.

    ``key`` is the key the server presented (to be pinned, or the one that replaced the pinned
    key) and ``pinned_fingerprint`` the fingerprint that is pinned, when there is one.
    ``branch_exists`` says whether the branch is already on the remote (a test that worked).
    """

    state: CheckState
    message: str
    host: str = ""
    key: HostKey | None = None
    pinned_fingerprint: str | None = None
    branch_exists: bool | None = None


def _failed(problem: GitProblem, host: str = "") -> ConnectionCheck:
    """A failed test with the fixed sentence of the problem."""
    return ConnectionCheck(CheckState.FAILED, PROBLEMS[problem].message, host)


async def _audit_test(
    db: AsyncSession, actor: AuditActor, check: ConnectionCheck, git: GitSettings
) -> None:
    """Audit a test with its host and outcome; never the address or a secret."""
    details: dict[str, object] = {"host": check.host, "outcome": check.state.value}
    if check.state is not CheckState.OK:
        details["reason"] = check.message
    if check.key is not None:
        details["fingerprint"] = check.key.fingerprint
    details["format"] = git.export_format
    await record_actor_audit(
        db,
        actor,
        action=ACTION_TEST,
        result=AuditResult.SUCCESS if check.state is CheckState.OK else AuditResult.FAILURE,
        target_type=TARGET_TYPE,
        target_name=check.host or None,
        details=details,
    )
    await db.commit()


async def _probe(db: AsyncSession, services: GitServices, git: GitSettings) -> ConnectionCheck:
    """The test itself: credential, host key for ssh, then a read of the remote."""
    try:
        loaded = await load_target(db, services, git)
    except GitSyncError as error:
        try:
            host = parse_saved_address(services, git).label
        except GitSyncError:
            host = ""
        return _failed(error.problem, host)
    remote = loaded.target.remote
    host = remote.label
    pin = None
    if remote.kind is RemoteKind.SSH:
        state = await load_state(db)
        pin = pinned_key(state, remote)
        await db.commit()
        try:
            offered = await scan_host_keys(services.runner, services.locations, remote)
        except GitSyncError as error:
            return _failed(error.problem, host)
        if pin is None:
            return ConnectionCheck(
                CheckState.NEEDS_PIN,
                PROBLEMS[GitProblem.NOT_PINNED].message,
                host,
                key=preferred_key(offered),
            )
        if not any(key.same_key(pin) for key in offered):
            return ConnectionCheck(
                CheckState.KEY_CHANGED,
                PROBLEMS[GitProblem.HOST_KEY_CHANGED].message,
                host,
                key=preferred_key(offered),
                pinned_fingerprint=pin.fingerprint,
            )
    try:
        with git_access(remote, services.locations, loaded.secrets, pin) as access:
            repository = GitRepository(
                services.runner,
                services.locations,
                loaded.target,
                access,
                protocols=services.protocols,
            )
            exists = await repository.remote_has_branch()
    except GitSyncError as error:
        return _failed(error.problem, host)
    message = (
        "The repository answered and the branch exists."
        if exists
        else "The repository answered. The branch does not exist yet; the first push creates it."
    )
    return ConnectionCheck(CheckState.OK, message, host, branch_exists=exists)


async def check_connection(
    db: AsyncSession, services: GitServices, *, actor: AuditActor
) -> ConnectionCheck:
    """Test the saved repository without sending anything, and audit the outcome."""
    git = (await load_stored_settings(db)).git
    check = await _probe(db, services, git)
    await _audit_test(db, actor, check, git)
    return check


async def accept_host_key(
    db: AsyncSession,
    services: GitServices,
    *,
    actor: AuditActor,
    key_type: str,
    fingerprint: str,
) -> ConnectionCheck:
    """Pin the host key the superadmin was shown, then test the connection again.

    The key is found again by scanning the server now, and is pinned only when its type and
    fingerprint are the ones that were shown. Pinning never replaces a key that is already
    pinned: that takes a change of the repository address.
    """
    git = (await load_stored_settings(db)).git
    try:
        remote = parse_saved_address(services, git)
    except GitSyncError as error:
        return _failed(error.problem)
    if remote.kind is not RemoteKind.SSH:
        return _failed(GitProblem.NOT_CONFIGURED, remote.label)
    state = await load_state(db)
    current = pinned_key(state, remote)
    await db.commit()
    try:
        offered = await scan_host_keys(services.runner, services.locations, remote)
    except GitSyncError as error:
        return _failed(error.problem, remote.label)
    shown = next(
        (key for key in offered if key.key_type == key_type and key.fingerprint == fingerprint),
        None,
    )
    if shown is None:
        return ConnectionCheck(CheckState.FAILED, CHANGED_AFTER_SHOWN, remote.label)
    if current is not None and not current.same_key(shown):
        return ConnectionCheck(CheckState.FAILED, PINNED_ALREADY, remote.label)
    if current is None:
        state = await load_state(db)
        pin_host_key(state, shown, services.clock())
        await record_actor_audit(
            db,
            actor,
            action=ACTION_PIN,
            result=AuditResult.SUCCESS,
            target_type=TARGET_TYPE,
            target_name=remote.label,
            details={
                "host": remote.label,
                "key_type": shown.key_type,
                "fingerprint": shown.fingerprint,
            },
        )
        await db.commit()
    return await check_connection(db, services, actor=actor)
