"""Reaching the docker CLI from the app: the Docker socket status and the child environment.

The post-deploy feature needs the Docker socket mounted into the container, a daemon that
answers through it and the compose plugin of the image's docker CLI. ``DockerCli.status`` reports
those three facts for the settings and the service screens and for the check made right before
a command runs. The cheap socket test (``socket_mounted``) never starts a process.

Child processes never inherit the environment of the app, which holds the master key, the CSRF
secret and the error reporting DSN. ``DockerCli.environment`` builds the whole environment of a
child from fixed values: a fixed PATH, a private scratch directory as HOME and Docker config
folder, a UTF-8 locale and ``DOCKER_HOST`` pinned to the socket that was checked.
"""

import asyncio
import os
import shutil
import stat
import tempfile
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.services.hook_process import BoundedOutput, run_process
from app.services.scratch_sweep import SCRATCH_PREFIX, claim_scratch

DOCKER_SOCKET = Path("/var/run/docker.sock")
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
CHILD_LANGUAGE = "C.UTF-8"
PROBE_TIMEOUT_SECONDS = 10.0
STATUS_TTL_SECONDS = 5.0

NOT_MOUNTED_REASON = "The Docker socket is not mounted."
CLI_MISSING_REASON = "The docker command is not installed in the app image."
DAEMON_UNREACHABLE_REASON = "The Docker daemon does not answer through the socket."
COMPOSE_MISSING_REASON = "Docker Compose is not available in the app image."


@dataclass(frozen=True)
class DockerStatus:
    """What the app can do with Docker right now.

    ``reason`` is fixed text that says why the feature is unavailable and is empty when it is
    available. ``compose_version`` has no leading ``v``.
    """

    mounted: bool
    reachable: bool
    compose_version: str | None
    reason: str | None = None

    @property
    def available(self) -> bool:
        """Whether post-deploy commands can run: socket, daemon and compose all work."""
        return self.mounted and self.reachable and self.compose_version is not None


class DockerCli:
    """The docker command of the app image and the socket it talks to.

    ``executable`` defaults to the ``docker`` found on the fixed child PATH. ``scratch`` is the
    private directory used as HOME and Docker config folder of children; it is created on first
    use inside the temporary directory unless given, and removed again when the object goes away.
    A scratch directory made here is locked by this object for as long as it lives, which is how
    ``app.services.scratch_sweep`` tells a folder in use from one a killed process left behind.
    ``clock`` is the monotonic time the status cache reads, replaceable in tests.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        socket_path: Path = DOCKER_SOCKET,
        scratch: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Remember where the docker command, the socket and the scratch directory are."""
        self._executable = executable
        self.socket_path = socket_path
        self._scratch = scratch
        self._scratch_claim: BinaryIO | None = None
        self._clock = clock
        self._probed: tuple[float, DockerStatus] | None = None

    @property
    def executable(self) -> str | None:
        """The absolute path of the docker command, None when it is not installed."""
        if self._executable is not None:
            return self._executable
        return shutil.which("docker", path=CHILD_PATH)

    @property
    def scratch(self) -> Path:
        """The private directory children use as HOME and Docker config folder."""
        if self._scratch is None:
            folder = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
            try:
                self._scratch_claim = claim_scratch(folder)
            except OSError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            self._scratch = folder
            weakref.finalize(self, shutil.rmtree, folder, ignore_errors=True)
        self._scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._scratch

    @property
    def socket_mounted(self) -> bool:
        """Whether the socket path exists and is a socket. Starts no process."""
        try:
            return stat.S_ISSOCK(os.stat(self.socket_path).st_mode)
        except OSError:
            return False

    def environment(self) -> dict[str, str]:
        """The complete environment of a child process, built from fixed values only."""
        scratch = str(self.scratch)
        return {
            "PATH": CHILD_PATH,
            "HOME": scratch,
            "LANG": CHILD_LANGUAGE,
            "DOCKER_CONFIG": scratch,
            "DOCKER_HOST": f"unix://{self.socket_path}",
        }

    def compose_environment(self) -> dict[str, str]:
        """The environment of a Compose command: the fixed one with remote sources (git, OCI) off,
        so Compose refuses them before any connection."""
        return {
            **self.environment(),
            "COMPOSE_EXPERIMENTAL_GIT_REMOTE": "false",
            "COMPOSE_EXPERIMENTAL_OCI_REMOTE": "false",
        }

    async def _probe(self, executable: str, *arguments: str) -> str | None:
        """The output of a short docker command, None when it fails, times out or is empty."""
        output = BoundedOutput()
        try:
            result = await run_process(
                [executable, *arguments],
                cwd=self.scratch,
                env=self.environment(),
                timeout=PROBE_TIMEOUT_SECONDS,
                output=output,
            )
        except OSError:
            return None
        text = output.text().strip()
        return text if result.exit_code == 0 and text else None

    async def status(self, *, fresh: bool = False) -> DockerStatus:
        """Check the socket, the daemon behind it and the compose plugin.

        The two probes start processes that can take up to ten seconds when the daemon hangs, so
        a screen that only shows the state gets the answer of the last probe when it is less than
        five seconds old. A check that decides whether something may run (saving a command,
        running one) passes ``fresh=True`` and always probes. The cheap tests of the socket and
        of the docker command are never cached.
        """
        if not self.socket_mounted:
            return DockerStatus(False, False, None, NOT_MOUNTED_REASON)
        executable = self.executable
        if executable is None:
            return DockerStatus(True, False, None, CLI_MISSING_REASON)
        if not fresh and self._probed is not None:
            probed_at, known = self._probed
            if self._clock() - probed_at < STATUS_TTL_SECONDS:
                return known
        found = await self._probe_status(executable)
        self._probed = (self._clock(), found)
        return found

    async def _probe_status(self, executable: str) -> DockerStatus:
        """Ask the daemon and the compose plugin, both at once."""
        server, compose = await asyncio.gather(
            self._probe(executable, "version", "--format", "{{.Server.Version}}"),
            self._probe(executable, "compose", "version", "--short"),
        )
        version = compose.splitlines()[0].lstrip("v") if compose else None
        if server is None:
            return DockerStatus(True, False, version, DAEMON_UNREACHABLE_REASON)
        if version is None:
            return DockerStatus(True, True, None, COMPOSE_MISSING_REASON)
        return DockerStatus(True, True, version)
