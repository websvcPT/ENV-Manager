"""The deploy lock: at most one deploy at a time per real deployment root.

Two deployments may share a root path, directly or through a symlink, so the lock is keyed on the
resolved real root, not on the deployment. It is an exclusive ``flock`` on a file in the lock
folder, which also holds between processes: the CLI runs as a separate process next to the web
server. The lock is never waited for; a second deploy is refused at once. It is released when the
descriptor closes, so a crash cannot leave it held.
"""

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.services.paths import PathValidationError, resolve_real_path


class DeployLockBusyError(Exception):
    """Another deploy holds the lock of this deployment root."""


class DeployLockUnavailableError(Exception):
    """The lock file cannot be created or opened."""


def lock_key(deployment_root: str) -> str:
    """The key of a deployment root: a hash of its real path, or of the stored text if it breaks."""
    try:
        real = str(resolve_real_path(deployment_root))
    except PathValidationError:
        real = deployment_root
    return hashlib.sha256(os.fsencode(real)).hexdigest()


@contextmanager
def hold_deploy_lock(lock_dir: Path, deployment_root: str) -> Iterator[None]:
    """Hold the lock of a deployment root for the body of the ``with`` block.

    Raises ``DeployLockBusyError`` at once when another holder has it and
    ``DeployLockUnavailableError`` when the lock file cannot be used.
    """
    try:
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            lock_dir / f"{lock_key(deployment_root)}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as error:
        raise DeployLockUnavailableError from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeployLockBusyError from error
        except OSError as error:
            raise DeployLockUnavailableError from error
        yield
    finally:
        os.close(descriptor)
