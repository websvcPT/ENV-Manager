"""The export: the change mark, write-once files, the key and layout changes, private modes."""

import json
import stat
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_session_factory
from app.models import AuditResult, Environment, Project
from app.seeds import SeedData
from app.services.app_settings import GitSettings
from app.services.audit import AuditActor, record_actor_audit
from app.services.git_export import (
    MANIFEST_FILE,
    META_FILE,
    PER_VERSION,
    SNAPSHOT,
    SNAPSHOT_FILE,
    VERSIONS_DIR,
    compute_mark,
    export_tree,
    meta_context,
    path_hash,
)
from app.services.versions import create_version


class Rig:
    """The pieces of an export: a database, a key, a working folder and a scratch folder."""

    def __init__(
        self,
        engine: AsyncEngine,
        crypto: CryptoService,
        database_path: Path,
        tmp_path: Path,
        seed: SeedData,
    ) -> None:
        """Hold the pieces; the folders are made empty."""
        self.engine = engine
        self.crypto = crypto
        self.database_path = database_path
        self.work = tmp_path / "work"
        self.scratch = tmp_path / "scratch"
        self.work.mkdir()
        self.scratch.mkdir()
        self.seed = seed
        self.factory = create_session_factory(engine)

    async def mark(self, **changes: object) -> str:
        """The change mark of the settings with these changes."""
        async with self.factory() as db:
            return await compute_mark(db, self.crypto, GitSettings(**changes))  # type: ignore[arg-type]

    async def export(self, layout: str = PER_VERSION, crypto: CryptoService | None = None):  # type: ignore[no-untyped-def]
        """Write the export of a layout into the working folder."""
        async with self.factory() as db:
            return await export_tree(
                db,
                crypto or self.crypto,
                self.work,
                GitSettings(export_format=layout),
                database_path=self.database_path,
                scratch=self.scratch,
            )

    def files(self) -> dict[str, bytes]:
        """Every file of the working folder by relative path."""
        return {
            str(path.relative_to(self.work)): path.read_bytes()
            for path in sorted(self.work.rglob("*"))
            if path.is_file()
        }


@pytest.fixture
def rig(
    seeded_database: AsyncEngine,
    crypto: CryptoService,
    database_path: Path,
    tmp_path: Path,
    seed_data: SeedData,
) -> Rig:
    """An export rig on the seeded database."""
    return Rig(seeded_database, crypto, database_path, tmp_path, seed_data)


async def test_the_mark_moves_when_a_version_or_the_structure_changes(rig: Rig) -> None:
    """A new version, a renamed file and a new project each move the mark; the same state not."""
    base = await rig.mark()
    assert await rig.mark() == base
    async with rig.factory() as db:
        await create_version(db, rig.crypto, rig.seed.editor.id, "A=1\n", author=None)
        await db.commit()
    after_version = await rig.mark()
    assert after_version != base
    async with rig.factory() as db:
        environment = await db.get(Environment, rig.seed.editor.id)
        assert environment is not None
        environment.name = ".env.renamed"
        await db.commit()
    after_rename = await rig.mark()
    assert after_rename not in {base, after_version}
    async with rig.factory() as db:
        db.add(Project(name="another"))
        await db.commit()
    assert await rig.mark() not in {base, after_version, after_rename}


async def test_the_mark_follows_where_and_how_the_export_goes_but_not_who_commits(
    rig: Rig,
) -> None:
    """The address, branch and layout are part of the mark; the author and the switch are not."""
    base = await rig.mark()
    assert await rig.mark(url="https://git.example.com/a.git") != base
    assert await rig.mark(url="https://git.example.com/a.git") != await rig.mark(
        url="https://git.example.com/b.git"
    )
    assert await rig.mark(branch="other") != base
    assert await rig.mark(export_format=SNAPSHOT) != base
    assert await rig.mark(author_name="Someone else", push_enabled=False) == base


async def test_the_audit_log_moves_the_mark_of_a_snapshot_only_and_not_for_its_own_entries(
    rig: Rig,
) -> None:
    """A snapshot holds the audit log, so an entry changes it; git and backup entries do not."""
    snapshot, per_version = await rig.mark(export_format=SNAPSHOT), await rig.mark()
    async with rig.factory() as db:
        for action in ("git.push", "git.test", "backup.create", "backup.download"):
            await record_actor_audit(
                db, AuditActor.cli(), action=action, result=AuditResult.SUCCESS
            )
        await db.commit()
    assert await rig.mark(export_format=SNAPSHOT) == snapshot
    async with rig.factory() as db:
        await record_actor_audit(
            db, AuditActor.cli(), action="settings.update", result=AuditResult.SUCCESS
        )
        await db.commit()
    assert await rig.mark(export_format=SNAPSHOT) != snapshot
    assert await rig.mark() == per_version


async def test_a_second_export_of_the_same_state_writes_and_changes_nothing(rig: Rig) -> None:
    """Version files are written once and the metadata only when it changed, so an unchanged
    export leaves every file byte for byte as it was."""
    first = await rig.export()
    before = rig.files()
    second = await rig.export()
    assert first.files_written > 0 and first.rebuilt
    assert (second.files_written, second.rebuilt) == (0, False)
    assert rig.files() == before


async def test_a_new_version_and_a_rename_add_and_rewrite_only_what_they_touch(rig: Rig) -> None:
    """A new version adds its file and rewrites that file's metadata; a rename rewrites the
    metadata of the files below it; every older version file stays as it was."""
    await rig.export()
    before = rig.files()
    async with rig.factory() as db:
        await create_version(db, rig.crypto, rig.seed.editor.id, "A=1\n", author=None)
        await db.commit()
    stats = await rig.export()
    after = rig.files()
    assert stats.files_written == 2
    changed = {path for path in after if before.get(path) != after[path]}
    folder = f"{VERSIONS_DIR}/{path_hash(rig.crypto, rig.seed.editor.id)}"
    assert changed == {f"{folder}/000004.enc", f"{folder}/{META_FILE}"}

    async with rig.factory() as db:
        environment = await db.get(Environment, rig.seed.editor.id)
        assert environment is not None
        environment.name = ".env.renamed"
        await db.commit()
    assert (await rig.export()).files_written == 1
    meta = json.loads(
        rig.crypto.decrypt(
            (rig.work / folder / META_FILE).read_text().strip(),
            meta_context(path_hash(rig.crypto, rig.seed.editor.id)),
        )
    )
    assert meta["environment"]["name"] == ".env.renamed"
    assert [item["number"] for item in meta["versions"]] == [1, 2, 3, 4]
    assert {path for path in rig.files() if path.endswith("000001.enc")} == {
        path for path in before if path.endswith("000001.enc")
    }


async def test_a_deleted_file_keeps_its_history_in_the_tree(rig: Rig) -> None:
    """The tree only grows: what was exported stays after the file is removed from the database."""
    await rig.export()
    folder = f"{VERSIONS_DIR}/{path_hash(rig.crypto, rig.seed.editor.id)}"
    kept = {path: data for path, data in rig.files().items() if path.startswith(folder)}
    assert kept
    async with rig.factory() as db:
        await db.execute(delete(Environment).where(Environment.id == rig.seed.editor.id))
        await db.commit()
    await rig.export()
    assert {path: data for path, data in rig.files().items() if path.startswith(folder)} == kept


async def test_a_new_key_writes_the_tree_again_under_new_paths(rig: Rig) -> None:
    """Paths and metadata are keyed, so under another key nothing of the old tree can stay."""
    await rig.export()
    old = set(rig.files())
    other = CryptoService(parse_master_key(generate_master_key()))
    stats = await rig.export(crypto=other)
    assert stats.rebuilt
    new = set(rig.files())
    old_folders = {path.split("/")[1] for path in old if path.startswith(VERSIONS_DIR)}
    new_folders = {path.split("/")[1] for path in new if path.startswith(VERSIONS_DIR)}
    assert old_folders and new_folders and not old_folders & new_folders
    manifest = (rig.work / MANIFEST_FILE).read_text().strip()
    with pytest.raises(Exception, match="authentication"):
        rig.crypto.decrypt(manifest, b"git-export:manifest")
    assert other.decrypt(manifest, b"git-export:manifest")


async def test_changing_the_layout_removes_the_files_of_the_other_one(rig: Rig) -> None:
    """The working tree only ever holds one layout; the history of the repository keeps the rest."""
    await rig.export(PER_VERSION)
    assert any(path.startswith(VERSIONS_DIR) for path in rig.files())
    stats = await rig.export(SNAPSHOT)
    assert stats.rebuilt
    assert set(rig.files()) == {MANIFEST_FILE, SNAPSHOT_FILE}
    again = await rig.export(PER_VERSION)
    assert again.rebuilt and SNAPSHOT_FILE not in rig.files()


async def test_the_snapshot_is_replaced_each_time_and_leaves_no_scratch_file(rig: Rig) -> None:
    """One file, sealed anew (a fresh nonce) at every export, with nothing left in scratch."""
    await rig.export(SNAPSHOT)
    first = rig.files()[SNAPSHOT_FILE]
    await rig.export(SNAPSHOT)
    assert rig.files()[SNAPSHOT_FILE] != first
    assert list(rig.scratch.iterdir()) == []
    assert not [name for name in rig.files() if name.startswith(".part")]


async def test_every_file_and_folder_of_the_export_is_private(rig: Rig) -> None:
    """Files are 0600 and folders 0700, whatever the process umask."""
    await rig.export()
    for path in rig.work.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path
    snapshot = rig.work.parent / "snapshot-work"
    snapshot.mkdir()
    async with rig.factory() as db:
        await export_tree(
            db,
            rig.crypto,
            snapshot,
            GitSettings(export_format=SNAPSHOT),
            database_path=rig.database_path,
            scratch=rig.scratch,
        )
    assert stat.S_IMODE((snapshot / SNAPSHOT_FILE).stat().st_mode) == 0o600


async def test_no_environment_means_a_manifest_and_nothing_else(
    engine: AsyncEngine, crypto: CryptoService, database_path: Path, tmp_path: Path
) -> None:
    """A database with no files exports a sealed manifest only, and an empty versions folder."""
    work, scratch = tmp_path / "work", tmp_path / "scratch"
    work.mkdir()
    scratch.mkdir()
    async with create_session_factory(engine)() as db:
        stats = await export_tree(
            db, crypto, work, GitSettings(), database_path=database_path, scratch=scratch
        )
    assert stats.rebuilt
    assert sorted(path.name for path in work.iterdir()) == [MANIFEST_FILE, VERSIONS_DIR]
    assert list((work / VERSIONS_DIR).iterdir()) == []
