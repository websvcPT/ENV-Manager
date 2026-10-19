"""Running git and the ssh tools as child processes, without a shell and without our secrets.

A child never inherits the environment of the app, which holds the master key and the CSRF
secret. Its whole environment is built from fixed values: a fixed PATH, a private home folder,
no git configuration from the system or a user, no terminal prompt, no agent, and only the
transports this feature uses. Every call has a deadline; at the deadline the whole process group
is killed. The output is read only to classify a failure and is never kept.
"""

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.git_errors import GitProblem, GitSyncError
from app.services.hook_process import BoundedOutput, run_process

CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
CHILD_LANGUAGE = "C"
OUTPUT_LIMIT_BYTES = 8192
REMOTE_PROTOCOLS = "ssh:https"
LOCAL_PROTOCOLS = "ssh:https:file"

# Deadlines in seconds. Contacting the server is bounded tighter than moving the data.
PROBE_TIMEOUT = 45.0
LOCAL_TIMEOUT = 60.0
FETCH_TIMEOUT = 300.0
PUSH_TIMEOUT = 600.0


@dataclass(frozen=True)
class GitOutput:
    """How a child ended. ``text`` is its merged output, for classification only."""

    exit_code: int | None
    timed_out: bool
    text: str

    @property
    def ok(self) -> bool:
        """Whether the command ended normally with exit code 0."""
        return not self.timed_out and self.exit_code == 0


class GitRunner(Protocol):
    """Runs one command (``git``, ``ssh-keyscan``) and returns its result."""

    async def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> GitOutput:
        """Run ``argv`` without a shell in ``cwd`` with exactly ``env``."""
        ...


class ProcessGitRunner:
    """The runner that starts real processes found on the fixed child PATH."""

    async def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> GitOutput:
        """Run the command with a deadline and bounded output.

        Raises ``GitSyncError`` when the program is missing or cannot start.
        """
        executable = shutil.which(argv[0], path=CHILD_PATH)
        if executable is None:
            raise GitSyncError(GitProblem.NOT_INSTALLED)
        output = BoundedOutput(OUTPUT_LIMIT_BYTES)
        try:
            result = await run_process(
                [executable, *argv[1:]], cwd=cwd, env=env, timeout=timeout, output=output
            )
        except OSError as error:
            raise GitSyncError(GitProblem.NOT_INSTALLED) from error
        return GitOutput(result.exit_code, result.timed_out, output.text())


def git_environment(home: Path, protocols: str = REMOTE_PROTOCOLS) -> dict[str, str]:
    """The complete environment of a git or ssh child, from fixed values only.

    ``protocols`` is git's own allow list of transports, so an address stored by other means
    than the settings form (a ``file`` or ``ext`` transport) is refused by git itself.
    """
    return {
        "PATH": CHILD_PATH,
        "HOME": str(home),
        "LANG": CHILD_LANGUAGE,
        "LC_ALL": CHILD_LANGUAGE,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ALLOW_PROTOCOL": protocols,
        "GIT_OPTIONAL_LOCKS": "0",
    }
