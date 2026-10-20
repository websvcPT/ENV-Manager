"""The encrypted export that is pushed to git, and the mark that says whether it changed.

Nothing readable leaves the application. Two layouts exist:

* ``per_version`` writes, for every version of every file, the ciphertext the database already
  holds (``versions/<path hash>/<number>.enc``, written once and never touched again, so the
  tree only grows) and one encrypted metadata file per file (``meta.enc``: the names of the
  project, deployment, service and file, their paths, the type of the file, and for each version
  its number, author, comment, time and keyed content hash). The path hash is a keyed hash of
  the file's id, so a name cannot be guessed from it without the key.
* ``snapshot`` writes ``snapshot.enc``: the same consistent snapshot a backup takes, sealed as a
  whole with the master key, since a plain snapshot holds names, paths and user names in clear.

A sealed ``manifest.enc`` names the layout and tells, by whether it opens with the current key,
whether the tree belongs to this key. After a key change or a layout change the tree is written
again from scratch, so no file sealed with another key stays in the working tree (the history of
the repository keeps what was pushed before).

The change mark is a keyed hash of cheap facts that move whenever the export would: counts,
largest ids and newest update times of the versions and of the structure, for a snapshot also
the audit log (without the entries git sync and backups write about themselves, or every push
would cause the next).
"""

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.models import (
    AuditEntry,
    Deployment,
    Environment,
    EnvironmentVersion,
    FileType,
    Project,
    Service,
    User,
)
from app.services.app_settings import GitSettings
from app.services.backup import BackupSnapshotError, write_snapshot
from app.services.git_errors import GitProblem, GitSyncError

FORMAT_VERSION = 1
# Raised whenever the metadata of a file gains a field, so the mark changes and the next hourly
# run pushes the rewritten metadata once, although no file, version or setting changed.
META_REVISION = 2
PER_VERSION = "per_version"
SNAPSHOT = "snapshot"
EXPORT_FORMATS = (PER_VERSION, SNAPSHOT)
MANIFEST_FILE = "manifest.enc"
VERSIONS_DIR = "versions"
META_FILE = "meta.enc"
SNAPSHOT_FILE = "snapshot.enc"
PATH_DOMAIN = "git-export-path"
MARK_DOMAIN = "git-export-mark"
MANIFEST_CONTEXT = b"git-export:manifest"
SNAPSHOT_CONTEXT = b"git-export:snapshot"
FILE_MODE = 0o600
FOLDER_MODE = 0o700
CONTENT_BATCH = 100
# Audit entries that git sync and backups write about their own runs do not move the mark.
SELF_AUDIT_PREFIXES = ("git.", "backup.")


@dataclass(frozen=True)
class ExportStats:
    """What an export wrote into the working folder."""

    files_written: int
    rebuilt: bool


def meta_context(path_hash: str) -> bytes:
    """The authenticated context that ties a metadata file to the folder it lives in."""
    return f"git-export:meta:{path_hash}".encode()


def path_hash(crypto: CryptoService, environment_id: int) -> str:
    """The folder name of a file: a keyed hash of its id."""
    return crypto.keyed_hash(PATH_DOMAIN, f"environment:{environment_id}")


async def compute_mark(db: AsyncSession, crypto: CryptoService, git: GitSettings) -> str:
    """A keyed hash that changes whenever the export of these settings would change."""
    versions = (
        await db.execute(
            select(
                func.count(EnvironmentVersion.id),
                func.coalesce(func.max(EnvironmentVersion.id), 0),
                func.coalesce(func.sum(EnvironmentVersion.id), 0),
                func.max(EnvironmentVersion.created_at),
            )
        )
    ).one()
    parts: list[object] = [git.export_format, git.url, git.branch, META_REVISION, *versions]
    for model in (Project, Deployment, Service, Environment):
        row = (
            await db.execute(
                select(func.count(model.id), func.max(model.id), func.max(model.updated_at))
            )
        ).one()
        parts.extend(row)
    if git.export_format == SNAPSHOT:
        query = select(func.count(AuditEntry.id), func.max(AuditEntry.id))
        for prefix in SELF_AUDIT_PREFIXES:
            query = query.where(~AuditEntry.action.startswith(prefix, autoescape=True))
        parts.extend((await db.execute(query)).one())
    return crypto.keyed_hash(MARK_DOMAIN, json.dumps(parts, default=str))


def _write_file(path: Path, text: str) -> None:
    """Write a file whole or not at all: a private temporary file renamed into place."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".part-")
    try:
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def _read_sealed(crypto: CryptoService, path: Path, context: bytes) -> bytes | None:
    """The plaintext of a sealed file, None when it is missing or does not open with this key."""
    try:
        return crypto.decrypt(path.read_text(encoding="ascii").strip(), context)
    except OSError, UnicodeDecodeError, CryptoError:
        return None


def _manifest_text(layout: str) -> bytes:
    """The plaintext of the manifest for a layout."""
    return json.dumps({"format": FORMAT_VERSION, "layout": layout}, sort_keys=True).encode()


def _prepare_tree(crypto: CryptoService, work: Path, layout: str) -> bool:
    """Make the working folder ready for a layout. Returns whether the tree was started over.

    A manifest that does not open with this key or names another layout means the files in the
    tree are not ours to extend: they are removed and the manifest is written again.
    """
    manifest = work / MANIFEST_FILE
    if _read_sealed(crypto, manifest, MANIFEST_CONTEXT) == _manifest_text(layout):
        return False
    shutil.rmtree(work / VERSIONS_DIR, ignore_errors=True)
    (work / SNAPSHOT_FILE).unlink(missing_ok=True)
    _write_file(manifest, crypto.encrypt(_manifest_text(layout), MANIFEST_CONTEXT))
    return True


@dataclass(frozen=True)
class _FileRow:
    """A managed file with the names above it."""

    environment_id: int
    environment_name: str
    target_path: str
    dist_path: str | None
    file_type: FileType
    service_id: int
    service_name: str
    deployment_id: int
    deployment_name: str
    root_path: str
    project_id: int
    project_name: str


async def _file_rows(db: AsyncSession) -> list[_FileRow]:
    """Every managed file with its service, deployment and project, in id order."""
    rows = await db.execute(
        select(
            Environment.id,
            Environment.name,
            Environment.target_path,
            Environment.dist_path,
            Environment.file_type,
            Service.id,
            Service.name,
            Deployment.id,
            Deployment.name,
            Deployment.root_path,
            Project.id,
            Project.name,
        )
        .join(Service, Environment.service_id == Service.id)
        .join(Deployment, Service.deployment_id == Deployment.id)
        .join(Project, Deployment.project_id == Project.id)
        .order_by(Environment.id)
    )
    return [_FileRow(*tuple(row)) for row in rows.all()]


async def _version_rows(db: AsyncSession) -> dict[int, list[dict[str, object]]]:
    """The metadata of every version by file id, oldest first. No content is read."""
    rows = await db.execute(
        select(
            EnvironmentVersion.environment_id,
            EnvironmentVersion.number,
            User.name,
            EnvironmentVersion.comment,
            EnvironmentVersion.created_at,
            EnvironmentVersion.content_hash,
        )
        .outerjoin(User, EnvironmentVersion.author_id == User.id)
        .order_by(EnvironmentVersion.environment_id, EnvironmentVersion.number)
    )
    grouped: dict[int, list[dict[str, object]]] = {}
    for environment_id, number, author, comment, created_at, content_hash in rows.all():
        grouped.setdefault(environment_id, []).append(
            {
                "number": number,
                "author": author,
                "comment": comment,
                "created_at": created_at.isoformat(),
                "content_hash": content_hash,
            }
        )
    return grouped


def _meta_text(row: _FileRow, versions: list[dict[str, object]]) -> bytes:
    """The plaintext metadata of one file, in a fixed layout so equal data reads equal."""
    return json.dumps(
        {
            "format": FORMAT_VERSION,
            "project": {"id": row.project_id, "name": row.project_name},
            "deployment": {
                "id": row.deployment_id,
                "name": row.deployment_name,
                "root_path": row.root_path,
            },
            "service": {"id": row.service_id, "name": row.service_name},
            "environment": {
                "id": row.environment_id,
                "name": row.environment_name,
                "target_path": row.target_path,
                "dist_path": row.dist_path,
                "file_type": row.file_type.value,
            },
            "versions": versions,
        },
        sort_keys=True,
    ).encode()


async def _write_missing_versions(
    db: AsyncSession, folder: Path, environment_id: int, numbers: list[int]
) -> None:
    """Write the stored ciphertext of the versions that have no file yet, a batch at a time."""
    for start in range(0, len(numbers), CONTENT_BATCH):
        batch = numbers[start : start + CONTENT_BATCH]
        rows = await db.execute(
            select(EnvironmentVersion.number, EnvironmentVersion.content_encrypted).where(
                EnvironmentVersion.environment_id == environment_id,
                EnvironmentVersion.number.in_(batch),
            )
        )
        for number, sealed in rows.all():
            _write_file(folder / f"{number:06d}.enc", sealed + "\n")


def _existing_numbers(folder: Path) -> set[int]:
    """The version numbers that already have a file in a folder."""
    return {int(path.stem) for path in folder.glob("*.enc") if path.stem.isdigit()}


async def _export_per_version(db: AsyncSession, crypto: CryptoService, work: Path) -> ExportStats:
    """Write the per-version layout into the working folder."""
    rebuilt = _prepare_tree(crypto, work, PER_VERSION)
    written = 1 if rebuilt else 0
    versions = await _version_rows(db)
    (work / VERSIONS_DIR).mkdir(mode=FOLDER_MODE, exist_ok=True)
    for row in await _file_rows(db):
        name = path_hash(crypto, row.environment_id)
        folder = work / VERSIONS_DIR / name
        folder.mkdir(mode=FOLDER_MODE, exist_ok=True)
        file_versions = versions.get(row.environment_id, [])
        missing = sorted(
            {int(str(item["number"])) for item in file_versions} - _existing_numbers(folder)
        )
        await _write_missing_versions(db, folder, row.environment_id, missing)
        written += len(missing)
        meta = _meta_text(row, file_versions)
        if _read_sealed(crypto, folder / META_FILE, meta_context(name)) != meta:
            _write_file(folder / META_FILE, crypto.encrypt(meta, meta_context(name)) + "\n")
            written += 1
    return ExportStats(written, rebuilt)


def _seal_snapshot(source: Path, crypto: CryptoService, scratch: Path, target: Path) -> None:
    """Take a snapshot of the database, seal it as a whole and write it to ``target``."""
    folder = Path(tempfile.mkdtemp(prefix="snapshot-", dir=scratch))
    try:
        copy = folder / "snapshot.db"
        write_snapshot(source, copy)
        _write_file(target, crypto.encrypt(copy.read_bytes(), SNAPSHOT_CONTEXT) + "\n")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def _export_snapshot(
    crypto: CryptoService, work: Path, source: Path, scratch: Path
) -> ExportStats:
    """Write the snapshot layout into the working folder, replacing the previous file."""
    rebuilt = _prepare_tree(crypto, work, SNAPSHOT)
    try:
        await asyncio.to_thread(_seal_snapshot, source, crypto, scratch, work / SNAPSHOT_FILE)
    except BackupSnapshotError as error:
        raise GitSyncError(GitProblem.EXPORT) from error
    return ExportStats(2 if rebuilt else 1, rebuilt)


async def export_tree(
    db: AsyncSession,
    crypto: CryptoService,
    work: Path,
    git: GitSettings,
    *,
    database_path: Path,
    scratch: Path,
) -> ExportStats:
    """Write the export of the chosen layout into the working folder.

    Raises ``GitSyncError`` (problem ``export``) when files cannot be written or the snapshot
    cannot be taken.
    """
    try:
        if git.export_format == SNAPSHOT:
            return await _export_snapshot(crypto, work, database_path, scratch)
        return await _export_per_version(db, crypto, work)
    except OSError as error:
        raise GitSyncError(GitProblem.EXPORT) from error
