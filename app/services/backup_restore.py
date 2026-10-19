"""Restoring a backup over the running database.

The backup is first copied to a private working file and checked there (see ``backup_inspect``),
so a refused file changes nothing and the checked file is the one that is restored. A safety
backup of the current data is taken next; if it cannot be made, nothing is restored. The working
copy is then made ready: brought to the current schema if the backup is older, stripped of every
session and pending sign-in (so everyone must sign in again), given the records of the backups
that exist in the folder now, and given the audit entry of the restore, because the audit log of
the replaced database is replaced with it. Only then is it switched in, through SQLite's online
backup into the live database: that is one write transaction, so the live data is replaced whole
or not at all, connections of the running application and of other processes stay valid, and a
failure leaves the current data as it was. The safety backup keeps what the restore replaced.

A backup made before a key rotation is sealed with the older key. When that key is given, the
backup is checked against it and the working copy is re-keyed to the current key (see
``key_rotation``) before the switch, so the restored data is the same as any other restore's.
"""

import asyncio
import secrets
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.db import create_engine, create_session_factory
from app.core.log import get_logger
from app.core.migrations import upgrade_to_head
from app.models import AuditResult, Backup, PendingLogin, UserSession
from app.services.audit import AuditActor, record_actor_audit, record_audit
from app.services.backup import (
    BUSY_TIMEOUT_SECONDS,
    BackupSnapshotError,
    copy_database,
    create_backup,
)
from app.services.backup_inspect import (
    SQLITE_HEADER,
    BackupInspection,
    RestoreProblem,
    RestoreRefusedError,
    inspect_backup,
)
from app.services.backup_lock import BackupBusyError, hold_backup_lock
from app.services.backup_records import RecordSnapshot, merge_records, snapshot_records
from app.services.backup_run import BackupLocations
from app.services.key_rotation import rekey_database

log = get_logger("backup_restore")

ACTION_RESTORE = "backup.restore"
TARGET_TYPE = "backup"
REFUSALS = frozenset(
    {
        RestoreProblem.UNREADABLE,
        RestoreProblem.NOT_A_BACKUP,
        RestoreProblem.DAMAGED,
        RestoreProblem.WRONG_KEY,
        RestoreProblem.NOT_THE_OLD_KEY,
        RestoreProblem.NEWER,
    }
)


@dataclass(frozen=True)
class RestoreResult:
    """What a restore did: the safety backup, the sessions it ended and the old revision."""

    safety_backup: str
    sessions_ended: int
    upgraded_from: str | None


@dataclass(frozen=True)
class Current:
    """What the database being replaced held that must survive or be counted."""

    records: list[RecordSnapshot]
    sessions: int


def _stage_copy(source: Path, work: Path) -> None:
    """Copy the named file into the private working file, refusing what is not a database."""
    try:
        with source.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as error:
        raise RestoreRefusedError(RestoreProblem.UNREADABLE) from error
    if header != SQLITE_HEADER:
        raise RestoreRefusedError(RestoreProblem.NOT_A_BACKUP)
    try:
        copy_database(source, work, read_only=True)
    except sqlite3.OperationalError as error:
        raise RestoreRefusedError(RestoreProblem.UNREADABLE) from error
    except sqlite3.DatabaseError as error:
        raise RestoreRefusedError(RestoreProblem.DAMAGED) from error
    except (sqlite3.Error, OSError) as error:
        raise RestoreRefusedError(RestoreProblem.UNREADABLE) from error


async def _read_current(db: AsyncSession) -> Current:
    """The backup records and the session count of the live database, then end the read.

    The read is ended with a commit (sessions keep their loaded objects after one), so a held
    read snapshot cannot hold the switch back and the caller's objects stay usable.
    """
    rows = (await db.execute(select(Backup))).scalars().all()
    sessions = (await db.execute(select(func.count()).select_from(UserSession))).scalar_one()
    current = Current(snapshot_records(rows), int(sessions))
    await db.commit()
    return current


async def _prepare(
    work: Path,
    *,
    inspection: BackupInspection,
    current: Current,
    locations: BackupLocations,
    actor: AuditActor,
    label: str,
    safety_name: str,
    keys: tuple[CryptoService, CryptoService] | None,
) -> None:
    """Make the working copy ready to become the live database (see the module description).

    ``keys`` is the older key the backup was made with and the key in use now, for a backup made
    before a key rotation: everything the key protects is moved to the current key here, in the
    working copy, so the switch brings in data the current key opens.
    """
    engine = create_engine(work)
    try:
        if inspection.needs_upgrade:
            await upgrade_to_head(engine)
        async with create_session_factory(engine)() as session:
            if keys is not None:
                await rekey_database(session, *keys)
            await session.execute(delete(UserSession))
            await session.execute(delete(PendingLogin))
            await merge_records(session, current.records, locations.backup_dir)
            await record_audit(
                session,
                action=ACTION_RESTORE,
                result=AuditResult.SUCCESS,
                actor_type=actor.type,
                actor_name=actor.label,
                ip_address=actor.ip_address,
                target_type=TARGET_TYPE,
                target_name=label,
                details={
                    "backup": label,
                    "safety_backup": safety_name,
                    "sessions_ended": current.sessions,
                    "upgraded_from": inspection.revision if inspection.needs_upgrade else None,
                    "older_key": keys is not None,
                },
            )
            await session.commit()
    finally:
        await engine.dispose()


def replace_database(prepared: Path, live: Path) -> None:
    """Replace the content of the live database with the prepared copy in one transaction.

    The page size of both must match: SQLite refuses to copy over a database in WAL mode
    otherwise, and a copy made by this application always matches.
    """
    with (
        closing(sqlite3.connect(prepared)) as source,
        closing(sqlite3.connect(live, timeout=BUSY_TIMEOUT_SECONDS)) as target,
    ):
        if (
            source.execute("PRAGMA page_size").fetchone()
            != target.execute("PRAGMA page_size").fetchone()
        ):
            raise RestoreRefusedError(RestoreProblem.FAILED)
        source.backup(target)


def _discard(work: Path) -> None:
    """Remove the working file and the side files SQLite may have left beside it."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        with suppress(OSError):
            Path(f"{work}{suffix}").unlink()


async def _audit_refusal(
    db: AsyncSession, actor: AuditActor, label: str, problem: RestoreProblem
) -> None:
    """Record a refused or failed restore in the live database; never fails the caller."""
    try:
        await record_actor_audit(
            db,
            actor,
            action=ACTION_RESTORE,
            result=AuditResult.BLOCKED if problem in REFUSALS else AuditResult.FAILURE,
            target_type=TARGET_TYPE,
            target_name=label,
            details={"backup": label, "reason": problem.value},
        )
        await db.commit()
    except Exception as error:
        log.error("the audit entry of a refused restore failed: %s", type(error).__name__)


async def restore_backup(
    db: AsyncSession,
    locations: BackupLocations,
    *,
    source: Path,
    label: str,
    crypto: CryptoService,
    actor: AuditActor,
    old_crypto: CryptoService | None = None,
) -> RestoreResult:
    """Replace the current data with the backup in ``source`` and end every session.

    ``label`` is the backup's file name, which the audit entry records instead of a path.
    ``old_crypto`` is the key of a backup made before a key rotation: the backup must then open
    with it, and its values and hashes are moved to ``crypto`` before the switch.
    Raises ``RestoreRefusedError`` with a fixed message when the backup is refused, the safety
    backup cannot be made, another backup or restore is running, or the switch fails; the
    current data is untouched in every one of those cases.
    """
    try:
        with hold_backup_lock(locations.lock_dir):
            return await _restore_locked(db, locations, source, label, crypto, actor, old_crypto)
    except BackupBusyError as error:
        raise RestoreRefusedError(RestoreProblem.BUSY) from error
    except BackupSnapshotError as error:
        raise RestoreRefusedError(RestoreProblem.FAILED) from error


async def _restore_locked(
    db: AsyncSession,
    locations: BackupLocations,
    source: Path,
    label: str,
    crypto: CryptoService,
    actor: AuditActor,
    old_crypto: CryptoService | None,
) -> RestoreResult:
    """The restore steps, with the backup lock held."""
    work = locations.database_path.with_name(f".restore-{secrets.token_hex(6)}.db")
    problem: RestoreProblem | None = None
    try:
        await asyncio.to_thread(_stage_copy, source, work)
        inspection = await asyncio.to_thread(
            inspect_backup,
            work,
            old_crypto or crypto,
            RestoreProblem.NOT_THE_OLD_KEY if old_crypto else RestoreProblem.WRONG_KEY,
        )
        current = await _read_current(db)
        try:
            safety = await create_backup(
                locations.database_path, locations.backup_dir, "pre-restore"
            )
        except BackupSnapshotError as error:
            raise RestoreRefusedError(RestoreProblem.SAFETY_FAILED) from error
        await _prepare(
            work,
            inspection=inspection,
            current=current,
            locations=locations,
            actor=actor,
            label=label,
            safety_name=safety.name,
            keys=(old_crypto, crypto) if old_crypto else None,
        )
        await asyncio.to_thread(replace_database, work, locations.database_path)
    except RestoreRefusedError as error:
        problem = error.problem
        raise
    except Exception as error:
        problem = RestoreProblem.FAILED
        log.error("a restore failed: %s", type(error).__name__)
        raise RestoreRefusedError(RestoreProblem.FAILED) from error
    finally:
        _discard(work)
        if problem is not None:
            await _audit_refusal(db, actor, label, problem)
    upgraded = inspection.revision if inspection.needs_upgrade else None
    log.info("the database was restored from %s", label)
    return RestoreResult(safety.name, current.sessions, upgraded)
