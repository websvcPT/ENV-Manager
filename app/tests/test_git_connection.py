"""The connection test and the host key pin: what is shown, what is stored, what is refused.

The server side is scripted: ``ssh-keyscan`` answers with real public keys made by ssh-keygen
and ``ls-remote`` with the exit codes git uses, so a test can follow the whole path of a first
test, the acceptance of a fingerprint, a later test and a key that changed.
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AuditEntry
from app.services.app_settings import GIT_DEPLOY_KEY_KEY, GIT_TOKEN_KEY, GitSettings
from app.services.audit import AuditActor
from app.services.git_connection import (
    CHANGED_AFTER_SHOWN,
    PINNED_ALREADY,
    CheckState,
    ConnectionCheck,
    accept_host_key,
    check_connection,
)
from app.services.git_errors import PROBLEMS, GitProblem
from app.services.git_hostkey import HostKey
from app.services.git_process import GitOutput
from app.services.git_settings import CleanGit, save_git
from app.services.git_state import load_state
from app.tests.git_helpers import (
    failed,
    generate_key,
    keygen_fingerprint,
    no_matching_refs,
    ok,
    store_secret,
)
from app.tests.git_world import World, https_only

SSH_URL = "ssh://git@git.example.com:2222/ops/env.git"
HTTPS_URL = "https://git.example.com/ops/env.git"
KNOWN_NAME = "[git.example.com]:2222"
ACTOR = AuditActor.cli()


def offered(world: World, name: str) -> tuple[str, str, str]:
    """A new host key made by ssh-keygen: the keyscan line, the key type and its fingerprint."""
    _private, public = generate_key(world.tmp_path, name)
    key_type, blob = public.split()[:2]
    fingerprint = keygen_fingerprint(world.tmp_path, public)
    return (
        f"{KNOWN_NAME} {key_type} {blob}\n# git.example.com:2222 SSH-2.0-OpenSSH_10\n",
        key_type,
        fingerprint,
    )


def serve_keys(world: World, scan_output: str) -> None:
    """Make ssh-keyscan answer with these lines and the repository with no branch."""
    world.runner.script["ssh-keyscan"] = lambda _call: GitOutput(0, False, scan_output)
    world.runner.script["ls-remote"] = lambda _call: no_matching_refs()


async def ssh_world(world: World) -> None:
    """An ssh address with a deploy key stored and no host key pinned."""
    await world.configure(url=SSH_URL)
    async with world.factory() as db:
        await store_secret(db, world.crypto, GIT_DEPLOY_KEY_KEY, "key")
    https_only(world)


async def check(world: World) -> ConnectionCheck:
    """Run the connection test."""
    async with world.factory() as db:
        return await check_connection(db, world.services, actor=ACTOR)


async def accept(world: World, key_type: str, fingerprint: str) -> ConnectionCheck:
    """Accept a host key by type and fingerprint."""
    async with world.factory() as db:
        return await accept_host_key(
            db, world.services, actor=ACTOR, key_type=key_type, fingerprint=fingerprint
        )


async def pinned(world: World) -> str | None:
    """The pinned entry, None when nothing is pinned."""
    async with world.factory() as db:
        return (await load_state(db)).host_key


async def test_a_first_test_shows_the_fingerprint_and_sends_nothing(git_world: World) -> None:
    """With no key pinned the test stops at the fingerprint ssh-keygen would print."""
    await ssh_world(git_world)
    scan, key_type, fingerprint = offered(git_world, "host")
    serve_keys(git_world, scan)
    result = await check(git_world)
    assert result.state is CheckState.NEEDS_PIN
    assert result.key is not None
    assert (result.key.key_type, result.key.fingerprint) == (key_type, fingerprint)
    assert result.key.host == KNOWN_NAME
    assert [call.command for call in git_world.runner.calls] == ["ssh-keyscan"]
    keyscan = git_world.runner.named("ssh-keyscan")[0]
    assert keyscan.argv[-4:] == ["-p", "2222", "--", "git.example.com"]
    assert await pinned(git_world) is None


async def test_accepting_the_fingerprint_pins_that_key_and_tests_again(git_world: World) -> None:
    """The accepted key is stored with its host name, the pin is audited, and the test that
    follows reads the repository with strict checking against exactly that key."""
    await ssh_world(git_world)
    scan, key_type, fingerprint = offered(git_world, "host")
    serve_keys(git_world, scan)
    result = await accept(git_world, key_type, fingerprint)
    assert result.state is CheckState.OK and result.branch_exists is False
    entry = await pinned(git_world)
    assert entry is not None and entry.startswith(f"{KNOWN_NAME} {key_type} ")
    read = git_world.runner.named("ls-remote")[0]
    assert "StrictHostKeyChecking=yes" in read.env["GIT_SSH_COMMAND"]
    assert list(read.files.values()).count(entry + "\n") == 1
    async with git_world.factory() as db:
        pins = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "git.host_key_pin")))
            .scalars()
            .all()
        )
    assert [pin.details for pin in pins] == [
        {"host": "git.example.com:2222", "key_type": key_type, "fingerprint": fingerprint}
    ]
    again = await check(git_world)
    assert again.state is CheckState.OK


async def test_a_key_that_changed_is_refused_and_nothing_is_sent(git_world: World) -> None:
    """A server that no longer presents the pinned key gets a refusal with both fingerprints, and
    no git command (so no credential) ever reaches it."""
    await ssh_world(git_world)
    scan, key_type, fingerprint = offered(git_world, "host")
    serve_keys(git_world, scan)
    await accept(git_world, key_type, fingerprint)
    git_world.runner.calls.clear()

    changed_scan, _type, changed_fingerprint = offered(git_world, "other")
    serve_keys(git_world, changed_scan)
    result = await check(git_world)
    assert result.state is CheckState.KEY_CHANGED
    assert result.message == PROBLEMS[GitProblem.HOST_KEY_CHANGED].message
    assert result.pinned_fingerprint == fingerprint
    assert result.key is not None and result.key.fingerprint == changed_fingerprint
    assert [call.command for call in git_world.runner.calls] == ["ssh-keyscan"]
    assert (await pinned(git_world)) is not None


async def test_a_fingerprint_that_changed_after_it_was_shown_is_not_pinned(
    git_world: World,
) -> None:
    """Accepting pins what the server presents now, and only if it is what was shown."""
    await ssh_world(git_world)
    scan, key_type, _fingerprint = offered(git_world, "host")
    serve_keys(git_world, scan)
    shown = await check(git_world)
    assert shown.key is not None
    swapped_scan, _type, _swapped = offered(git_world, "swapped")
    serve_keys(git_world, swapped_scan)
    refused = await accept(git_world, key_type, shown.key.fingerprint)
    assert (refused.state, refused.message) == (CheckState.FAILED, CHANGED_AFTER_SHOWN)
    assert await pinned(git_world) is None
    forged = await accept(git_world, key_type, "SHA256:not-a-key-the-server-has")
    assert forged.message == CHANGED_AFTER_SHOWN
    assert await pinned(git_world) is None


async def test_a_pinned_key_is_never_replaced_by_accepting_another(git_world: World) -> None:
    """Re-pinning takes a change of the address; a post for another key is refused."""
    await ssh_world(git_world)
    scan, key_type, fingerprint = offered(git_world, "host")
    serve_keys(git_world, scan)
    await accept(git_world, key_type, fingerprint)
    before = await pinned(git_world)
    other_scan, other_type, other_fingerprint = offered(git_world, "other")
    serve_keys(git_world, other_scan)
    refused = await accept(git_world, other_type, other_fingerprint)
    assert (refused.state, refused.message) == (CheckState.FAILED, PINNED_ALREADY)
    assert await pinned(git_world) == before


async def test_an_https_address_has_no_host_key_to_pin(git_world: World) -> None:
    """Pinning is for ssh addresses only."""
    await git_world.configure(url=HTTPS_URL)
    https_only(git_world)
    refused = await accept(git_world, "ssh-ed25519", "SHA256:x")
    assert refused.state is CheckState.FAILED and git_world.runner.calls == []


async def test_a_server_that_cannot_be_scanned_fails_with_a_fixed_sentence(
    git_world: World,
) -> None:
    """No keys offered (nothing listens, or it is not ssh) is a network problem, no raw output."""
    await ssh_world(git_world)
    git_world.runner.script["ssh-keyscan"] = lambda _call: failed(
        "# git.example.com:2222 SSH-2.0-OpenSSH\nconnect: Connection refused", code=1
    )
    result = await check(git_world)
    assert (result.state, result.message) == (
        CheckState.FAILED,
        PROBLEMS[GitProblem.NETWORK].message,
    )


async def test_a_test_with_an_https_token_reads_the_remote_and_sends_nothing(
    git_world: World,
) -> None:
    """The test is a read: ls-remote only, with the token in a private file, never a push."""
    await git_world.configure(url=HTTPS_URL)
    async with git_world.factory() as db:
        await store_secret(db, git_world.crypto, GIT_TOKEN_KEY, "tok-abc")
    https_only(git_world)
    git_world.runner.script["ls-remote"] = lambda _call: ok()
    result = await check(git_world)
    assert (result.state, result.branch_exists) == (CheckState.OK, True)
    assert [call.command for call in git_world.runner.calls] == ["ls-remote"]
    assert "tok-abc" not in "\n".join(git_world.runner.calls[0].argv)
    git_world.runner.script["ls-remote"] = lambda _call: failed(
        "remote: Invalid username or password.\nfatal: Authentication failed"
    )
    refused = await check(git_world)
    assert (refused.state, refused.message) == (
        CheckState.FAILED,
        PROBLEMS[GitProblem.AUTH].message,
    )


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("", GitProblem.NOT_CONFIGURED),
        (HTTPS_URL, GitProblem.NO_TOKEN),
        (SSH_URL, GitProblem.NO_DEPLOY_KEY),
    ],
)
async def test_a_test_says_what_is_missing_before_it_connects(
    git_world: World, url: str, problem: GitProblem
) -> None:
    """No address, no token or no key: a fixed sentence and no process."""
    await git_world.configure(url=url)
    https_only(git_world)
    result = await check(git_world)
    assert (result.state, result.message) == (CheckState.FAILED, PROBLEMS[problem].message)
    assert git_world.runner.calls == []


async def test_every_test_is_audited_with_the_host_only(git_world: World) -> None:
    """The audit entry names the host and the outcome, never the address, user or credential."""
    await git_world.configure(url="https://ops@git.example.com:8443/ops/env.git")
    async with git_world.factory() as db:
        await store_secret(db, git_world.crypto, GIT_TOKEN_KEY, "tok-abc")
    https_only(git_world)
    git_world.runner.script["ls-remote"] = lambda _call: ok()
    await check(git_world)
    async with git_world.factory() as db:
        entry = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "git.test")))
            .scalars()
            .one()
        )
    assert entry.details["host"] == "git.example.com:8443"
    assert entry.details["outcome"] == "ok"
    assert "ops" not in repr(entry.details) and "tok-abc" not in repr(entry.details)


async def test_changing_the_address_clears_the_pin_and_the_clone_but_saving_the_same_one_keeps_them(
    git_world: World,
) -> None:
    """The pin belongs to the address: a new address forgets it (and audits that), the same
    address keeps it, and the working clone of the old repository is removed."""
    await ssh_world(git_world)
    scan, key_type, fingerprint = offered(git_world, "host")
    serve_keys(git_world, scan)
    await accept(git_world, key_type, fingerprint)
    locations = git_world.services.locations
    locations.work.mkdir(parents=True)
    (locations.work / "marker").write_text("old repository")

    async def save(url: str) -> bool:
        async with git_world.factory() as db:
            clean = CleanGit(GitSettings(url=url), "", "", False, False)
            return await save_git(db, git_world.crypto, clean, actor=ACTOR, locations=locations)

    assert await save(SSH_URL) is False
    assert await pinned(git_world) is not None and locations.work.exists()
    assert await save("ssh://git@git.example.com:2222/ops/other.git") is True
    assert await pinned(git_world) is None
    assert not locations.work.exists()
    async with git_world.factory() as db:
        clears = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "git.host_key_clear")))
            .scalars()
            .all()
        )
        settings_entry = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "settings.update")))
            .scalars()
            .all()[-1]
        )
    assert [item.details["reason"] for item in clears] == ["address_changed"]
    changes = settings_entry.details["changes"]
    assert isinstance(changes, list)
    assert {
        "setting": "git.url",
        "kind": "url",
        "old": "ssh://git.example.com:2222",
        "new": "ssh://git.example.com:2222",
    } in changes


def test_a_host_key_is_a_known_hosts_line_with_the_openssh_fingerprint(tmp_path: Path) -> None:
    """The fingerprint and the line are what ssh itself uses."""
    _private, public = generate_key(tmp_path, "host")
    key_type, blob = public.split()[:2]
    key = HostKey(KNOWN_NAME, key_type, blob)
    assert key.fingerprint == keygen_fingerprint(tmp_path, public)
    assert key.entry == f"{KNOWN_NAME} {key_type} {blob}"
