"""A fake ``docker`` executable for tests: a real script, so process handling is real.

The script answers the two probes the app makes (``docker version --format ...`` for the daemon
and ``docker compose version --short`` for the plugin) and records every other call in its
folder: the arguments, the working directory and the whole environment it was started with. What
a recorded call does comes from ``behavior.sh`` in the same folder, which a test replaces with a
shell snippet (it can exit non-zero, print a lot, sleep or start a background child). The call
number is available to the snippet as ``$n``. The "socket" is a real socket file, so the mounted
check is the real one.
"""

import json
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

from app.services.docker_cli import DockerCli

SCRIPT = """#!/bin/sh
dir=$(dirname "$0")
case "$1 $2" in
  "version --format")
    echo probe >> "$dir/probes"
    [ -f "$dir/no-daemon" ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    [ -f "$dir/slow-daemon" ] && exec sleep 300
    echo 29.9.1
    exit 0 ;;
  "compose version")
    [ -f "$dir/no-compose" ] && { echo "unknown command" >&2; exit 1; }
    echo v2.40.0
    exit 0 ;;
esac
mkdir -p "$dir/calls"
n=$(ls "$dir/calls" | wc -l)
n=$((n / 3 + 1))
printf '%s\\0' "$@" > "$dir/calls/$n.argv"
pwd > "$dir/calls/$n.cwd"
env > "$dir/calls/$n.env"
. "$dir/behavior.sh"
"""


CAPTURE_CONFIG = """mkdir -p "$dir/configs"
cp "$DOCKER_CONFIG/config.json" "$dir/configs/$n.json"
stat -c %a "$DOCKER_CONFIG/config.json" > "$dir/configs/$n.file-mode"
stat -c %a "$DOCKER_CONFIG" > "$dir/configs/$n.folder-mode"
"""


@dataclass(frozen=True)
class Call:
    """One recorded call of the fake docker."""

    arguments: tuple[str, ...]
    working_directory: str
    environment: dict[str, str]


@dataclass(frozen=True)
class CapturedConfig:
    """The ``config.json`` a call found in its Docker config folder, copied while it ran.

    ``file_mode`` and ``folder_mode`` are the permission bits of the file and of the folder as the
    call saw them, which stay readable after the run removed both.
    """

    content: dict[str, dict[str, dict[str, str]]]
    file_mode: int
    folder_mode: int


class FakeDocker:
    """The fake executable, its socket file and the records of what was run through it."""

    def __init__(self, folder: Path) -> None:
        """Create the script and its default behavior (print the arguments, exit 0)."""
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.executable = folder / "docker"
        self.executable.write_text(SCRIPT, encoding="utf-8")
        self.executable.chmod(0o755)
        self.socket_path = folder / "docker.sock"
        self.behavior('echo "ran $*"')

    def mount_socket(self) -> None:
        """Create the socket file the app looks for, unless it exists already."""
        if self.socket_path.exists():
            return
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
        finally:
            listener.close()
        assert stat.S_ISSOCK(os.stat(self.socket_path).st_mode)

    def behavior(self, script: str) -> None:
        """Replace what recorded calls do with a shell snippet."""
        (self.folder / "behavior.sh").write_text(script + "\n", encoding="utf-8")

    def capture_config(self, then: str = "") -> None:
        """Make recorded calls copy ``$DOCKER_CONFIG/config.json`` and the modes beside the calls.

        ``then`` is what the call does afterwards (nothing by default). The copies live outside
        the ``calls`` folder, whose entries are counted to number the calls.
        """
        self.behavior(CAPTURE_CONFIG + then)

    def captured_config(self, number: int = 1) -> CapturedConfig | None:
        """What call ``number`` (from 1) found in its config folder, None when it held no file."""
        records = self.folder / "configs"
        copy = records / f"{number}.json"
        if not copy.exists():
            return None
        return CapturedConfig(
            content=json.loads(copy.read_text(encoding="utf-8")),
            file_mode=int((records / f"{number}.file-mode").read_text("utf-8").strip(), 8),
            folder_mode=int((records / f"{number}.folder-mode").read_text("utf-8").strip(), 8),
        )

    def switch(self, name: str, on: bool = True) -> None:
        """Turn a failure mode on or off: ``no-daemon``, ``no-compose`` or ``slow-daemon``."""
        marker = self.folder / name
        if on:
            marker.write_text("", encoding="utf-8")
        else:
            marker.unlink(missing_ok=True)

    def cli(self, *, mounted: bool = True) -> DockerCli:
        """A ``DockerCli`` on the fake executable; the socket is created unless told not to."""
        if mounted:
            self.mount_socket()
        return DockerCli(
            executable=str(self.executable),
            socket_path=self.socket_path,
            scratch=self.folder / "scratch",
        )

    def probes(self) -> int:
        """How many times the app asked the daemon for its version, which is once per status
        check that really probes (the compose probe always comes with it)."""
        record = self.folder / "probes"
        return len(record.read_text(encoding="utf-8").splitlines()) if record.exists() else 0

    def calls(self) -> list[Call]:
        """The recorded calls in order, probes excluded."""
        records = self.folder / "calls"
        if not records.is_dir():
            return []
        numbers = sorted(int(path.stem) for path in records.glob("*.argv"))
        found: list[Call] = []
        for number in numbers:
            raw = (records / f"{number}.argv").read_bytes().split(b"\0")[:-1]
            environment = dict(
                line.split("=", 1)
                for line in (records / f"{number}.env").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            found.append(
                Call(
                    arguments=tuple(part.decode("utf-8") for part in raw),
                    working_directory=(records / f"{number}.cwd").read_text("utf-8").strip(),
                    environment=environment,
                )
            )
        return found
