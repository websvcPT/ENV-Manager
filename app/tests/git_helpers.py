"""Helpers shared by the git sync tests.

Real git runs against bare repositories in temporary folders (the container the tests run in has
git and the ssh tools). The recording runner sits at the process boundary: it delegates local
commands to real git, answers the commands that would need a network from a script, and keeps
every argument list and environment together with the content of the credential files that
existed while the call ran, so a test can show where a secret was and where it was not.
"""

import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models.types import utc_now
from app.services.app_settings import GitSettings, save_section, write_secret
from app.services.audit import AuditActor
from app.services.git_locations import GitLocations
from app.services.git_process import (
    LOCAL_PROTOCOLS,
    REMOTE_PROTOCOLS,
    GitOutput,
    GitRunner,
    ProcessGitRunner,
)
from app.services.git_services import GitServices
from app.services.notify_events import NotificationEvent

TEST_PATH = "/usr/local/bin:/usr/bin:/bin"
PATH_IN_TEXT = re.compile(r"/[^\s'\"=:]+")


def tool(program: str, *arguments: str, cwd: Path | None = None) -> bytes:
    """Run a real program (git, ssh-keygen) for a test's setup or checks and return its output.

    The environment is a fixed one with no configuration of a user. A failure is an assertion
    error that carries the error output.
    """
    executable = shutil.which(program, path=TEST_PATH)
    assert executable is not None, f"{program} is not installed"
    env = {
        "PATH": TEST_PATH,
        "HOME": str(cwd) if cwd else "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    done = subprocess.run(  # noqa: S603
        [executable, *arguments], cwd=cwd, env=env, capture_output=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"{program} {' '.join(arguments)} failed: {done.stderr.decode()}")
    return done.stdout


def git(*arguments: str, cwd: Path | None = None) -> str:
    """The text output of real git."""
    return tool("git", *arguments, cwd=cwd).decode()


def make_bare_repository(path: Path, branch: str = "main") -> Path:
    """An empty bare repository whose default branch is ``branch``."""
    git("init", "--bare", "--quiet", f"--initial-branch={branch}", str(path))
    return path


def clone(bare: Path, destination: Path, branch: str = "main") -> Path:
    """A fresh clone of a branch of a bare repository."""
    git("clone", "--quiet", "--branch", branch, str(bare), str(destination))
    return destination


def ok() -> GitOutput:
    """A command that succeeded and printed nothing."""
    return GitOutput(0, False, "")


def failed(text: str, code: int = 128) -> GitOutput:
    """A command that failed with this output."""
    return GitOutput(code, False, text)


def no_matching_refs() -> GitOutput:
    """What ``git ls-remote --exit-code`` answers when the branch does not exist."""
    return GitOutput(2, False, "")


@dataclass
class Call:
    """One command that was run: its arguments, its environment and the credential files."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    files: dict[str, str] = field(default_factory=dict)
    modes: dict[str, int] = field(default_factory=dict)

    @property
    def command(self) -> str:
        """The name of the program or, for git, of its subcommand."""
        if self.argv[0] != "git":
            return self.argv[0]
        skip = False
        for item in self.argv[1:]:
            if skip:
                skip = False
            elif item == "-c":
                skip = True
            elif not item.startswith("-"):
                return item
        return "git"


Handler = Callable[[Call], GitOutput | Awaitable[GitOutput | None] | None]


class RecordingRunner:
    """Runs commands for a test: scripted answers for some, real git for the rest.

    ``script`` maps a command name (``ls-remote``, ``fetch``, ``push``, ``ssh-keyscan``) to a
    handler; a command with no handler goes to ``inner`` (real processes by default), which is
    what the local operations of a push need. ``scratch`` is the folder whose files are read at
    call time.
    """

    def __init__(
        self,
        scratch: Path | None = None,
        script: Mapping[str, Handler] | None = None,
        inner: GitRunner | None = None,
    ) -> None:
        """Hold the script, the real runner and the folder of credential files."""
        self.calls: list[Call] = []
        self.script: dict[str, Handler] = dict(script or {})
        self._inner = inner or ProcessGitRunner()
        self._scratch = scratch

    def _files_during_call(self, call: Call) -> None:
        """Keep the content and the mode of every file under the scratch folder the call names."""
        if self._scratch is None:
            return
        for text in [*call.argv, *call.env.values()]:
            for path_text in PATH_IN_TEXT.findall(text):
                path = Path(path_text)
                if path.is_file() and self._scratch in path.parents:
                    call.files[path_text] = path.read_text()
                    call.modes[path_text] = path.stat().st_mode & 0o777

    async def run(
        self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> GitOutput:
        """Record the call, then answer it from the script or with real git.

        A handler that answers ``None`` lets the real command run.
        """
        call = Call(list(argv), dict(env), cwd)
        self._files_during_call(call)
        self.calls.append(call)
        handler = self.script.get(call.command)
        answer = handler(call) if handler is not None else None
        if isinstance(answer, Awaitable):
            answer = await answer
        if answer is None:
            return await self._inner.run(argv, cwd=cwd, env=env, timeout=timeout)
        return answer

    def named(self, command: str) -> list[Call]:
        """The calls of one command, in order."""
        return [call for call in self.calls if call.command == command]


class RecordingEvents:
    """A notifier that keeps the events it receives."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[NotificationEvent] = []

    async def notify(self, event: NotificationEvent) -> None:
        """Keep the event."""
        self.events.append(event)


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, now: datetime | None = None) -> None:
        """Start at ``now`` (the real time by default)."""
        self.now = now or utc_now()

    def __call__(self) -> datetime:
        """The current moment of the test."""
        return self.now


class Sleeper:
    """A ``sleep`` that waits for nothing and remembers how long it was asked to."""

    def __init__(self) -> None:
        """Start with no waits."""
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Note the wait."""
        self.waits.append(seconds)


def git_services(
    tmp_path: Path,
    crypto: CryptoService,
    database_path: Path,
    *,
    runner: GitRunner | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    events: RecordingEvents | None = None,
    local: bool = True,
) -> GitServices:
    """Services on a temporary git folder. ``local`` opens the ``file`` transport for bare
    repositories in temporary folders."""
    notifier = events or RecordingEvents()
    return GitServices(
        crypto,
        GitLocations(tmp_path / "git"),
        database_path,
        runner=runner or ProcessGitRunner(),
        protocols=LOCAL_PROTOCOLS if local else REMOTE_PROTOCOLS,
        allow_local=local,
        sleep=sleeper or Sleeper(),
        clock=clock or Clock(),
        notifier=lambda: notifier,
    )


async def configure_git(db: AsyncSession, **changes: object) -> GitSettings:
    """Save git settings (the defaults with ``changes`` applied) and commit."""
    settings = GitSettings(**changes)  # type: ignore[arg-type]
    await save_section(db, "git", settings, actor=AuditActor.cli())
    await db.commit()
    return settings


def scratch_files(scratch: Path) -> list[Path]:
    """Every file left under a folder."""
    return [path for path in scratch.rglob("*") if path.is_file()] if scratch.exists() else []


def generate_key(
    folder: Path, name: str, *, passphrase: str = "", key_type: str = "ed25519", pem: bool = False
) -> tuple[str, str]:
    """A new key pair from ssh-keygen: the private key text and the public key line.

    The key has no passphrase unless one is given; ``pem`` writes the older PEM format instead of
    the OpenSSH one.
    """
    path = folder / name
    options = ["-m", "PEM"] if pem else []
    arguments = ["-q", "-t", key_type, *options, "-N", passphrase, "-C", "test", "-f", str(path)]
    tool("ssh-keygen", *arguments)
    return path.read_text(), Path(f"{path}.pub").read_text().strip()


def keygen_fingerprint(folder: Path, public_line: str) -> str:
    """The SHA256 fingerprint of a public key as ssh-keygen prints it."""
    path = folder / "fingerprint.pub"
    path.write_text(public_line + "\n")
    return tool("ssh-keygen", "-l", "-f", str(path)).decode().split()[1]


async def store_secret(db: AsyncSession, crypto: CryptoService, key: str, value: str) -> None:
    """Store a secret setting and commit."""
    await write_secret(db, crypto, key, value, actor=AuditActor.cli())
    await db.commit()
