"""Owning the scratch folder of the Docker CLI, and sweeping what a killed process left behind.

The Docker CLI of the app works inside a private scratch folder in the temporary directory, made
anew by each process. Every command run that carries registry logins makes a ``run-*`` folder in
it with a ``config.json`` and removes the folder when the run ends. A process that is killed hard
(SIGKILL, the out-of-memory killer) never gets to remove it, and a container that restarts keeps
its temporary directory, so the next process would find the file of the one before.

A process that makes a scratch folder takes an exclusive ``flock`` on a file inside it and holds
it for as long as the folder is in use. The kernel drops the lock when the process ends, however
it ended, and a restarted container cannot mistake a recycled process id for the old owner the
way it could with a pid file. ``sweep_stale_scratch`` removes the folders whose lock it can take,
which belong to no running process, so a folder in use by this process, another worker of the
test run or a command line deploy is never touched. A folder with no lock file is left for a
short grace period (it may be a moment after its creation) and removed after that.
"""

import fcntl
import os
import shutil
import stat
import time
from pathlib import Path
from typing import BinaryIO

SCRATCH_PREFIX = "envmanager-docker-"
RUN_FOLDER_PREFIX = "run-"
LOCK_FILE_NAME = ".lock"
LOCK_FILE_MODE = 0o600
LOCKLESS_GRACE_SECONDS = 60.0


def claim_scratch(folder: Path) -> BinaryIO:
    """Mark a new scratch folder as in use by this process.

    The returned file holds the lock; keep it open as long as the folder is used and let it go
    (or close it) to give the folder up.
    """
    descriptor = os.open(folder / LOCK_FILE_NAME, os.O_WRONLY | os.O_CREAT, LOCK_FILE_MODE)
    handle = os.fdopen(descriptor, "wb")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def _run_folders(folder: Path) -> int:
    """How many per-run folders a scratch folder holds."""
    try:
        return sum(1 for child in folder.iterdir() if child.name.startswith(RUN_FOLDER_PREFIX))
    except OSError:
        return 0


def _is_stale(folder: Path, modified: float, now: float) -> bool:
    """Whether no running process holds the scratch folder.

    The lock is taken for the length of the check only; the caller removes the folder right
    after, and nothing else claims a folder that was made by another process.
    """
    try:
        descriptor = os.open(folder / LOCK_FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return now - modified >= LOCKLESS_GRACE_SECONDS
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return True


def sweep_stale_scratch(
    temp_root: Path, *, user_id: int | None = None, now: float | None = None
) -> int:
    """Remove the scratch folders below ``temp_root`` that no running process uses.

    Only real folders owned by ``user_id`` (the current user by default) whose name starts with
    ``SCRATCH_PREFIX`` are considered; a link, a file or another user's folder is left alone.
    Returns how many per-run folders were removed with them. A folder that cannot be removed is
    skipped and tried again by the next start.
    """
    owner = os.getuid() if user_id is None else user_id
    moment = time.time() if now is None else now
    try:
        candidates = sorted(temp_root.glob(f"{SCRATCH_PREFIX}*"))
    except OSError:
        return 0
    removed = 0
    for folder in candidates:
        try:
            info = folder.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
            continue
        if not _is_stale(folder, info.st_mtime, moment):
            continue
        held = _run_folders(folder)
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed += held
    return removed
