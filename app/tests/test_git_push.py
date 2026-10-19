"""Pushing the export to a real bare repository: both layouts, the watermark, retries and records.

The repository is a bare one in a temporary folder, reached through the ``file`` transport that
only the service layer allows; git itself is real. The clock and the sleep are replaced so a day
of schedule and the waits of the retries run at once, and the commands that would need a network
are scripted.
"""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.display_time import DisplayTime
from app.models import AuditEntry, AuditResult, Environment, EnvironmentVersion
from app.services.audit import AuditActor, record_actor_audit
from app.services.git_errors import PROBLEMS, GitProblem
from app.services.git_export import (
    MANIFEST_FILE,
    META_FILE,
    PER_VERSION,
    SNAPSHOT,
    SNAPSHOT_CONTEXT,
    SNAPSHOT_FILE,
    VERSIONS_DIR,
    meta_context,
    path_hash,
)
from app.services.git_process import GitOutput
from app.services.git_push import (
    MIN_PUSH_GAP_SECONDS,
    RETRY_DELAYS,
    PushStatus,
    SkipReason,
    run_scheduled_push,
)
from app.services.git_repo import COMMIT_EMAIL
from app.services.git_state import load_state
from app.services.notify_events import GitPushFailed
from app.services.shell_banners import load_shell_banners
from app.services.versions import create_version
from app.tests.git_helpers import Call, clone, failed, git
from app.tests.git_scan import check_paths, collect_needles, scan_history, scan_tree
from app.tests.git_world import World

NETWORK_DOWN = "fatal: unable to access 'https://git.example.com/': Could not resolve host"
AUTH_REFUSED = "remote: Invalid username or password.\nfatal: Authentication failed for 'x'"


def network_down(_call: Call) -> GitOutput:
    """A remote that cannot be reached."""
    return failed(NETWORK_DOWN)


def auth_refused(_call: Call) -> GitOutput:
    """A remote that refuses the credentials."""
    return failed(AUTH_REFUSED)


async def add_version(world: World, text: str = "NEW=value-after-push\n") -> int:
    """Save a new version of the seeded editor file and return that file's id."""
    environment_id = world.seed.editor.id
    async with world.factory() as db:
        await create_version(db, world.crypto, environment_id, text, author=None)
        await db.commit()
    return environment_id


def push_readme(bare: Path, folder: Path) -> None:
    """Put one commit with a README on the branch of an empty bare repository."""
    git("init", "--quiet", "--initial-branch=main", str(folder))
    (folder / "README.md").write_text("hello\n")
    git("add", "README.md", cwd=folder)
    git(
        "-c",
        "user.name=someone",
        "-c",
        "user.email=s@example.com",
        "commit",
        "-q",
        "-m",
        "readme",
        cwd=folder,
    )
    git("push", "--quiet", str(bare), "main", cwd=folder)


@pytest.mark.parametrize("layout", [PER_VERSION, SNAPSHOT])
async def test_the_first_push_holds_only_ciphertext_and_hashed_paths(
    git_world: World, layout: str
) -> None:
    """Whatever the layout, a clone shows ciphertext under hashed paths: no value, no name."""
    await git_world.configure(export_format=layout)
    result = await git_world.push()
    assert result.status is PushStatus.PUSHED
    checkout = git_world.checkout()
    assert MANIFEST_FILE in check_paths(checkout)
    needles = await collect_needles(git_world.engine, git_world.crypto)
    assert needles.values and needles.names
    assert scan_tree(checkout, needles) == []
    assert scan_history(checkout, needles) == []
    commits = git("log", "--format=%an|%ae|%s", cwd=checkout).splitlines()
    assert len(commits) == 1
    assert commits[0].startswith(f"ENV Manager|{COMMIT_EMAIL}|Encrypted export ")


async def test_the_scan_finds_a_plaintext_file_so_a_clean_result_means_something(
    git_world: World,
) -> None:
    """The scan is not vacuous: a readable value or name in the tree or the history is found."""
    await git_world.configure()
    await git_world.push()
    checkout = git_world.checkout()
    needles = await collect_needles(git_world.engine, git_world.crypto)
    (checkout / "leak.txt").write_text("MYSQL_PASSWORD=Zq7vK2mPw9\n")
    with pytest.raises(AssertionError, match="is not ciphertext"):
        scan_tree(checkout, needles)
    (checkout / "leak.txt").unlink()
    git(
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@x",
        "commit",
        "--allow-empty",
        "-m",
        "Fancy Proj. Name Zq7vK2mPw9 leak",
        cwd=checkout,
    )
    assert scan_history(checkout, needles)


async def test_the_per_version_layout_has_one_file_per_version_that_opens_with_the_key(
    git_world: World,
) -> None:
    """Each version file is the stored ciphertext, and the metadata file names what it holds."""
    await git_world.configure()
    await git_world.push()
    checkout = git_world.checkout()
    async with git_world.factory() as db:
        versions = (await db.execute(select(EnvironmentVersion))).scalars().all()
        some = (await db.execute(select(Environment).limit(1))).scalars().one()
    assert versions
    for version in versions:
        name = path_hash(git_world.crypto, version.environment_id)
        sealed = (checkout / VERSIONS_DIR / name / f"{version.number:06d}.enc").read_text().strip()
        assert sealed == version.content_encrypted
    name = path_hash(git_world.crypto, some.id)
    sealed_meta = (checkout / VERSIONS_DIR / name / META_FILE).read_text().strip()
    meta = json.loads(git_world.crypto.decrypt(sealed_meta, meta_context(name)))
    assert meta["environment"]["name"] == some.name
    assert meta["environment"]["target_path"] == some.target_path
    assert [item["number"] for item in meta["versions"]] == sorted(
        v.number for v in versions if v.environment_id == some.id
    )


async def test_the_snapshot_layout_is_one_sealed_database_file(
    git_world: World, tmp_path: Path
) -> None:
    """The snapshot opens with the key into a database that holds the seeded structure."""
    await git_world.configure(export_format=SNAPSHOT)
    await git_world.push()
    checkout = git_world.checkout()
    opened = tmp_path / "opened.db"
    opened.write_bytes(
        git_world.crypto.decrypt((checkout / SNAPSHOT_FILE).read_text().strip(), SNAPSHOT_CONTEXT)
    )
    with sqlite3.connect(opened) as connection:
        names = {row[0] for row in connection.execute("select name from projects")}
    assert names == {"Fancy Proj. Name", "billing"}


async def test_nothing_changed_means_nothing_pushed_and_one_new_version_adds_only_its_files(
    git_world: World,
) -> None:
    """The watermark: an unchanged export is not pushed, and a new version adds only its file."""
    await git_world.configure()
    assert (await git_world.push()).status is PushStatus.PUSHED
    git_world.later()
    again = await git_world.push()
    assert (again.status, again.skip) == (PushStatus.SKIPPED, SkipReason.NO_CHANGE)
    before = int(git("rev-list", "--count", "main", cwd=git_world.bare))

    environment_id = await add_version(git_world)
    git_world.later()
    assert (await git_world.push()).status is PushStatus.PUSHED
    assert int(git("rev-list", "--count", "main", cwd=git_world.bare)) == before + 1
    changed = git("diff", "--name-only", "HEAD~1", "HEAD", cwd=git_world.checkout("later"))
    folder = f"{VERSIONS_DIR}/{path_hash(git_world.crypto, environment_id)}"
    assert sorted(changed.splitlines()) == [f"{folder}/000004.enc", f"{folder}/{META_FILE}"]


async def test_a_push_waits_an_hour_after_the_last_one(git_world: World) -> None:
    """Even with a change waiting, a second push inside the hour is skipped."""
    await git_world.configure()
    assert (await git_world.push()).status is PushStatus.PUSHED
    await add_version(git_world)
    git_world.later(10)
    recent = await git_world.push()
    assert (recent.status, recent.skip) == (PushStatus.SKIPPED, SkipReason.RECENT)
    git_world.later(MIN_PUSH_GAP_SECONDS // 60)
    assert (await git_world.push()).status is PushStatus.PUSHED


async def test_an_unconfigured_or_switched_off_sync_runs_no_command(git_world: World) -> None:
    """With no address, or with the switch off, nothing runs at all."""
    first = await git_world.push()
    assert (first.status, first.skip) == (PushStatus.SKIPPED, SkipReason.NOT_CONFIGURED)
    await git_world.configure(push_enabled=False)
    second = await git_world.push()
    assert (second.status, second.skip) == (PushStatus.SKIPPED, SkipReason.DISABLED)
    assert git_world.runner.calls == []


async def test_a_new_branch_or_format_pushes_everything_again(git_world: World) -> None:
    """The mark includes where and how the export goes, so changing either pushes it all."""
    await git_world.configure()
    await git_world.push()
    git_world.later()
    assert (await git_world.push()).skip is SkipReason.NO_CHANGE

    await git_world.configure(branch="other")
    assert (await git_world.push()).status is PushStatus.PUSHED
    assert git("rev-parse", "--verify", "other", cwd=git_world.bare)
    git_world.later()
    await git_world.configure(branch="other", export_format=SNAPSHOT)
    assert (await git_world.push()).status is PushStatus.PUSHED
    tree = git("ls-tree", "-r", "--name-only", "other", cwd=git_world.bare).split()
    assert SNAPSHOT_FILE in tree
    assert not any(path.startswith(VERSIONS_DIR) for path in tree)


async def test_a_remote_with_commits_of_its_own_is_built_on_and_never_forced(
    git_world: World, tmp_path: Path
) -> None:
    """A README that someone pushed stays, and the push is a plain fast-forward on top of it."""
    push_readme(git_world.bare, tmp_path / "other")
    await git_world.configure()
    assert (await git_world.push()).status is PushStatus.PUSHED
    assert (clone(git_world.bare, tmp_path / "after") / "README.md").read_text() == "hello\n"
    log = git("log", "--format=%s", "main", cwd=git_world.bare).splitlines()
    assert log[-1] == "readme" and len(log) == 2
    for call in git_world.runner.named("push"):
        assert "--force" not in call.argv and not any(a.startswith("+") for a in call.argv)


async def test_a_fault_that_can_pass_is_retried_with_waits_and_then_succeeds(
    git_world: World,
) -> None:
    """Two network failures, then the repository answers: three attempts, waits of 5 and 30 s."""
    await git_world.configure()
    answers = iter([failed(NETWORK_DOWN), failed(NETWORK_DOWN)])
    git_world.runner.script["ls-remote"] = lambda _call: next(answers, None)
    result = await git_world.push()
    assert (result.status, result.attempts) == (PushStatus.PUSHED, 3)
    assert git_world.sleeper.waits == [5.0, 30.0]
    assert git_world.events.events == []
    async with git_world.factory() as db:
        state = await load_state(db)
    assert (state.failures, state.retry_at, state.last_error) == (0, None, None)


async def test_a_push_that_keeps_failing_is_recorded_announced_audited_and_waited_on(
    git_world: World,
) -> None:
    """After four attempts the run fails once: state, event, audit entry and banner all agree,
    and the wait before the next run doubles each time."""
    await git_world.configure()
    git_world.runner.script["ls-remote"] = network_down
    reason = PROBLEMS[GitProblem.NETWORK].message

    result = await git_world.push()
    assert (result.status, result.attempts, result.reason) == (PushStatus.FAILED, 4, reason)
    assert git_world.sleeper.waits == list(RETRY_DELAYS)
    assert git_world.events.events == [GitPushFailed(reason, 4)]
    async with git_world.factory() as db:
        state = await load_state(db)
        assert state.failures == 1 and state.last_error == reason
        assert state.retry_at == git_world.clock.now + timedelta(hours=1)
        entry = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "git.push")))
            .scalars()
            .one()
        )
        assert entry.result is AuditResult.FAILURE
        assert entry.actor_name == "scheduler"
        assert entry.details == {
            "host": "local path",
            "format": PER_VERSION,
            "problem": "network",
            "reason": reason,
            "attempts": 4,
        }
        banners = await load_shell_banners(db, DisplayTime())
    assert any(
        "Git push failing" in banner.message and reason in banner.message for banner in banners
    )

    git_world.later(30)
    waiting = await git_world.push()
    assert (waiting.status, waiting.skip) == (PushStatus.SKIPPED, SkipReason.WAITING)
    assert len(git_world.events.events) == 1

    git_world.later(31)
    assert (await git_world.push()).status is PushStatus.FAILED
    async with git_world.factory() as db:
        state = await load_state(db)
    assert state.failures == 2
    assert state.retry_at == git_world.clock.now + timedelta(hours=2)
    assert len(git_world.events.events) == 2

    del git_world.runner.script["ls-remote"]
    git_world.later(121)
    assert (await git_world.push()).status is PushStatus.PUSHED
    async with git_world.factory() as db:
        state = await load_state(db)
        assert (state.failures, state.retry_at, state.last_error) == (0, None, None)
        banners = await load_shell_banners(db, DisplayTime())
    assert not any("Git push" in banner.message for banner in banners)


async def test_a_refused_credential_is_not_retried(git_world: World) -> None:
    """A fault that waiting cannot fix fails at once, without the waits."""
    await git_world.configure()
    git_world.runner.script["ls-remote"] = auth_refused
    result = await git_world.push()
    assert (result.status, result.attempts) == (PushStatus.FAILED, 1)
    assert result.reason == PROBLEMS[GitProblem.AUTH].message
    assert git_world.sleeper.waits == []


async def test_a_rejected_push_is_reported_without_any_text_of_the_server(
    git_world: World,
) -> None:
    """A protected branch is a fixed sentence: nothing the server printed reaches a record."""
    await git_world.configure()
    git_world.runner.script["push"] = lambda _call: failed(
        " ! [remote rejected] HEAD -> main (protected branch hook declined)\nerror: secret-detail"
    )
    result = await git_world.push()
    assert result.reason == PROBLEMS[GitProblem.REJECTED].message
    assert "secret-detail" not in repr(git_world.events.events)
    async with git_world.factory() as db:
        text = repr([entry.details for entry in (await db.execute(select(AuditEntry))).scalars()])
        state = await load_state(db)
    assert "secret-detail" not in text and "secret-detail" not in (state.last_error or "")


async def test_the_pushes_own_audit_entries_do_not_start_the_next_push(git_world: World) -> None:
    """In the snapshot layout the audit log is part of the export, but git and backup entries
    do not move the mark: an idle installation pushes nothing, another entry pushes again."""
    await git_world.configure(export_format=SNAPSHOT)
    assert (await git_world.push()).status is PushStatus.PUSHED
    git_world.later()
    assert (await git_world.push()).skip is SkipReason.NO_CHANGE

    async with git_world.factory() as db:
        await record_actor_audit(
            db, AuditActor.cli(), action="backup.create", result=AuditResult.SUCCESS
        )
        await db.commit()
    git_world.later()
    assert (await git_world.push()).skip is SkipReason.NO_CHANGE

    async with git_world.factory() as db:
        await record_actor_audit(
            db, AuditActor.cli(), action="user.create", result=AuditResult.SUCCESS
        )
        await db.commit()
    git_world.later()
    assert (await git_world.push()).status is PushStatus.PUSHED


async def test_the_per_version_layout_ignores_audit_entries(git_world: World) -> None:
    """An entry that is not about a file or its structure does not change that export."""
    await git_world.configure()
    assert (await git_world.push()).status is PushStatus.PUSHED
    async with git_world.factory() as db:
        await record_actor_audit(
            db, AuditActor.cli(), action="user.create", result=AuditResult.SUCCESS
        )
        await db.commit()
    git_world.later()
    assert (await git_world.push()).skip is SkipReason.NO_CHANGE


async def test_the_scheduled_job_pushes_when_due_and_never_raises(git_world: World) -> None:
    """The job the scheduler runs does the same as a due push, and a broken database or any
    other unexpected error ends the run quietly instead of killing the scheduler."""
    await git_world.configure()
    await run_scheduled_push(git_world.factory, git_world.services)
    assert git("rev-parse", "--verify", "main", cwd=git_world.bare)

    def broken() -> None:
        raise RuntimeError("the database is gone")

    await run_scheduled_push(broken, git_world.services)  # type: ignore[arg-type]


async def test_an_unexpected_error_is_a_recorded_failure_with_a_fixed_sentence(
    git_world: World,
) -> None:
    """A bug or a broken disk must not end as a silent run: it fails like any other fault, and
    nothing the exception said reaches the record."""
    await git_world.configure()

    def explode(_call: Call) -> GitOutput:
        raise RuntimeError("secret-detail from a bug")

    git_world.runner.script["ls-remote"] = explode
    result = await git_world.push()
    reason = PROBLEMS[GitProblem.FAILED].message
    assert (result.status, result.attempts, result.reason) == (PushStatus.FAILED, 4, reason)
    assert "secret-detail" not in repr(git_world.events.events)
    async with git_world.factory() as db:
        state = await load_state(db)
        entries = (await db.execute(select(AuditEntry))).scalars().all()
    assert state.failures == 1 and state.last_error == reason
    assert "secret-detail" not in repr([entry.details for entry in entries])


async def test_a_run_whose_settings_changed_meanwhile_records_no_failure(
    git_world: World,
) -> None:
    """The superadmin fixes the repository while a push fails: the old failure is dropped, not
    banner and not a wait for the settings that were just saved."""
    await git_world.configure()

    async def change_then_fail(_call: Call) -> GitOutput:
        await git_world.configure(branch="other")
        return failed(AUTH_REFUSED)

    git_world.runner.script["ls-remote"] = change_then_fail
    result = await git_world.push()
    assert (result.status, result.skip) == (PushStatus.SKIPPED, SkipReason.SETTINGS_CHANGED)
    assert git_world.events.events == []
    async with git_world.factory() as db:
        state = await load_state(db)
        pushes = (
            (await db.execute(select(AuditEntry).where(AuditEntry.action == "git.push")))
            .scalars()
            .all()
        )
    assert (state.failures, state.retry_at, state.last_error) == (0, None, None)
    assert pushes == []
