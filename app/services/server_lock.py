"""The server lock: one process at a time serves, or rewrites, a data folder.

The running server holds an exclusive ``flock`` on a file in the data folder from its start to its
end, and the key rotation takes the same lock for as long as it runs, so a server cannot start in
the middle of a rotation and a rotation cannot start under a server. It holds between containers
that mount the same data folder, because the kernel owns the lock, and it is never waited for.

The lock disappears with the process that holds it, so a crash cannot leave it held. A holder
writes a line about itself into the file and empties the file when it lets go; text found in the
file by the next holder therefore means the previous one did not end cleanly (a stale lock), which
is reported and does not stop anything. The line is only for people to read: process ids repeat
between containers, so it is never used to decide whether the lock is held.
"""

import fcntl
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.log import get_logger

log = get_logger("server_lock")

HOLDER_LINE_BYTES = 512
STALE_FILE_MODE = 0o600


class ServerLockBusyError(Exception):
    """Another process holds the server lock of this data folder."""

    def __init__(self, holder: str | None) -> None:
        """Keep the line the holder wrote about itself, if it wrote one."""
        super().__init__("another process is using this data folder")
        self.holder = holder


class ServerLockUnavailableError(Exception):
    """The lock file cannot be created or opened."""


@dataclass
class ServerLock:
    """A held lock. ``previous`` is what a holder that never let go left in the file."""

    descriptor: int
    previous: str | None

    def release(self) -> None:
        """Empty the file (a clean end) and give the lock up. Safe to call twice."""
        if self.descriptor < 0:
            return
        try:
            os.ftruncate(self.descriptor, 0)
        except OSError as error:
            log.warning("the server lock file could not be emptied: %s", type(error).__name__)
        finally:
            os.close(self.descriptor)
            self.descriptor = -1


def _read_holder(descriptor: int) -> str | None:
    """The line the holder wrote, or None when the file is empty."""
    text = os.pread(descriptor, HOLDER_LINE_BYTES, 0).decode("utf-8", errors="replace").strip()
    return text or None


def _holder_line(role: str) -> bytes:
    """A line saying who holds the lock: role, process id, host name and start time."""
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{role} pid={os.getpid()} host={socket.gethostname()} since={started}\n"
    return line.encode("utf-8")[:HOLDER_LINE_BYTES]


def acquire_server_lock(path: Path, role: str) -> ServerLock:
    """Take the lock of the data folder for the rest of the process or until ``release``.

    ``role`` is a few words for the line in the file ("server", "key rotation"). Raises
    ``ServerLockBusyError`` at once when another holder has the lock and
    ``ServerLockUnavailableError`` when the file cannot be used.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, STALE_FILE_MODE
        )
    except OSError as error:
        raise ServerLockUnavailableError from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        holder = _read_holder(descriptor)
        os.close(descriptor)
        raise ServerLockBusyError(holder) from error
    except OSError as error:
        os.close(descriptor)
        raise ServerLockUnavailableError from error
    previous = _read_holder(descriptor)
    if previous is not None:
        log.warning("the previous holder of the data folder did not end cleanly: %s", previous)
    os.ftruncate(descriptor, 0)
    os.pwrite(descriptor, _holder_line(role), 0)
    return ServerLock(descriptor, previous)
