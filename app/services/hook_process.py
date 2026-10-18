"""Running one child process with a deadline, a process group and bounded output.

The child starts in its own session, so everything it spawns (the docker CLI starts the compose
plugin as a child) shares one process group that is killed as a whole on a timeout or when the
caller is cancelled. Its standard output and error are merged and read as they arrive; only the
first and the last ``OUTPUT_PART_BYTES`` bytes are kept, so a command that prints a lot never
fills the memory of the app. The child has no standard input.
"""

import asyncio
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.services.output_scrub import OutputScrubber

OUTPUT_PART_BYTES = 4096
READ_CHUNK_BYTES = 65536
DRAIN_GRACE_SECONDS = 2.0
REAP_SECONDS = 5.0


class BoundedOutput:
    """The first and the last ``limit`` bytes of a stream, with a marker for what lies between."""

    def __init__(self, limit: int = OUTPUT_PART_BYTES) -> None:
        """Keep at most ``limit`` bytes from the start and ``limit`` bytes from the end."""
        self._limit = limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._last = b""

    def add(self, chunk: bytes) -> None:
        """Take the next bytes of the stream."""
        if not chunk:
            return
        self._total += len(chunk)
        self._last = chunk[-1:]
        room = self._limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self._tail += chunk
            if len(self._tail) > self._limit:
                del self._tail[: len(self._tail) - self._limit]

    def note(self, text: str) -> None:
        """Add a line of our own, started on a fresh line."""
        prefix = "\n" if self._last not in {b"", b"\n"} else ""
        self.add((prefix + text).encode("utf-8"))

    def text(self, scrubber: OutputScrubber | None = None) -> str:
        """The kept output as text, with a marker where bytes were left out.

        With a ``scrubber`` the known values are replaced before the bytes are decoded. When
        output was left out, the two kept parts are scrubbed on their own and a value that the
        cut split between a kept part and the omitted bytes is replaced as well.
        """
        omitted = self._total - len(self._head) - len(self._tail)
        head, tail = bytes(self._head), bytes(self._tail)
        if omitted:
            if scrubber is not None:
                head, tail = scrubber.scrub_head(head), scrubber.scrub_tail(tail)
            marker = f"\n[... {omitted} bytes omitted ...]\n".encode()
            data = head + marker + tail
        else:
            data = head + tail
            if scrubber is not None:
                data = scrubber.scrub_bytes(data)
        return data.decode("utf-8", errors="replace").replace("\x00", "")


@dataclass(frozen=True)
class ProcessResult:
    """How a child ended: its exit code, or ``timed_out`` when it was killed at the deadline."""

    exit_code: int | None
    timed_out: bool = False


def kill_group(process: asyncio.subprocess.Process) -> None:
    """Kill the process group of a child that was started in its own session.

    Only called while the child is not yet reaped, so the group id still belongs to it.
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


async def _pump(stream: asyncio.StreamReader, output: BoundedOutput) -> None:
    """Read the stream to its end into the bounded output."""
    while chunk := await stream.read(READ_CHUNK_BYTES):
        output.add(chunk)


async def _finish_output(reader: asyncio.Task[None]) -> None:
    """Wait briefly for the rest of the output; a grandchild holding the pipe is not waited for."""
    with suppress(TimeoutError):
        await asyncio.wait_for(reader, DRAIN_GRACE_SECONDS)


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    output: BoundedOutput,
) -> ProcessResult:
    """Run ``argv`` without a shell in ``cwd`` with exactly ``env`` and read its output.

    At the deadline the whole process group is killed and the result says it timed out. If the
    calling task is cancelled the group is killed as well, so nothing is left running after the
    lock that protected it is released. Raises ``OSError`` when the program cannot be started.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stdout = cast(asyncio.StreamReader, process.stdout)  # a PIPE was requested
    reader = asyncio.create_task(_pump(stdout, output))
    try:
        try:
            async with asyncio.timeout(timeout):
                exit_code = await process.wait()
        except TimeoutError:
            kill_group(process)
            await process.wait()
            await _finish_output(reader)
            return ProcessResult(None, timed_out=True)
        await _finish_output(reader)
        return ProcessResult(exit_code)
    finally:
        if process.returncode is None:
            kill_group(process)
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), REAP_SECONDS)
        reader.cancel()
