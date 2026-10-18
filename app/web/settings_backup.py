"""The Backup tab: schedule and retention, the list of backups, Run now, Download and Restore.

Superadmin only: every route holds the settings capability, so any other group gets 403. Saving
the form reschedules the backup job at once. A backup taken by Run now answers the refreshed
list and sends the banner area again, because the result can set or clear the failed-backup
banner. Download streams the file from the backup folder with no caching and is audited. Restore
asks for confirmation in a dialog that names the backup, then replaces the data and ends every
session, the caller's own included, so the answer sends the browser to the sign-in page.
"""

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.display_time import display_of
from app.core.sessions import SESSION_COOKIE_NAME
from app.core.templating import render
from app.models import AuditResult, Backup, BackupStatus, BackupTrigger
from app.services.app_settings import BackupSettings, StoredSettings
from app.services.audit import AuditActor, record_actor_audit
from app.services.backup_events import BackupNotifier
from app.services.backup_files import (
    BackupFileMissingError,
    backup_file_size,
    open_backup_file,
    stream_backup,
)
from app.services.backup_inspect import RestoreProblem, RestoreRefusedError
from app.services.backup_lock import BackupBusyError
from app.services.backup_records import HAS_FILE, get_backup
from app.services.backup_restore import restore_backup
from app.services.backup_run import BackupLocations, run_backup
from app.services.backup_schedule import BackupScheduler
from app.services.backup_settings import (
    BackupForm,
    BackupFormError,
    retention_options,
    save_backup,
    schedule_options,
    validate_backup,
)
from app.services.backup_view import extra_destination_problem, format_size, load_backup_list
from app.services.paths import load_allowed_paths
from app.services.shell_banners import load_shell_banners
from app.web.dependencies import get_crypto, get_db, get_stored_settings
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_backup.html"
LIST_FRAGMENT = "fragments/backup_list.html"
RUN_FRAGMENT = "fragments/backup_run_result.html"
RESTORE_FRAGMENT = "fragments/backup_restore.html"
DOWNLOAD_TYPE = "application/vnd.sqlite3"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]

REFUSAL_STATUS = {
    RestoreProblem.BUSY: status.HTTP_409_CONFLICT,
    RestoreProblem.SAFETY_FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
    RestoreProblem.FAILED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _locations(request: Request) -> BackupLocations:
    """Where the application keeps its database, backups and locks."""
    locations: BackupLocations = request.app.state.backup_locations
    return locations


def _scheduler(request: Request) -> BackupScheduler:
    """The application's scheduler."""
    scheduler: BackupScheduler = request.app.state.backup_scheduler
    return scheduler


def _notifier(request: Request) -> BackupNotifier:
    """The application's backup notifier."""
    notifier: BackupNotifier = request.app.state.backup_notifier
    return notifier


def _actor(request: Request, context: ManageSettings) -> AuditActor:
    """The signed-in superadmin as the actor of an audit entry."""
    return AuditActor.for_user(context.user, client_address(request))


async def _form_context(
    request: Request,
    db: AsyncSession,
    settings: BackupSettings,
    form: BackupForm,
    *,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the settings form needs: values, refusals, the choices and the toast."""
    allowed = await load_allowed_paths(db)
    return {
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "schedule_options": schedule_options(),
        "retention_options": retention_options(settings),
        "zone": display_of(request).label,
        "destination_problem": extra_destination_problem(settings, allowed),
    }


@router.get("/settings/backup", include_in_schema=False)
async def backup_page(
    request: Request, db: Database, _context: ManageSettings, stored: Stored
) -> Response:
    """The Backup tab."""
    form = BackupForm.from_settings(stored.backup)
    page = {
        **tab_context("backup"),
        **await _form_context(request, db, stored.backup, form),
        "backups": await load_backup_list(db, _locations(request).backup_dir),
    }
    return render(request, "pages/settings_backup.html", page)


@router.post("/settings/backup", include_in_schema=False)
async def save_backup_route(
    request: Request,
    db: Database,
    context: ManageSettings,
    stored: Stored,
    schedule: Annotated[str, Form()] = "",
    retention: Annotated[str, Form()] = "",
    extra_destination: Annotated[str, Form()] = "",
) -> Response:
    """Save the backup settings and reschedule. A refused value answers the form again (422)."""
    form = BackupForm(schedule, retention, extra_destination)
    allowed = await load_allowed_paths(db)
    try:
        clean = validate_backup(form, stored=stored.backup, allowed=allowed)
    except BackupFormError as error:
        page = await _form_context(request, db, stored.backup, form, errors=error.errors)
        return render(request, FORM_FRAGMENT, page, status_code=422)
    changes = await save_backup(db, clean, actor=_actor(request, context))
    await _scheduler(request).reload()
    if changes:
        saved = {
            "tone": "success",
            "title": "Backup settings saved",
            "message": "The schedule and retention apply now.",
        }
    else:
        saved = {
            "tone": "info",
            "title": "Nothing to save",
            "message": "The settings are unchanged.",
        }
    page = await _form_context(request, db, clean, BackupForm.from_settings(clean), saved=saved)
    return render(request, FORM_FRAGMENT, page)


def _result_toast(
    outcome: BackupStatus, size_bytes: int | None, error: str | None
) -> dict[str, str]:
    """The toast that reports a backup taken by Run now."""
    if outcome is BackupStatus.SUCCESS:
        size = format_size(size_bytes) if size_bytes is not None else "unknown size"
        return {"tone": "success", "title": "Backup created", "message": f"The backup is {size}."}
    if outcome is BackupStatus.PARTIAL:
        return {
            "tone": "warning",
            "title": "Backup created, not copied",
            "message": error or "The copy to the extra destination failed.",
        }
    return {"tone": "error", "title": "Backup failed", "message": error or "The backup failed."}


@router.post("/settings/backups", include_in_schema=False)
async def run_now(request: Request, db: Database, context: ManageSettings) -> Response:
    """Take a backup now and answer the refreshed list and banner area."""
    locations = _locations(request)
    code = status.HTTP_200_OK
    try:
        run = await run_backup(
            db,
            locations,
            trigger=BackupTrigger.MANUAL,
            actor=_actor(request, context),
            notifier=_notifier(request),
        )
    except BackupBusyError:
        code = status.HTTP_409_CONFLICT
        result = {
            "tone": "error",
            "title": "Backup not started",
            "message": "Another backup or restore is running. Try again in a moment.",
        }
    else:
        result = _result_toast(run.record.status, run.record.size_bytes, run.record.error)
    request.state.banners = await load_shell_banners(db, display_of(request))
    page = {"backups": await load_backup_list(db, locations.backup_dir), "result": result}
    return render(request, RUN_FRAGMENT, page, status_code=code)


@router.get("/settings/backups/{backup_id:int}/download", include_in_schema=False)
async def download_backup(
    request: Request, backup_id: int, db: Database, context: ManageSettings
) -> Response:
    """Stream a backup file. Not cached, and the download is audited."""
    record = await get_backup(db, backup_id)
    if record is None or record.status not in HAS_FILE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup not found")
    try:
        opened = open_backup_file(_locations(request).backup_dir, record.filename)
    except BackupFileMissingError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup file not found") from error
    try:
        await record_actor_audit(
            db,
            _actor(request, context),
            action="backup.download",
            result=AuditResult.SUCCESS,
            target_type="backup",
            target_id=record.id,
            target_name=record.filename,
            details={"size_bytes": opened.size},
        )
        await db.commit()
    except BaseException:
        os.close(opened.descriptor)
        raise
    return StreamingResponse(
        stream_backup(opened),
        media_type=DOWNLOAD_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{record.filename}"',
            "Content-Length": str(opened.size),
            "Cache-Control": "no-store",
        },
    )


async def _restore_dialog(
    request: Request, db: AsyncSession, backup_id: int
) -> tuple[Backup, dict[str, Any], int]:
    """The backup, the confirm dialog's content for it and the status code of that answer."""
    record = await get_backup(db, backup_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup not found")
    page: dict[str, Any] = {
        "backup_id": record.id,
        "when": display_of(request).text(record.created_at),
        "refusal": None,
    }
    size = backup_file_size(_locations(request).backup_dir, record.filename)
    if record.status not in HAS_FILE or size is None:
        page["refusal"] = "The file of this backup is no longer available."
        return record, page, status.HTTP_409_CONFLICT
    return record, page, status.HTTP_200_OK


@router.get("/settings/backups/{backup_id:int}/restore", include_in_schema=False)
async def restore_dialog(
    request: Request, backup_id: int, db: Database, _context: ManageSettings
) -> Response:
    """The confirm dialog of a restore: names the backup and warns that data is replaced."""
    _record, page, code = await _restore_dialog(request, db, backup_id)
    return render(request, RESTORE_FRAGMENT, page, status_code=code)


@router.post("/settings/backups/{backup_id:int}/restore", include_in_schema=False)
async def restore(
    request: Request, backup_id: int, db: Database, context: ManageSettings, crypto: Crypto
) -> Response:
    """Restore a backup. Success ends every session and sends the browser to the sign-in page.

    A refused backup, a busy lock or a failed switch answers the dialog with the reason and
    leaves the current data as it was.
    """
    record, page, code = await _restore_dialog(request, db, backup_id)
    if page["refusal"] is not None:
        return render(request, RESTORE_FRAGMENT, page, status_code=code)
    locations = _locations(request)
    try:
        await restore_backup(
            db,
            locations,
            source=locations.backup_dir / record.filename,
            label=record.filename,
            crypto=crypto,
            actor=_actor(request, context),
        )
    except RestoreRefusedError as error:
        page["refusal"] = str(error)
        code = REFUSAL_STATUS.get(error.problem, status.HTTP_422_UNPROCESSABLE_CONTENT)
        return render(request, RESTORE_FRAGMENT, page, status_code=code)
    await _scheduler(request).reload()
    response = Response(
        status_code=200, headers={"HX-Redirect": "/login", "Cache-Control": "no-store"}
    )
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
