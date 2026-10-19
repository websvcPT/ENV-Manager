"""The git operations of a push: look at the remote, prepare the working clone, commit, push.

The working clone is only a cache. Before every push it is made to match the remote branch (or
started empty when the branch does not exist yet), the export is written on top of it, and the
result is committed and pushed without force. Every call is a ``git`` argument list with a
deadline, run by a ``GitRunner`` with an environment of fixed values; the address, credentials
and the remote configuration never hold a secret. A failed call raises ``GitSyncError`` with one
of the fixed problems, never the text git printed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.services.git_auth import GitAccess
from app.services.git_errors import GitProblem, GitSyncError, classify
from app.services.git_locations import GitLocations
from app.services.git_process import (
    FETCH_TIMEOUT,
    LOCAL_TIMEOUT,
    PROBE_TIMEOUT,
    PUSH_TIMEOUT,
    REMOTE_PROTOCOLS,
    GitOutput,
    GitRunner,
    git_environment,
)
from app.services.git_remote import GitRemote

COMMIT_EMAIL = "env-manager@localhost"
DEFAULT_AUTHOR = "ENV Manager"
NO_MATCHING_REFS_EXIT = 2
LOW_SPEED_LIMIT = "1000"
LOW_SPEED_SECONDS = "60"
FIXED_OPTIONS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "gc.autoDetach=false",
    "-c",
    f"http.lowSpeedLimit={LOW_SPEED_LIMIT}",
    "-c",
    f"http.lowSpeedTime={LOW_SPEED_SECONDS}",
)


@dataclass(frozen=True)
class RepoTarget:
    """Where to push: the parsed address, the branch and the name commits carry."""

    remote: GitRemote
    branch: str
    author_name: str


def commit_author(name: str) -> str:
    """The author name for a commit: the setting, or the default when it cannot be used.

    Git strips angle brackets and refuses a name made only of them; a line break would split
    the header. The form refuses both, so this only guards a value stored by other means.
    """
    cleaned = "".join(char for char in name if char.isprintable() and char not in "<>").strip()
    return cleaned[:100] or DEFAULT_AUTHOR


class GitRepository:
    """The git calls for one push or one connection test, with one access for all of them."""

    def __init__(
        self,
        runner: GitRunner,
        locations: GitLocations,
        target: RepoTarget,
        access: GitAccess,
        *,
        protocols: str = REMOTE_PROTOCOLS,
    ) -> None:
        """Hold what every call needs; the environment is built from fixed values."""
        self._runner = runner
        self._locations = locations
        self._target = target
        self._access = access
        self._environment = {**git_environment(locations.home, protocols), **access.env}

    async def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        extra_env: dict[str, str] | None = None,
    ) -> GitOutput:
        """Run one git command; the caller decides what its exit code means."""
        argv = ["git", *self._access.args, *FIXED_OPTIONS, *arguments]
        env = {**self._environment, **(extra_env or {})}
        return await self._runner.run(argv, cwd=cwd, env=env, timeout=timeout)

    async def _checked(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        extra_env: dict[str, str] | None = None,
    ) -> GitOutput:
        """Run a git command and raise ``GitSyncError`` unless it succeeded."""
        result = await self._run(arguments, cwd=cwd, timeout=timeout, extra_env=extra_env)
        if not result.ok:
            raise GitSyncError(classify(result.text, timed_out=result.timed_out))
        return result

    async def remote_has_branch(self) -> bool:
        """Whether the branch exists on the remote. Reads only; nothing is sent.

        Raises ``GitSyncError`` when the remote cannot be read, which also proves that the
        address, the credential and (for ssh) the pinned host key work for reading.
        """
        self._locations.ensure()
        result = await self._run(
            [
                "ls-remote",
                "--exit-code",
                "--heads",
                "--",
                self._target.remote.url,
                f"refs/heads/{self._target.branch}",
            ],
            cwd=self._locations.home,
            timeout=PROBE_TIMEOUT,
        )
        if result.ok:
            return True
        if result.exit_code == NO_MATCHING_REFS_EXIT and not result.timed_out:
            return False
        raise GitSyncError(classify(result.text, timed_out=result.timed_out))

    async def _init_work(self) -> None:
        """Create an empty repository in the working folder with the remote set."""
        work = self._locations.work
        try:
            work.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise GitSyncError(GitProblem.WORKDIR) from error
        await self._checked(
            ["init", "--quiet", f"--initial-branch={self._target.branch}"],
            cwd=work,
            timeout=LOCAL_TIMEOUT,
        )
        await self._checked(
            ["config", "remote.origin.url", self._target.remote.url],
            cwd=work,
            timeout=LOCAL_TIMEOUT,
        )

    async def _work_matches_remote(self) -> bool:
        """Whether the working folder is a repository whose origin is the current address."""
        work = self._locations.work
        if not (work / ".git").is_dir():
            return False
        result = await self._run(
            ["config", "--get", "remote.origin.url"], cwd=work, timeout=LOCAL_TIMEOUT
        )
        return result.ok and result.text.strip() == self._target.remote.url

    async def prepare_work(self, *, branch_exists: bool) -> Path:
        """Make the working clone match the remote branch, or empty when there is none.

        With the branch on the remote, the clone fetches it and is reset to its tip, so the
        export is always built on exactly what the repository holds and the push is a plain
        fast-forward. Without it, the clone starts empty and the push creates the branch.
        """
        self._locations.ensure()
        work = self._locations.work
        if not branch_exists or not await self._work_matches_remote():
            self._locations.reset_work()
            await self._init_work()
        if not branch_exists:
            return work
        branch = self._target.branch
        await self._checked(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--prune",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            cwd=work,
            timeout=FETCH_TIMEOUT,
        )
        await self._checked(
            ["checkout", "--quiet", "--force", "-B", branch, f"refs/remotes/origin/{branch}"],
            cwd=work,
            timeout=LOCAL_TIMEOUT,
        )
        await self._checked(["clean", "--quiet", "-ffdx"], cwd=work, timeout=LOCAL_TIMEOUT)
        return work

    async def commit_all(self, message: str) -> bool:
        """Commit every change in the working folder. Returns whether there was one."""
        work = self._locations.work
        await self._checked(["add", "--all"], cwd=work, timeout=LOCAL_TIMEOUT)
        status = await self._checked(["status", "--porcelain"], cwd=work, timeout=LOCAL_TIMEOUT)
        if not status.text.strip():
            return False
        name = commit_author(self._target.author_name)
        identity = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
        }
        await self._checked(
            ["commit", "--quiet", "--no-verify", "--no-gpg-sign", "--message", message],
            cwd=work,
            timeout=LOCAL_TIMEOUT,
            extra_env=identity,
        )
        return True

    async def push(self) -> None:
        """Push the working clone's head to the branch, never with force."""
        await self._checked(
            ["push", "--quiet", "origin", f"HEAD:refs/heads/{self._target.branch}"],
            cwd=self._locations.work,
            timeout=PUSH_TIMEOUT,
        )
