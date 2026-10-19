"""What every git operation shares: the collaborators and the reading of a saved repository.

``GitServices`` bundles the crypto service, the folders, the process runner and the clock, so the
push, the connection test and the tests that replace the runner or the clock all take one
object. ``load_target`` turns the saved settings into a parsed address and the credential its
transport needs, decrypted for the length of the operation only.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.models.types import utc_now
from app.services.app_settings import (
    GIT_DEPLOY_KEY_KEY,
    GIT_TOKEN_KEY,
    GitSettings,
    read_secret,
)
from app.services.git_auth import GitSecrets
from app.services.git_errors import GitProblem, GitSyncError
from app.services.git_locations import GitLocations
from app.services.git_process import REMOTE_PROTOCOLS, GitRunner, ProcessGitRunner
from app.services.git_remote import GitRemote, RemoteError, RemoteKind, parse_remote
from app.services.git_repo import RepoTarget
from app.services.notify_events import NO_EVENT_NOTIFIER, EventNotifier


@dataclass(frozen=True)
class GitServices:
    """The long-lived collaborators of git sync.

    ``protocols`` and ``allow_local`` open the local transports (a path or ``file://``) for
    tests of the service layer; the defaults allow ssh and https only. ``sleep`` and ``clock``
    let tests run the retries and the schedule without waiting. ``notifier`` is read each time
    an event is announced, so the application can swap its notifier after startup.
    """

    crypto: CryptoService
    locations: GitLocations
    database_path: Path
    runner: GitRunner = field(default_factory=ProcessGitRunner)
    protocols: str = REMOTE_PROTOCOLS
    allow_local: bool = False
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], datetime] = utc_now
    notifier: Callable[[], EventNotifier] = lambda: NO_EVENT_NOTIFIER


@dataclass(frozen=True)
class LoadedTarget:
    """A saved repository ready to use: where to push and the credential it needs."""

    target: RepoTarget
    secrets: GitSecrets


def parse_saved_address(services: GitServices, git: GitSettings) -> GitRemote:
    """The parsed saved address. Raises ``GitSyncError`` when none is saved or it is unusable."""
    if not git.configured:
        raise GitSyncError(GitProblem.NOT_CONFIGURED)
    try:
        return parse_remote(git.url, allow_local=services.allow_local)
    except RemoteError as error:
        raise GitSyncError(GitProblem.NOT_CONFIGURED) from error


async def load_secrets(db: AsyncSession, services: GitServices, remote: GitRemote) -> GitSecrets:
    """The credential the address needs, decrypted: the deploy key for ssh, the token for https.

    Raises ``GitSyncError`` when it is not stored or cannot be read with the current key.
    """
    if remote.kind is RemoteKind.FILE:
        return GitSecrets()
    ssh = remote.kind is RemoteKind.SSH
    try:
        secret = await read_secret(
            db, services.crypto, GIT_DEPLOY_KEY_KEY if ssh else GIT_TOKEN_KEY
        )
    except CryptoError as error:
        raise GitSyncError(GitProblem.UNREADABLE_CREDENTIAL) from error
    if secret is None:
        raise GitSyncError(GitProblem.NO_DEPLOY_KEY if ssh else GitProblem.NO_TOKEN)
    return GitSecrets(deploy_key=secret) if ssh else GitSecrets(token=secret)


async def load_target(db: AsyncSession, services: GitServices, git: GitSettings) -> LoadedTarget:
    """The saved repository and its credential. Raises ``GitSyncError`` when either is missing."""
    remote = parse_saved_address(services, git)
    secrets = await load_secrets(db, services, remote)
    return LoadedTarget(RepoTarget(remote, git.branch, git.author_name), secrets)
