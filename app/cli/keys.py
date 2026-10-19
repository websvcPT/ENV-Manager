"""The ``key`` commands: rotating the master key, offline."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.notify import cli_notifier
from app.cli.session import run_command
from app.config import Settings, get_settings
from app.core.crypto import CryptoError, CryptoService, load_master_key
from app.core.log import get_logger
from app.models import BackupStatus, BackupTrigger
from app.services.audit import AuditActor
from app.services.backup_lock import BackupBusyError, hold_backup_lock
from app.services.backup_run import BackupLocations, run_backup
from app.services.key_check import ensure_key_check
from app.services.key_files import NewKey, prepare_new_key
from app.services.key_rotation import RekeyReport, RotationError, rotate_master_key
from app.services.notify_events import MasterKeyRotated, announce_event
from app.services.server_lock import (
    ServerLockBusyError,
    ServerLockUnavailableError,
    acquire_server_lock,
)

log = get_logger("cli_key")

key_app = typer.Typer(help="Master key operations.", no_args_is_help=True)


@dataclass(frozen=True)
class RotationOutcome:
    """What a rotation did: the counts, the backup taken first and the key it moved to."""

    report: RekeyReport
    backup: Path
    backup_status: BackupStatus
    backup_error: str | None
    new_key: NewKey


class RotationRefusedError(Exception):
    """The rotation did not start or did not finish; the message is safe to show."""


def _kept(new_key_file: Path) -> str:
    """A sentence saying the new key file is still there and the next run uses it, if it is."""
    if not new_key_file.is_file():
        return ""
    return f" The new key file {new_key_file} is still there and the next run uses it."


def _fail(message: str) -> typer.Exit:
    """Print an error line and return the exit that ends the command with code 1."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


async def _rotate(
    db: AsyncSession, settings: Settings, old: CryptoService, current: bytes, new_key_file: Path
) -> RotationOutcome:
    """The steps of a rotation, in the order that keeps a refusal free of side effects.

    The current key must open the data; the new key file must be usable (it is created only after
    that); a backup is taken (a failed one stops everything); and only then does the one
    transaction run, under the backup lock so no backup or restore starts meanwhile. Superadmins
    are told afterwards, through the new key.
    """
    await ensure_key_check(db, old)
    new_key = prepare_new_key(new_key_file, current)
    locations = BackupLocations.from_settings(settings)
    run = await run_backup(
        db,
        locations,
        trigger=BackupTrigger.ROTATION,
        actor=AuditActor.cli(),
        notifier=cli_notifier(db),
    )
    record = run.record
    if record.status is BackupStatus.FAILURE:
        raise RotationRefusedError(f"the backup taken first failed ({record.error})")
    new = CryptoService(new_key.key)
    with hold_backup_lock(locations.lock_dir):
        report = await rotate_master_key(
            db, old=old, new=new, actor=AuditActor.cli(), backup=record.filename
        )
    await announce_event(
        cli_notifier(db, new),
        MasterKeyRotated(report.versions, report.totp_secrets + report.setting_secrets, "cli"),
    )
    return RotationOutcome(
        report,
        locations.backup_dir / record.filename,
        record.status,
        record.error,
        new_key,
    )


def _print_outcome(outcome: RotationOutcome, new_key_file: Path) -> None:
    """The counts, the backup, and the steps that are left."""
    report = outcome.report
    typer.echo(
        f"key rotated: {report.versions} versions, {report.totp_secrets} second factor secrets "
        f"and {report.setting_secrets} secret settings were sealed again with the new key"
    )
    typer.echo(
        f"{report.deploy_hashes_recomputed} deploy hashes were recomputed, "
        f"{report.deploy_hashes_dropped} matched no stored version and were dropped, "
        f"{report.seen_addresses_cleared} remembered sign-in addresses were cleared"
    )
    typer.echo(f"backup taken first, sealed with the OLD key: {outcome.backup}")
    if outcome.backup_status is BackupStatus.PARTIAL:
        typer.echo(
            f"warning: the backup was not copied to the extra destination: {outcome.backup_error}",
            err=True,
        )
    state = "created" if outcome.new_key.created else "read"
    typer.echo(f"new key file ({state}): {new_key_file}")
    typer.echo("")
    typer.echo("Steps left:")
    typer.echo(
        "  1. Point ENVMANAGER_KEY_FILE at the new key file. If the key came from "
        "ENVMANAGER_KEY, remove that variable: the application refuses to start with both."
    )
    typer.echo("  2. Start the application. It refuses to start with the old key from now on.")
    typer.echo(
        "  3. Keep the old key in your secret store, apart from the backup files and labelled "
        "with the backups it opens. To restore one of them: "
        "envmanager backup restore <file> --key-file <old key file>."
    )
    if report.git_progress_reset:
        typer.echo(
            "  4. The next git sync run pushes the whole export again under the new key. "
            "Earlier commits stay in the repository and open only with the old key, so keep "
            "the old key as long as they are kept."
        )
    if outcome.new_key.created:
        typer.echo("  5. Copy the new key to your secret store now. This file is the only copy.")


@key_app.command("rotate")
def rotate_key(
    new_key_file: Annotated[
        Path,
        typer.Option(
            "--new-key-file",
            help=(
                "Where the new master key is, or will be written (mode 0600) when the file "
                "does not exist. Its folder must exist and must outlive this command."
            ),
        ),
    ],
) -> None:
    """Re-encrypt everything with a new master key, offline.

    Stop the application first: the command refuses to run while a server holds the data folder.
    A backup is taken first, then every sealed value is moved to the new key and every keyed hash
    is recomputed in one transaction, so a failure leaves the database as it was. The command
    prints the steps left (point ENVMANAGER_KEY_FILE at the new key, start the application).
    """
    settings = get_settings()
    try:
        lock = acquire_server_lock(settings.server_lock_file, "key rotation")
    except ServerLockBusyError as error:
        holder = f" ({error.holder})" if error.holder else ""
        raise _fail(
            f"the data folder is in use by a running server or another rotation{holder}. "
            "Stop the application first, then run this command again."
        ) from error
    except ServerLockUnavailableError as error:
        raise _fail("the lock file in the data folder cannot be used") from error
    try:
        current = load_master_key(settings.key, settings.key_file)
        old = CryptoService(current)

        async def operation(db: AsyncSession) -> RotationOutcome:
            return await _rotate(db, settings, old, current, new_key_file)

        outcome = run_command(operation)
    except typer.Exit:
        raise
    except (CryptoError, RotationError, RotationRefusedError) as error:
        reason = str(error).rstrip(".")
        raise _fail(f"{reason}. The database was not changed.{_kept(new_key_file)}") from error
    except BackupBusyError as error:
        raise _fail("another backup or restore is running. Nothing was changed.") from error
    except Exception as error:
        log.error("the key rotation failed: %s", type(error).__name__)
        raise _fail(
            f"the rotation failed ({type(error).__name__}) and was rolled back, so the database "
            f"is as it was.{_kept(new_key_file)}"
        ) from error
    finally:
        lock.release()
    _print_outcome(outcome, new_key_file)
