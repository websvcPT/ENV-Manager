"""The allowed path list: live checks, status rows, add and remove, audit and notification."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import AllowedPath, AuditEntry, Deployment, Project
from app.services import allowed_paths
from app.services.allowed_paths import (
    ACTION_ADD,
    ACTION_REMOVE,
    DUPLICATE_MESSAGE,
    MISSING_NOTE,
    MOVED_NOTE,
    STATE_MISSING,
    STATE_MOVED,
    STATE_OK,
    AllowedPathError,
    AllowedPathRow,
    EntryCheck,
    add_allowed_path,
    check_entry,
    list_allowed_paths,
    remove_allowed_path,
    uncovered_deployments,
)
from app.services.audit import AuditActor
from app.services.settings_events import SettingsChanged

DATA_PATH = Path("/data-volume-for-tests")


class RecordingNotifier:
    """A notifier that remembers events and the paths that were stored when they arrived."""

    def __init__(self, engine: AsyncEngine | None = None, *, fail: bool = False) -> None:
        """Remember the engine to read the stored list from and whether to raise."""
        self.events: list[SettingsChanged] = []
        self.stored_at_event: list[list[str]] = []
        self.engine = engine
        self.fail = fail

    async def settings_changed(self, event: SettingsChanged) -> None:
        """Record the event and the list as another session sees it."""
        self.events.append(event)
        if self.engine is not None:
            async with create_session_factory(self.engine)() as db:
                rows = (await db.execute(select(AllowedPath.path))).scalars()
                self.stored_at_event.append(sorted(rows))
        if self.fail:
            raise RuntimeError("the notifier is broken")


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A real folder that counts as a host mount, holding two sub folders."""
    root = (tmp_path / "host").resolve()
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    (root / "note.txt").write_text("not a folder", encoding="utf-8")
    return root


def check(raw: str, host: Path, *, existing: tuple[str, ...] = ()) -> EntryCheck:
    """Check an entry with the host folder as the only mount."""
    return check_entry(raw, data_path=DATA_PATH, existing=existing, mount_points=[host])


def test_an_empty_input_has_no_lines_and_cannot_be_added(host: Path) -> None:
    """The neutral hint shows until something is typed."""
    result = check("   ", host)
    assert result.lines == () and not result.ok and result.message is None


def test_a_good_entry_lists_what_was_checked(host: Path) -> None:
    """Existing, mounted and writable folders pass with the mount named."""
    result = check(f"{host}/alpha", host)
    assert result.ok and result.real_path == host / "alpha"
    assert [(line.ok, line.text) for line in result.lines] == [
        (True, "Absolute and normalized"),
        (True, "Exists"),
        (True, f"Inside the mount {host}, writable"),
    ]


def test_a_read_only_folder_is_accepted_and_said_to_be_read_only(host: Path) -> None:
    """Deployments cannot be created there, but the entry itself is valid."""
    folder = host / "alpha"
    folder.chmod(0o500)
    try:
        result = check(str(folder), host)
    finally:
        folder.chmod(0o700)
    assert result.ok
    assert result.lines[2].text == f"Inside the mount {host}, read-only"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("/", "The filesystem root cannot be an allowed path."),
        ("relative/path", "Path must be absolute."),
        ("{host}/alpha/../beta", "Path must not contain '..' components."),
        ("{host}/missing", "Path does not exist."),
        ("{host}/note.txt", "Path is not a directory."),
        ("/data-volume-for-tests/inner", "The application data volume cannot be an allowed path."),
    ],
)
def test_a_bad_entry_gets_one_failed_line_with_a_sentence(
    host: Path, raw: str, message: str
) -> None:
    """Each refusal is a single capitalised sentence ending in a full stop."""
    result = check(raw.format(host=host), host)
    assert not result.ok and result.real_path is None
    assert [(line.ok, line.text) for line in result.lines] == [(False, message)]
    assert result.message == message


def test_a_folder_outside_every_mount_is_refused(host: Path, tmp_path: Path) -> None:
    """A path that no host mount holds fails, even when it exists."""
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    result = check(str(elsewhere), host)
    assert [line.text for line in result.lines] == ["Path is not inside a mounted host directory."]


def test_a_path_already_in_the_list_is_refused_also_through_a_symlink(
    host: Path, tmp_path: Path
) -> None:
    """Duplicates are found on the real path, so an alias of an entry is the same entry."""
    alias = tmp_path / "alias"
    alias.symlink_to(host / "alpha")
    existing = (str(host / "alpha"),)
    for raw in (f"{host}/alpha", f"{host}/alpha/", str(alias)):
        result = check(raw, host, existing=existing)
        assert [line.text for line in result.lines] == [DUPLICATE_MESSAGE], raw
        assert result.real_path is None


async def add(
    engine: AsyncEngine,
    raw: str,
    host: Path,
    notifier: RecordingNotifier | None = None,
) -> AllowedPath:
    """Add an entry as the CLI actor with the host folder as the only mount."""
    async with create_session_factory(engine)() as db:
        return await add_allowed_path(
            db,
            raw,
            actor=AuditActor.cli(),
            data_path=DATA_PATH,
            notifier=notifier or RecordingNotifier(),
            mount_points=[host],
        )


async def stored_paths_of(engine: AsyncEngine) -> list[str]:
    """The stored entries, sorted."""
    async with create_session_factory(engine)() as db:
        return sorted((await db.execute(select(AllowedPath.path))).scalars())


async def audit_of(engine: AsyncEngine, action: str) -> list[AuditEntry]:
    """The audit entries of one action, oldest first."""
    async with create_session_factory(engine)() as db:
        return list(
            (
                await db.execute(
                    select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
                )
            ).scalars()
        )


async def test_adding_stores_the_real_path_audits_and_announces_after_the_commit(
    engine: AsyncEngine, host: Path, tmp_path: Path
) -> None:
    """The stored entry is the real path, one audit entry names it, and the notifier is told
    only once the entry is committed and visible to another session."""
    alias = tmp_path / "alias"
    alias.symlink_to(host / "alpha")
    notifier = RecordingNotifier(engine)
    entry = await add(engine, f"  {alias}/ ", host, notifier)
    assert entry.path == str(host / "alpha")
    assert await stored_paths_of(engine) == [str(host / "alpha")]
    (record,) = await audit_of(engine, ACTION_ADD)
    assert record.actor_name == "cli" and record.target_name == str(host / "alpha")
    assert record.target_type == "allowed_path" and record.details["path"] == str(host / "alpha")
    expected = SettingsChanged("allowed_paths", "added", str(host / "alpha"), "cli")
    assert notifier.events == [expected]
    assert notifier.stored_at_event == [[str(host / "alpha")]]


async def test_a_refused_entry_changes_and_audits_nothing(engine: AsyncEngine, host: Path) -> None:
    """A path that fails a check raises its message and leaves no row, entry or event."""
    notifier = RecordingNotifier()
    with pytest.raises(AllowedPathError, match=r"Path does not exist\."):
        await add(engine, f"{host}/missing", host, notifier)
    with pytest.raises(AllowedPathError, match=r"The filesystem root cannot be an allowed path\."):
        await add(engine, "/", host, notifier)
    assert await stored_paths_of(engine) == []
    assert await audit_of(engine, ACTION_ADD) == []
    assert notifier.events == []


async def test_adding_a_duplicate_is_refused(engine: AsyncEngine, host: Path) -> None:
    """The second add of a path says it is in the list already."""
    await add(engine, f"{host}/alpha", host)
    with pytest.raises(AllowedPathError, match="already in the list"):
        await add(engine, f"{host}/alpha", host)
    assert len(await audit_of(engine, ACTION_ADD)) == 1


async def test_a_concurrent_add_that_loses_the_race_is_a_duplicate(
    engine: AsyncEngine, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the unique index refuses a path the check let through, the answer is the duplicate
    refusal and the session stays usable."""
    await add(engine, f"{host}/alpha", host)
    real_check = allowed_paths.check_entry

    def forget_existing(raw: str, **kwargs: Any) -> EntryCheck:
        return real_check(raw, **{**kwargs, "existing": ()})

    monkeypatch.setattr(allowed_paths, "check_entry", forget_existing)
    async with create_session_factory(engine)() as db:
        with pytest.raises(AllowedPathError, match="already in the list"):
            await add_allowed_path(
                db,
                f"{host}/alpha",
                actor=AuditActor.cli(),
                data_path=DATA_PATH,
                notifier=RecordingNotifier(),
                mount_points=[host],
            )
        assert (await db.execute(select(AllowedPath))).scalars().all() != []
    assert len(await audit_of(engine, ACTION_ADD)) == 1


async def test_a_notifier_that_fails_never_fails_the_change(
    engine: AsyncEngine, host: Path
) -> None:
    """The change is committed and audited even when delivering the notification raises."""
    notifier = RecordingNotifier(fail=True)
    await add(engine, f"{host}/alpha", host, notifier)
    assert await stored_paths_of(engine) == [str(host / "alpha")]
    assert len(notifier.events) == 1


async def rows_by_name(engine: AsyncEngine, host: Path) -> dict[str, AllowedPathRow]:
    """The status rows of the list, by the last component of each path."""
    async with create_session_factory(engine)() as db:
        rows = await list_allowed_paths(db, mount_points=[host])
    return {Path(row.path).name: row for row in rows}


async def deployment_at(engine: AsyncEngine, name: str, root: Path) -> None:
    """A deployment named ``name`` in the first project at ``root``."""
    async with create_session_factory(engine)() as db:
        project = Project(name=f"p-{name}")
        db.add(project)
        await db.flush()
        db.add(Deployment(project_id=project.id, name=name, root_path=str(root)))
        await db.commit()


async def test_rows_show_status_and_the_deployments_under_each_entry(
    engine: AsyncEngine, host: Path, tmp_path: Path
) -> None:
    """Ok, missing and moved entries each read as such, with the count of deployments under them."""
    await add(engine, f"{host}/alpha", host)
    await add(engine, f"{host}/beta", host)
    await deployment_at(engine, "one", host / "alpha" / "web")
    await deployment_at(engine, "two", host / "alpha")
    await deployment_at(engine, "three", host / "beta" / "api")
    await deployment_at(engine, "four", tmp_path / "nowhere")
    rows = await rows_by_name(engine, host)
    assert rows["alpha"].state == STATE_OK and rows["alpha"].mounted and rows["alpha"].writable
    assert rows["alpha"].deployments == 2 and rows["beta"].deployments == 1
    assert rows["alpha"].note is None

    (host / "beta").rmdir()
    (host / "beta").symlink_to(host / "alpha")
    (host / "alpha" / "inside").mkdir()
    rows = await rows_by_name(engine, host)
    assert rows["beta"].state == STATE_MOVED and rows["beta"].note == MOVED_NOTE
    assert not rows["beta"].mounted and not rows["beta"].writable

    (host / "alpha").rename(host / "alpha-gone")
    rows = await rows_by_name(engine, host)
    assert rows["alpha"].state == STATE_MISSING and rows["alpha"].note == MISSING_NOTE


async def test_a_read_only_entry_reads_as_mounted_but_not_writable(
    engine: AsyncEngine, host: Path
) -> None:
    """The writable column follows the folder permissions."""
    await add(engine, f"{host}/alpha", host)
    (host / "alpha").chmod(0o500)
    try:
        async with create_session_factory(engine)() as db:
            (row,) = await list_allowed_paths(db, mount_points=[host])
    finally:
        (host / "alpha").chmod(0o700)
    assert row.state == STATE_OK and row.mounted and not row.writable


async def test_removing_deletes_only_the_entry_and_counts_the_deployments_left_outside(
    engine: AsyncEngine, host: Path
) -> None:
    """The folder and its files stay; the audit entry says how many deployments lose their path."""
    await add(engine, f"{host}/alpha", host)
    await add(engine, f"{host}/beta", host)
    await deployment_at(engine, "one", host / "alpha" / "web")
    (host / "alpha" / "keep.txt").write_text("stay", encoding="utf-8")
    async with create_session_factory(engine)() as db:
        entries = (await db.execute(select(AllowedPath))).scalars()
        entry = next(e for e in entries if "alpha" in e.path)
        entry_id = entry.id
    notifier = RecordingNotifier(engine)
    async with create_session_factory(engine)() as db:
        await remove_allowed_path(db, entry_id, actor=AuditActor.cli(), notifier=notifier)
    assert await stored_paths_of(engine) == [str(host / "beta")]
    assert (host / "alpha" / "keep.txt").read_text(encoding="utf-8") == "stay"
    (record,) = await audit_of(engine, ACTION_REMOVE)
    assert record.details == {"path": str(host / "alpha"), "deployments_outside": 1}
    expected = SettingsChanged("allowed_paths", "removed", str(host / "alpha"), "cli")
    assert notifier.events == [expected]
    assert notifier.stored_at_event == [[str(host / "beta")]]


async def test_a_deployment_that_another_entry_covers_is_not_flagged(
    engine: AsyncEngine, host: Path
) -> None:
    """With a parent entry in the list, removing the inner one leaves the deployment allowed."""
    await add(engine, str(host), host)
    await add(engine, f"{host}/alpha", host)
    await deployment_at(engine, "one", host / "alpha" / "web")
    await deployment_at(engine, "two", host / "beta" / "web")
    async with create_session_factory(engine)() as db:
        entries = list((await db.execute(select(AllowedPath))).scalars())
        inner = next(e for e in entries if e.path.endswith("alpha"))
        outer = next(e for e in entries if e.path == str(host))
        assert await uncovered_deployments(db, inner, entries) == 0
        assert await uncovered_deployments(db, outer, entries) == 1


async def test_removing_an_entry_that_is_gone_says_so(engine: AsyncEngine) -> None:
    """A second removal of the same id is refused and audits nothing."""
    async with create_session_factory(engine)() as db:
        with pytest.raises(AllowedPathError, match="no longer in the list"):
            await remove_allowed_path(db, 999, actor=AuditActor.cli(), notifier=RecordingNotifier())
    assert await audit_of(engine, ACTION_REMOVE) == []


def test_the_data_volume_is_refused_for_real_paths(tmp_path: Path) -> None:
    """The volume itself and anything that contains it cannot be allowed."""
    data = (tmp_path / "data").resolve()
    (data / "backups").mkdir(parents=True)
    for raw in (str(data), str(data / "backups"), str(tmp_path)):
        result = check_entry(raw, data_path=data, mount_points=[tmp_path])
        assert result.message == "The application data volume cannot be an allowed path.", raw
