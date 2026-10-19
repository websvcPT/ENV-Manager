"""How git is started: the environment, the argument lists and where a credential is and is not.

Local commands run for real and the commands that would reach a server are scripted, so every
call can be inspected: no argument list or environment holds a token or a key, the credential
exists only in a private file while the call runs, the remote configuration of the working clone
holds the plain address, and the files are gone afterwards.
"""

import shutil
import stat
import subprocess
from urllib.parse import quote

import pytest

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.services.app_settings import GIT_DEPLOY_KEY_KEY, GIT_TOKEN_KEY
from app.services.git_auth import GitSecrets, git_access
from app.services.git_errors import PROBLEMS, GitProblem
from app.services.git_hostkey import parse_entry
from app.services.git_push import PushStatus
from app.services.git_remote import parse_remote
from app.services.git_state import load_state, pin_host_key
from app.tests.git_helpers import Call, generate_key, git, no_matching_refs, ok, store_secret
from app.tests.git_world import World, https_only

TOKEN = "tok-9f2c1d7a5b3e-SECRET"
HTTPS_URL = "https://ops@git.example.com:8443/ops/env.git"
SSH_URL = "ssh://git@git.example.com:2222/ops/env.git"
BASE_KEYS = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_ALLOW_PROTOCOL",
    "GIT_OPTIONAL_LOCKS",
}
IDENTITY_KEYS = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
SSH_KEYS = {"GIT_SSH_COMMAND", "GIT_SSH_VARIANT"}


def script_network(world: World) -> None:
    """The remote answers that its branch does not exist and accepts the push."""
    world.runner.script["ls-remote"] = lambda _call: no_matching_refs()
    world.runner.script["push"] = lambda _call: ok()


def assert_clean_environment(call: Call, allowed: set[str]) -> None:
    """Only fixed names, none of the app's own variables and a private home folder."""
    assert set(call.env) <= allowed, set(call.env) - allowed
    assert not any(name.startswith("ENVMANAGER") for name in call.env)
    assert call.env["GIT_TERMINAL_PROMPT"] == "0"
    assert call.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert call.env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "SSH_AUTH_SOCK" not in call.env and "GIT_ASKPASS" not in call.env


async def store_https(world: World) -> None:
    """Save an https address and its token."""
    await world.configure(url=HTTPS_URL)
    async with world.factory() as db:
        await store_secret(db, world.crypto, GIT_TOKEN_KEY, TOKEN)
    https_only(world)
    script_network(world)


async def test_a_token_reaches_git_only_through_a_private_credential_file(
    git_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No argument, environment value or remote setting holds the token, and the file that does
    is private, named by the helper option only and removed when the push ends."""
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    await store_https(git_world)
    result = await git_world.push()
    assert result.status is PushStatus.PUSHED

    encoded = quote(TOKEN, safe="")
    assert git_world.runner.calls
    for call in git_world.runner.calls:
        text = "\n".join([*call.argv, *call.env.values()])
        assert TOKEN not in text and encoded not in text
        assert_clean_environment(call, BASE_KEYS | IDENTITY_KEYS)
        assert call.env["GIT_ALLOW_PROTOCOL"] == "ssh:https"
        assert call.env["HOME"] == str(git_world.services.locations.home)
        assert call.argv[:4] == ["git", "-c", "credential.helper=", "-c"]
        assert call.argv[4].startswith("credential.helper=store --file=")

    first = git_world.runner.named("ls-remote")[0]
    assert list(first.files.values()) == [f"https://ops:{encoded}@git.example.com:8443\n"]
    assert [stat.S_IMODE(mode) for mode in first.modes.values()] == [0o600]
    assert first.argv[-3:] == ["--", HTTPS_URL, "refs/heads/main"]
    push = git_world.runner.named("push")[0]
    assert push.argv[-4:] == ["push", "--quiet", "origin", "HEAD:refs/heads/main"]
    assert HTTPS_URL not in push.argv

    work = git_world.services.locations.work
    assert git("config", "--get", "remote.origin.url", cwd=work).strip() == HTTPS_URL
    assert TOKEN not in (work / ".git" / "config").read_text()
    locations = git_world.services.locations
    assert list(locations.scratch.iterdir()) == []
    for folder in (locations.root, locations.home, locations.scratch):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


async def test_a_deploy_key_reaches_ssh_only_through_private_files_and_a_strict_command(
    git_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key and the pinned host key are private files, and the ssh command allows nothing
    else: no agent, no other identity, strict checking against only the pinned key."""
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    key_text, _public = generate_key(git_world.tmp_path, "deploy")
    _host_private, host_public = generate_key(git_world.tmp_path, "host")
    host = parse_entry(f"[git.example.com]:2222 {' '.join(host_public.split()[:2])}")
    assert host is not None
    await git_world.configure(url=SSH_URL)
    browser_text = key_text.replace("\n", "\r\n").rstrip()
    async with git_world.factory() as db:
        await store_secret(db, git_world.crypto, GIT_DEPLOY_KEY_KEY, browser_text)
        pin_host_key(await load_state(db), host, git_world.clock.now)
        await db.commit()
    https_only(git_world)
    script_network(git_world)

    assert (await git_world.push()).status is PushStatus.PUSHED

    body = [line for line in key_text.splitlines() if line and "-----" not in line]
    for call in git_world.runner.calls:
        text = "\n".join([*call.argv, *call.env.values()])
        assert all(line not in text for line in body)
        assert_clean_environment(call, BASE_KEYS | IDENTITY_KEYS | SSH_KEYS)
    call = git_world.runner.named("ls-remote")[0]
    command = call.env["GIT_SSH_COMMAND"]
    for option in (
        "-F /dev/null",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        "HostKeyAlgorithms=ssh-ed25519",
        "ForwardAgent=no",
        "PasswordAuthentication=no",
        "ConnectTimeout=",
    ):
        assert option in command
    assert call.env["GIT_SSH_VARIANT"] == "ssh"
    files = {path.rsplit("/", 1)[1]: text for path, text in call.files.items()}
    assert files["key"] == key_text and files["key"].endswith("\n") and "\r" not in files["key"]
    assert files["known_hosts"] == host.entry + "\n"
    assert host.entry.startswith("[git.example.com]:2222 ssh-ed25519 ")
    assert {stat.S_IMODE(mode) for mode in call.modes.values()} == {0o600}
    assert list(git_world.services.locations.scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("url", "stored", "problem"),
    [
        (SSH_URL, None, GitProblem.NOT_PINNED),
        (HTTPS_URL, None, GitProblem.NO_TOKEN),
    ],
)
async def test_without_what_the_address_needs_nothing_is_run(
    git_world: World, url: str, stored: str | None, problem: GitProblem
) -> None:
    """A missing credential or an unpinned host key fails the run before any git command."""
    await git_world.configure(url=url)
    async with git_world.factory() as db:
        if url == SSH_URL:
            await store_secret(db, git_world.crypto, GIT_DEPLOY_KEY_KEY, "key")
    https_only(git_world)
    result = await git_world.push()
    assert (result.status, result.attempts) == (PushStatus.FAILED, 1)
    assert result.reason == PROBLEMS[problem].message
    assert git_world.runner.calls == []


async def test_a_deploy_key_is_needed_for_ssh_and_a_pin_for_this_host_and_port(
    git_world: World,
) -> None:
    """No key stored is its own problem, and a pin made for another port does not count."""
    _host_private, host_public = generate_key(git_world.tmp_path, "host")
    other_port = parse_entry(f"git.example.com {' '.join(host_public.split()[:2])}")
    assert other_port is not None
    await git_world.configure(url=SSH_URL)
    https_only(git_world)
    assert (await git_world.push()).reason == PROBLEMS[GitProblem.NO_DEPLOY_KEY].message
    async with git_world.factory() as db:
        await store_secret(db, git_world.crypto, GIT_DEPLOY_KEY_KEY, "key")
        pin_host_key(await load_state(db), other_port, git_world.clock.now)
        await db.commit()
    git_world.later(61)
    assert (await git_world.push()).reason == PROBLEMS[GitProblem.NOT_PINNED].message


async def test_a_credential_sealed_with_another_key_is_reported_as_unreadable(
    git_world: World,
) -> None:
    """After a restore with the wrong key the credential cannot be opened: a fixed sentence."""
    await git_world.configure(url=HTTPS_URL)
    other = CryptoService(parse_master_key(generate_master_key()))
    async with git_world.factory() as db:
        await store_secret(db, other, GIT_TOKEN_KEY, TOKEN)
    https_only(git_world)
    result = await git_world.push()
    assert result.reason == PROBLEMS[GitProblem.UNREADABLE_CREDENTIAL].message
    assert git_world.runner.calls == []


async def test_an_address_stored_with_another_transport_is_refused_by_git_itself(
    git_world: World,
) -> None:
    """A path or ext address that got into the settings by other means is not run: the address
    is refused at parse time, and git is told which transports exist as a second barrier."""
    await git_world.configure(url="ext::sh -c 'touch /tmp/x'")
    https_only(git_world)
    result = await git_world.push()
    assert result.reason == PROBLEMS[GitProblem.NOT_CONFIGURED].message
    assert git_world.runner.calls == []
    await git_world.configure(url=str(git_world.bare))
    git_world.later(61)
    local = await git_world.push()
    assert local.reason == PROBLEMS[GitProblem.NOT_CONFIGURED].message
    assert git_world.runner.calls == []


@pytest.mark.parametrize(
    "token",
    [TOKEN, "ghp_a+b/c=d@e:f%g h#i?j&k", "pa$$word'quoted\"", "ünï-tökén"],
)
def test_git_itself_reads_the_credential_file_for_the_right_host_only(
    git_world: World, token: str
) -> None:
    """Real git, asked for credentials with the helper option of a call, answers with exactly
    the user and token for the address in the file and with nothing for any other host."""
    remote = parse_remote(HTTPS_URL)
    locations = git_world.services.locations
    with git_access(remote, locations, GitSecrets(token=token), None) as access:
        helper = [*access.args]
        assert helper[:3] == ["-c", "credential.helper=", "-c"]

        def ask(host: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(  # noqa: S603
                [shutil.which("git") or "git", *helper, "credential", "fill"],
                input=f"protocol=https\nhost={host}\nusername=ops\n\n",
                capture_output=True,
                text=True,
                check=False,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": str(git_world.tmp_path),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                },
            )

        mine = ask("git.example.com:8443")
        assert mine.returncode == 0
        answer = dict(line.split("=", 1) for line in mine.stdout.splitlines() if "=" in line)
        assert (answer["username"], answer["password"]) == ("ops", token)
        other = ask("evil.example.com")
        assert other.returncode != 0 and token not in other.stdout
    assert list(locations.scratch.iterdir()) == []
