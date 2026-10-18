"""The backup lock: at most one backup, prune or restore at a time.

It is an exclusive ``flock`` on a file in the lock folder, so it holds between processes: the
command line runs next to the web server, and a second worker of the web server would take the
same lock. It is never waited for; a second operation is refused at once and the scheduled job
simply skips its turn. It is released when the descriptor closes, so a crash cannot leave it held.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.services.backup import BackupProblem, BackupSnapshotError

LOCK_NAME = "backup.lock"


class BackupBusyError(Exception):
    """Another backup or restore holds the lock."""


@contextmanager
def hold_backup_lock(lock_dir: Path) -> Iterator[None]:
    """Hold the backup lock for the body of the ``with`` block.

    Raises ``BackupBusyError`` at once when another holder has it and ``BackupSnapshotError``
    when the lock file cannot be used.
    """
    try:
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            lock_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except OSError as error:
        raise BackupSnapshotError(BackupProblem.FOLDER_UNUSABLE) from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupBusyError from error
        except OSError as error:
            raise BackupSnapshotError(BackupProblem.FOLDER_UNUSABLE) from error
        yield
    finally:
        os.close(descriptor)
