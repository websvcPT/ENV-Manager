"""Why a git call failed, as fixed phrases.

The text a git or ssh command prints can name paths and hosts and, with some servers, more than
that, so it is read only to pick one of the problems below and is never stored, logged or shown.
Every problem has one fixed sentence and says whether trying again later can help (a network
fault) or cannot (a refused credential, a changed host key).
"""

from dataclasses import dataclass
from enum import StrEnum


class GitProblem(StrEnum):
    """What went wrong with a git operation."""

    NOT_INSTALLED = "not_installed"
    NOT_CONFIGURED = "not_configured"
    NO_DEPLOY_KEY = "no_deploy_key"
    NO_TOKEN = "no_token"  # noqa: S105
    UNREADABLE_CREDENTIAL = "unreadable_credential"
    NOT_PINNED = "not_pinned"
    HOST_KEY_CHANGED = "host_key_changed"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    STALE = "stale"
    NETWORK = "network"
    TIMEOUT = "timeout"
    WORKDIR = "workdir"
    EXPORT = "export"
    FAILED = "failed"


@dataclass(frozen=True)
class ProblemInfo:
    """The sentence of a problem and whether another attempt can succeed."""

    message: str
    transient: bool


PROBLEMS: dict[GitProblem, ProblemInfo] = {
    GitProblem.NOT_INSTALLED: ProblemInfo(
        "Git or the ssh client is not installed in the app image.", False
    ),
    GitProblem.NOT_CONFIGURED: ProblemInfo("No usable repository address is saved.", False),
    GitProblem.NO_DEPLOY_KEY: ProblemInfo("No deploy key is stored for this ssh address.", False),
    GitProblem.NO_TOKEN: ProblemInfo("No access token is stored for this https address.", False),
    GitProblem.UNREADABLE_CREDENTIAL: ProblemInfo(
        "The stored credential cannot be read with the current key.", False
    ),
    GitProblem.NOT_PINNED: ProblemInfo(
        "The server's host key is not pinned yet. Test the connection and trust the key.", False
    ),
    GitProblem.HOST_KEY_CHANGED: ProblemInfo(
        "The server's host key does not match the pinned key. Nothing was sent.", False
    ),
    GitProblem.AUTH: ProblemInfo("The repository refused the credentials.", False),
    GitProblem.NOT_FOUND: ProblemInfo(
        "The repository was not found, or the credentials cannot see it.", False
    ),
    GitProblem.REJECTED: ProblemInfo(
        "The repository refused the push, for example a protected branch.", False
    ),
    GitProblem.STALE: ProblemInfo("The repository changed while the push was running.", True),
    GitProblem.NETWORK: ProblemInfo("The repository could not be reached.", True),
    GitProblem.TIMEOUT: ProblemInfo("The git command ran out of time.", True),
    GitProblem.WORKDIR: ProblemInfo("The working folder for git cannot be used.", False),
    GitProblem.EXPORT: ProblemInfo("The export could not be written.", False),
    GitProblem.FAILED: ProblemInfo("The git command failed.", True),
}

# Phrases of git, ssh and the servers in front of them, checked in this order: a changed host
# key first (the connection is refused before anything else is said), then credentials.
_PATTERNS: tuple[tuple[GitProblem, tuple[str, ...]], ...] = (
    (
        GitProblem.HOST_KEY_CHANGED,
        (
            "remote host identification has changed",
            "host key verification failed",
            "no matching host key type",
            "has changed and you have requested strict checking",
        ),
    ),
    (
        GitProblem.AUTH,
        (
            "permission denied (publickey",
            "authentication failed",
            "invalid username or password",
            "could not read username",
            "could not read password",
            "terminal prompts disabled",
            "access denied",
            "returned error: 401",
            "returned error: 403",
            "invalid credentials",
            "load key",
            "incorrect passphrase",
        ),
    ),
    (
        GitProblem.NOT_FOUND,
        (
            "repository not found",
            "returned error: 404",
            "does not appear to be a git repository",
            "not found",
        ),
    ),
    (
        GitProblem.STALE,
        ("non-fast-forward", "fetch first", "stale info", "tip of your current branch is behind"),
    ),
    (
        GitProblem.REJECTED,
        (
            "remote rejected",
            "[rejected]",
            "protected branch",
            "declined",
            "failed to push some refs",
        ),
    ),
    (
        GitProblem.NETWORK,
        (
            "could not resolve host",
            "connection refused",
            "connection timed out",
            "operation timed out",
            "network is unreachable",
            "no route to host",
            "connection reset",
            "connection closed",
            "unable to access",
            "ssh: connect to host",
            "kex_exchange_identification",
            "could not read from remote repository",
            "early eof",
        ),
    ),
)


class GitSyncError(Exception):
    """A git operation failed. The message is the problem's fixed sentence, nothing else."""

    def __init__(self, problem: GitProblem) -> None:
        """Hold the problem and its sentence."""
        info = PROBLEMS[problem]
        super().__init__(info.message)
        self.problem = problem
        self.message = info.message
        self.transient = info.transient


def classify(output: str, *, timed_out: bool = False) -> GitProblem:
    """The problem that git or ssh output describes.

    A command that ran out of time is a timeout whatever it printed. Output that matches no
    known phrase is a plain failure.
    """
    if timed_out:
        return GitProblem.TIMEOUT
    text = output.lower()
    for problem, phrases in _PATTERNS:
        if any(phrase in text for phrase in phrases):
            return problem
    return GitProblem.FAILED
