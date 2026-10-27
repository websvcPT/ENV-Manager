"""Reading from Docker: one read-only command, its standard output as text.

Every read of the runtime (listing Compose projects, labels, containers, networks) goes through
``run_docker_query``. The docker command is the absolute one of ``DockerCli`` and runs without a
shell in the private scratch folder, with the fixed environment of ``DockerCli`` (Compose
commands also get the remote sources off), no standard input and a process group of its own that
is killed at the deadline and when the caller is cancelled.

Standard output and error stay apart, so a Compose ``WARN`` line never lands in a JSON document.
Standard output is kept whole: past the size limit the command is killed and the call fails,
nothing is truncated. Standard error is only read to tell a daemon that does not answer from any
other failure; it is never returned, logged or shown. A failure is one ``DockerQueryError`` with
a fixed sentence that is safe to show.

The caller passes only read-only arguments. A command that changes anything does not belong here.
"""

from collections.abc import Sequence
from enum import StrEnum

from app.core.log import get_logger
from app.services.docker_cli import CLI_MISSING_REASON, DAEMON_UNREACHABLE_REASON, DockerCli
from app.services.hook_process import capture_process

log = get_logger("docker_query")

QUERY_TIMEOUT_SECONDS = 15.0
MAX_QUERY_BYTES = 4 * 1024 * 1024
STDERR_BYTES = 4096

TIMED_OUT_MESSAGE = "Docker did not answer in time."
TOO_LARGE_MESSAGE = "Docker gave more output than can be read."
FAILED_MESSAGE = "The docker command failed."

_DAEMON_MARKERS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "docker.sock: connect",
)


class QueryFailure(StrEnum):
    """Why a query failed."""

    MISSING = "missing"
    DAEMON = "daemon"
    TIMED_OUT = "timed_out"
    TOO_LARGE = "too_large"
    FAILED = "failed"


class DockerQueryError(Exception):
    """A docker query failed; the message is fixed text that is safe to show."""

    def __init__(self, failure: QueryFailure, message: str) -> None:
        """Keep why it failed and the sentence to show."""
        super().__init__(message)
        self.failure = failure
        self.message = message


def _daemon_unreachable(stderr: bytes) -> bool:
    """Whether the error output says the daemon could not be reached."""
    text = stderr.decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _DAEMON_MARKERS)


async def run_docker_query(
    cli: DockerCli,
    arguments: Sequence[str],
    *,
    timeout: float = QUERY_TIMEOUT_SECONDS,
    max_bytes: int = MAX_QUERY_BYTES,
) -> str:
    """Run ``docker <arguments>`` and return its standard output as text.

    Raises ``DockerQueryError`` when the docker command is missing or cannot start, the daemon
    does not answer, the deadline passes, the output is larger than ``max_bytes`` or the command
    exits with a non-zero code.
    """
    executable = cli.executable
    if executable is None:
        raise DockerQueryError(QueryFailure.MISSING, CLI_MISSING_REASON)
    is_compose = bool(arguments) and arguments[0] == "compose"
    try:
        captured = await capture_process(
            [executable, *arguments],
            cwd=cli.scratch,
            env=cli.compose_environment() if is_compose else cli.environment(),
            timeout=timeout,
            stdout_limit=max_bytes,
            stderr_limit=STDERR_BYTES,
        )
    except OSError as error:
        raise DockerQueryError(QueryFailure.MISSING, CLI_MISSING_REASON) from error
    if captured.timed_out:
        raise DockerQueryError(QueryFailure.TIMED_OUT, TIMED_OUT_MESSAGE)
    if captured.too_large:
        raise DockerQueryError(QueryFailure.TOO_LARGE, TOO_LARGE_MESSAGE)
    if captured.exit_code != 0:
        subcommand = arguments[0] if arguments else ""
        log.warning("docker %s exited with code %s", subcommand, captured.exit_code)
        if _daemon_unreachable(captured.stderr):
            raise DockerQueryError(QueryFailure.DAEMON, DAEMON_UNREACHABLE_REASON)
        raise DockerQueryError(QueryFailure.FAILED, FAILED_MESSAGE)
    return captured.stdout.decode("utf-8", errors="replace")
