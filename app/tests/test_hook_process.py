"""Bounded output and the child process runner: deadline, process group, environment, cancel."""

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from app.services.hook_process import (
    OUTPUT_PART_BYTES,
    BoundedOutput,
    CapturedProcess,
    ProcessResult,
    capture_process,
    run_process,
)

ENVIRONMENT = {"PATH": "/usr/local/bin:/usr/bin:/bin", "ONLY": "this"}


def process_is_gone(pid: int) -> bool:
    """Whether a process no longer runs: absent, or a zombie its parent has not reaped yet.

    In a container without an init process a killed grandchild stays a zombie of whatever
    adopted it; it is dead all the same.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return True
    return fields[0] == "Z"


async def wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    """Poll until the process is gone or the time is up."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process_is_gone(pid):
            return True
        await asyncio.sleep(0.05)
    return process_is_gone(pid)


async def run_shell(
    script: str, tmp_path: Path, *, timeout: float = 20.0, output: BoundedOutput | None = None
) -> tuple[ProcessResult, BoundedOutput]:
    """Run a shell script through the runner in the temporary folder."""
    collected = output or BoundedOutput()
    result = await run_process(
        ["/bin/sh", "-c", script], cwd=tmp_path, env=ENVIRONMENT, timeout=timeout, output=collected
    )
    return result, collected


def test_output_that_fits_is_kept_whole() -> None:
    output = BoundedOutput()
    output.add(b"first\n")
    output.add(b"second\n")
    assert output.text() == "first\nsecond\n"


def test_long_output_keeps_the_first_and_last_four_kilobytes_with_a_marker() -> None:
    output = BoundedOutput()
    head = b"H" * OUTPUT_PART_BYTES
    middle = b"M" * 30000
    tail = b"T" * OUTPUT_PART_BYTES
    for part in (head, middle, tail):
        for start in range(0, len(part), 5000):
            output.add(part[start : start + 5000])
    text = output.text()
    assert text.startswith("H" * OUTPUT_PART_BYTES)
    assert text.endswith("T" * OUTPUT_PART_BYTES)
    assert "M" not in text.replace("[... 30000 bytes omitted ...]", "")
    assert "[... 30000 bytes omitted ...]" in text
    assert len(text) < 2 * OUTPUT_PART_BYTES + 100


def test_output_of_exactly_the_limit_has_no_marker() -> None:
    output = BoundedOutput()
    output.add(b"a" * (2 * OUTPUT_PART_BYTES))
    assert "omitted" not in output.text() and len(output.text()) == 2 * OUTPUT_PART_BYTES


def test_notes_start_on_a_fresh_line_and_bytes_that_are_not_text_do_not_fail() -> None:
    output = BoundedOutput()
    output.note("[first]\n")
    output.add(b"no newline")
    output.note("[second]\n")
    output.add(b"\xff\x00ok\n")
    text = output.text()
    assert text.startswith("[first]\nno newline\n[second]\n")
    assert "\x00" not in text and text.endswith("ok\n")


async def test_the_exit_code_and_merged_output_are_returned(tmp_path: Path) -> None:
    result, output = await run_shell("echo out; echo err >&2; exit 7", tmp_path)
    assert (result.exit_code, result.timed_out) == (7, False)
    assert sorted(output.text().split()) == ["err", "out"]


async def test_the_child_gets_exactly_the_given_environment_and_directory(tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    output = BoundedOutput()
    await run_process(
        ["/bin/sh", "-c", "env; pwd"],
        cwd=folder,
        env={"PATH": "/usr/bin:/bin", "ONLY": "this"},
        timeout=10,
        output=output,
    )
    lines = output.text().splitlines()
    assert "ONLY=this" in lines and str(folder) in lines
    assert not any(line.startswith(("HOME=", "ENVMANAGER_")) for line in lines)


async def test_the_child_has_no_standard_input(tmp_path: Path) -> None:
    result, output = await run_shell("cat; echo done", tmp_path, timeout=5)
    assert result.exit_code == 0 and output.text() == "done\n"


async def test_a_program_that_does_not_exist_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await run_process(
            [str(tmp_path / "missing")], cwd=tmp_path, env={}, timeout=5, output=BoundedOutput()
        )


async def test_a_flood_of_output_is_bounded_while_it_is_read(tmp_path: Path) -> None:
    result, output = await run_shell("yes abcdefghij | head -c 3000000; exit 4", tmp_path)
    assert result.exit_code == 4
    assert len(output.text()) < 2 * OUTPUT_PART_BYTES + 100
    assert "bytes omitted" in output.text()


async def test_the_deadline_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    result, _ = await run_shell(
        f"sleep 300 & echo $! > {pid_file}; echo started; wait", tmp_path, timeout=1.0
    )
    assert (result.exit_code, result.timed_out) == (None, True)
    assert time.monotonic() - started < 10
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_output_written_before_the_deadline_is_kept(tmp_path: Path) -> None:
    result, output = await run_shell("echo before; exec sleep 300", tmp_path, timeout=1.0)
    assert result.timed_out and output.text() == "before\n"


async def test_a_background_child_holding_the_pipe_does_not_hang_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.hook_process.DRAIN_GRACE_SECONDS", 0.5)
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    result, output = await run_shell(f"sleep 30 & echo $! > {pid_file}; echo done", tmp_path)
    assert result.exit_code == 0 and output.text() == "done\n"
    assert time.monotonic() - started < 10
    child = int(pid_file.read_text(encoding="utf-8"))
    assert not process_is_gone(child)

    os.kill(child, signal.SIGKILL)


async def test_cancelling_the_caller_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    task = asyncio.create_task(
        run_shell(f"sleep 300 & echo $! > {pid_file}; wait", tmp_path, timeout=60)
    )
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.05)
    child = int(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_until_gone(child)


async def capture_shell(
    script: str,
    tmp_path: Path,
    *,
    timeout: float = 20.0,
    stdout_limit: int = 1000,
    stderr_limit: int = 100,
) -> CapturedProcess:
    """Run a shell script through the capturing runner in the temporary folder."""
    return await capture_process(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=ENVIRONMENT,
        timeout=timeout,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )


async def test_a_captured_process_keeps_standard_output_and_error_apart(tmp_path: Path) -> None:
    result = await capture_shell("echo warn >&2; echo '{}'; exit 3", tmp_path)
    assert (result.exit_code, result.stdout, result.stderr) == (3, b"{}\n", b"warn\n")
    assert not result.timed_out and not result.too_large


async def test_a_captured_process_has_no_standard_input(tmp_path: Path) -> None:
    result = await capture_shell("cat; echo done", tmp_path, timeout=5)
    assert result.exit_code == 0 and result.stdout == b"done\n"


async def test_captured_output_of_exactly_the_limit_is_whole(tmp_path: Path) -> None:
    result = await capture_shell("head -c 1000 /dev/zero | tr '\\0' x", tmp_path)
    assert result.exit_code == 0 and result.stdout == b"x" * 1000 and not result.too_large


async def test_captured_output_past_the_limit_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    result = await capture_shell(
        f"sleep 300 & echo $! > {pid_file}; head -c 1001 /dev/zero | tr '\\0' x; wait", tmp_path
    )
    assert result.too_large and not result.timed_out
    assert len(result.stdout) == 1000
    assert time.monotonic() - started < 10
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_captured_standard_error_keeps_only_its_first_bytes(tmp_path: Path) -> None:
    result = await capture_shell("yes e | head -c 100000 >&2; echo ok", tmp_path)
    assert result.exit_code == 0 and result.stdout == b"ok\n"
    assert result.stderr == (b"e\n" * 50)


async def test_the_deadline_kills_a_captured_process_group_and_keeps_what_was_read(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    result = await capture_shell(
        f"sleep 300 & echo $! > {pid_file}; echo before; wait", tmp_path, timeout=1.0
    )
    assert (result.exit_code, result.timed_out) == (None, True)
    assert result.stdout == b"before\n"
    assert time.monotonic() - started < 10
    assert await wait_until_gone(int(pid_file.read_text(encoding="utf-8")))


async def test_cancelling_a_captured_process_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    task = asyncio.create_task(
        capture_shell(f"sleep 300 & echo $! > {pid_file}; wait", tmp_path, timeout=60)
    )
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.05)
    child = int(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_until_gone(child)


async def test_a_captured_program_that_does_not_exist_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await capture_process(
            [str(tmp_path / "missing")],
            cwd=tmp_path,
            env={},
            timeout=5,
            stdout_limit=10,
            stderr_limit=10,
        )
