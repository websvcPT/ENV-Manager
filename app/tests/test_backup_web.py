"""The Backup tab: the page, saving, Run now, Download, the restore dialog and the banner."""

import re
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.models import AppSetting, AuditResult, Backup, BackupStatus, BackupTrigger, UserSession
from app.services.audit import AuditActor
from app.services.backup_lock import hold_backup_lock
from app.services.backup_run import run_backup
from app.tests.audit_helpers import audit_entries_from
from app.tests.backup_helpers import (
    RecordingBackupNotifier,
    allow_path,
    app_locations,
    app_session_factory,
    backup_records,
    save_backup_settings,
)
from app.tests.conftest import sign_in
from app.tests.settings_helpers import (
    NON_SUPERADMINS,
    browser_as,
    changes_of,
    error_of,
    selected_of,
    settings_entries,
    stored,
    stored_rows,
    toast_of,
    value_of,
)
from app.tests.web_helpers import HTMX, assert_never_cached, main_of, post

GENERAL_FORM = {
    "time_zone": "UTC",
    "date_format": "iso",
    "dist_missing_policy": "block",
    "drift_policy": "halt",
    "missing_target_policy": "create",
    "file_mode": "0600",
    "file_owner": "",
    "file_group": "",
}
DEFAULT_FORM = {"schedule": "daily", "retention": "count:14", "extra_destination": ""}
WARNING_TEXT = "Replaces all current data with this backup."


@pytest.fixture
async def root(web_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """The browser signed in as the superadmin."""
    await sign_in(web_client, "root")
    return web_client


async def run_now(root: httpx.AsyncClient) -> httpx.Response:
    """Press Run now."""
    return await post(root, "/settings/backups")


async def make_backup(web_app: FastAPI) -> Backup:
    """Take a backup through the service, as the command line would."""
    async with app_session_factory(web_app)() as db:
        run = await run_backup(
            db,
            app_locations(web_app),
            trigger=BackupTrigger.MANUAL,
            actor=AuditActor.cli(),
            notifier=RecordingBackupNotifier(),
        )
    return run.record


def rows_of(page: str) -> list[str]:
    """The markup of every backup row of a page or fragment."""
    return re.findall(r'<tr data-fragment="backup-row".*?</tr>', page, re.DOTALL)


async def test_the_tab_shows_the_defaults_and_an_empty_list(root: httpx.AsyncClient) -> None:
    """Daily, keep 14, no extra folder, no backups yet, and the restore dialog ready."""
    response = await root.get("/settings/backup")
    assert response.status_code == 200
    assert_never_cached(response)
    page = main_of(response.text)
    assert (selected_of(page, "bs"), selected_of(page, "br"), value_of(page, "bx")) == (
        "daily",
        "count:14",
        "",
    )
    assert "Runs in the display time zone (UTC). Weekly runs on Sunday at 02:00." in page
    assert "Must be inside an allowed path." in page
    assert "Schedule and retention" in page and ">Backups</h2>" in page
    assert "No backups yet" in page
    assert 'hx-post="/settings/backups"' in page and "Run now" in page
    assert 'id="backup-restore"' in page and 'data-fragment="backup-list"' in page
    assert rows_of(page) == []
    assert "Not available yet" not in page


async def test_saving_stores_audits_and_reschedules_at_once(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A save is stored, audited with old and new values, and moves the scheduled job."""
    response = await post(
        root,
        "/settings/backup",
        {"schedule": "weekly", "retention": "days:30", "extra_destination": ""},
    )
    assert response.status_code == 200
    assert_never_cached(response)
    assert toast_of(response.text) == (
        "Backup settings saved",
        "The schedule and retention apply now.",
    )
    assert selected_of(response.text, "bs") == "weekly"
    assert selected_of(response.text, "br") == "days:30"
    current = (await stored(web_app)).backup
    assert (current.schedule, current.retention_count, current.retention_days) == ("weekly", 0, 30)
    (entry,) = await settings_entries(web_app)
    assert entry.action == "settings.update" and entry.actor_name == "root"
    assert entry.details["section"] == "backup"
    assert {change["setting"]: (change["old"], change["new"]) for change in changes_of(entry)} == {
        "backup.schedule": ("daily", "weekly"),
        "backup.retention_count": (14, 0),
        "backup.retention_days": (0, 30),
    }
    next_run = web_app.state.backup_scheduler.next_backup()
    assert next_run is not None and next_run.weekday() == 6


async def test_an_unchanged_save_says_so_and_stores_nothing(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Saving the defaults again changes and audits nothing."""
    response = await post(root, "/settings/backup", DEFAULT_FORM)
    assert toast_of(response.text) == ("Nothing to save", "The settings are unchanged.")
    assert await stored_rows(web_app) == {}
    assert await settings_entries(web_app) == []


@pytest.mark.parametrize(
    ("form", "field", "message"),
    [
        ({**DEFAULT_FORM, "schedule": "hourly"}, "bs", "Choose one of the listed schedules."),
        (
            {**DEFAULT_FORM, "retention": "forever"},
            "br",
            "Choose one of the listed retention limits.",
        ),
        ({**DEFAULT_FORM, "extra_destination": "/etc"}, "bx", "Must be inside an allowed path."),
        ({**DEFAULT_FORM, "extra_destination": "backups"}, "bx", "Path must be absolute."),
    ],
)
async def test_a_refused_value_answers_the_form_with_the_reason(
    root: httpx.AsyncClient,
    web_app: FastAPI,
    form: dict[str, str],
    field: str,
    message: str,
) -> None:
    """Each refusal is a 422 with the reason next to its field, and nothing is stored."""
    response = await post(root, "/settings/backup", form)
    assert response.status_code == 422
    assert error_of(response.text, field) == message
    assert await stored_rows(web_app) == {}


async def test_an_extra_destination_inside_an_allowed_path_is_stored_as_its_real_path(
    root: httpx.AsyncClient, web_app: FastAPI, tmp_path: Path
) -> None:
    """The folder must exist inside the list; the real path is what is kept."""
    real = (tmp_path / "allowed" / "backups").resolve()
    real.mkdir(parents=True)
    link = real.parent / "link"
    link.symlink_to(real)
    await allow_path(app_session_factory(web_app), real.parent)
    response = await post(
        root,
        "/settings/backup",
        {**DEFAULT_FORM, "extra_destination": f"  {link}  "},
    )
    assert response.status_code == 200
    assert value_of(response.text, "bx") == str(real)
    assert (await stored(web_app)).backup.extra_destination == str(real)
    refused = await post(
        root,
        "/settings/backup",
        {**DEFAULT_FORM, "extra_destination": str(real.parent / "missing")},
    )
    assert error_of(refused.text, "bx") == "The folder does not exist."


async def test_a_stored_destination_that_is_no_longer_allowed_is_called_out(
    root: httpx.AsyncClient, web_app: FastAPI, tmp_path: Path
) -> None:
    """The page says why backups are not copied there, instead of failing quietly."""
    folder = (tmp_path / "gone-from-the-list").resolve()
    folder.mkdir()
    await save_backup_settings(app_session_factory(web_app), extra_destination=str(folder))
    page = (await root.get("/settings/backup")).text
    assert "Backups are not copied there until this is fixed." in page
    assert "The saved folder is no longer inside an allowed path." in page


async def test_a_new_time_zone_moves_the_scheduled_run(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Saving General settings with another zone reschedules, since 02:00 is in that zone."""
    response = await post(root, "/settings/general", {**GENERAL_FORM, "time_zone": "Asia/Tokyo"})
    assert response.status_code == 200
    next_run = web_app.state.backup_scheduler.next_backup()
    assert next_run is not None
    assert next_run.utcoffset() == timedelta(hours=9) and next_run.hour == 2


async def test_run_now_takes_a_backup_and_lists_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The answer is the refreshed list with a toast, the banner area out of band, and the audit."""
    response = await run_now(root)
    assert response.status_code == 200
    assert_never_cached(response)
    title, _message = toast_of(response.text) or ("", "")
    assert title == "Backup created"
    (row,) = rows_of(response.text)
    (record,) = await backup_records(app_session_factory(web_app))
    assert (record.trigger, record.status, record.created_by_id is not None) == (
        BackupTrigger.MANUAL,
        BackupStatus.SUCCESS,
        True,
    )
    assert f'href="/settings/backups/{record.id}/download"' in row
    assert f'hx-get="/settings/backups/{record.id}/restore"' in row
    assert 'data-status="success"' in row and "Manual" in row
    assert re.search(r"[0-9.]+ (?:KB|MB)", row)
    assert 'id="banner-area"' in response.text and 'hx-swap-oob="true"' in response.text
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.create")
    assert entry.actor_name == "root" and entry.result is AuditResult.SUCCESS
    again = await run_now(root)
    assert len(rows_of(again.text)) == 2


async def test_a_failed_run_says_so_and_raises_the_banner_until_one_succeeds(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The failure shows in the toast, the row and the banner on every page, then clears."""
    locations = app_locations(web_app)
    locations.backup_dir.mkdir(mode=0o700, exist_ok=True)
    moved = locations.backup_dir.parent / "backups-away"
    locations.backup_dir.rename(moved)
    locations.backup_dir.write_text("a file where the folder belongs")
    response = await run_now(root)
    assert toast_of(response.text) == ("Backup failed", "The backup folder cannot be used.")
    (row,) = rows_of(response.text)
    assert 'data-status="failed"' in row and "The backup folder cannot be used." in row
    assert ">None</span>" in row and "Download" not in row and "Restore" not in row
    assert "Backup failed: last backup (" in response.text
    assert 'href="/settings/backup"' in response.text
    page = (await root.get("/projects")).text
    assert "Backup failed: last backup (" in page and "Open backup settings" in page
    locations.backup_dir.unlink()
    moved.rename(locations.backup_dir)
    await run_now(root)
    assert "Backup failed" not in (await root.get("/projects")).text


async def test_a_backup_that_was_not_copied_is_partial_with_its_own_banner(
    root: httpx.AsyncClient, web_app: FastAPI, tmp_path: Path
) -> None:
    """The snapshot exists and can be used, and the page and banner say the copy failed."""
    folder = (tmp_path / "not-allowed").resolve()
    folder.mkdir()
    await save_backup_settings(app_session_factory(web_app), extra_destination=str(folder))
    response = await run_now(root)
    assert toast_of(response.text) == (
        "Backup created, not copied",
        "The extra destination is outside the allowed paths.",
    )
    (row,) = rows_of(response.text)
    assert 'data-status="partial"' in row and "Not copied" in row
    assert "Download" in row and "Restore" in row
    assert "Backup incomplete: the last backup (" in (await root.get("/projects")).text


async def test_only_the_superadmin_sees_the_banner_and_only_on_pages(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Admins and viewers never get it, and a fragment request carries none."""
    locations = app_locations(web_app)
    locations.backup_dir.write_text("blocked")
    await run_now(root)
    assert "Backup failed" in (await root.get("/projects")).text
    assert "Backup failed" not in (await root.get("/projects/table", headers=HTMX)).text
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            assert "Backup failed" not in (await http.get("/projects")).text, user


async def test_a_backup_in_progress_is_refused(root: httpx.AsyncClient, web_app: FastAPI) -> None:
    """Run now while another backup holds the lock answers 409 and records nothing."""
    with hold_backup_lock(app_locations(web_app).lock_dir):
        response = await run_now(root)
    assert response.status_code == 409
    assert toast_of(response.text) == (
        "Backup not started",
        "Another backup or restore is running. Try again in a moment.",
    )
    assert await backup_records(app_session_factory(web_app)) == []


async def test_a_backup_whose_file_is_gone_has_no_links(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A file removed by hand leaves a row that says so instead of a broken link."""
    record = await make_backup(web_app)
    (app_locations(web_app).backup_dir / record.filename).unlink()
    (row,) = rows_of((await root.get("/settings/backup")).text)
    assert "File removed" in row and "Download" not in row and "Restore" not in row


async def test_download_streams_the_file_not_cached_and_audits_it(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The bytes are the file, the headers forbid caching and the download is in the audit log."""
    record = await make_backup(web_app)
    path = app_locations(web_app).backup_dir / record.filename
    response = await root.get(f"/settings/backups/{record.id}/download")
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert response.headers["content-type"] == "application/vnd.sqlite3"
    assert response.headers["content-disposition"] == f'attachment; filename="{record.filename}"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == str(path.stat().st_size)
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.download")
    assert (entry.actor_name, entry.target_name, entry.result) == (
        "root",
        record.filename,
        AuditResult.SUCCESS,
    )
    assert entry.details == {"size_bytes": path.stat().st_size}


async def test_download_refuses_what_is_not_a_file_of_ours(
    root: httpx.AsyncClient, web_app: FastAPI, tmp_path: Path
) -> None:
    """Unknown ids, failed backups, missing files and swapped links all answer 404."""
    record = await make_backup(web_app)
    locations = app_locations(web_app)
    assert (await root.get("/settings/backups/9999/download")).status_code == 404
    async with app_session_factory(web_app)() as db:
        failed = Backup(
            filename="envmanager-20261018T000000000000Z-manual.db",
            trigger=BackupTrigger.MANUAL,
            status=BackupStatus.FAILURE,
            error="The database could not be copied.",
        )
        db.add(failed)
        await db.commit()
        failed_id = failed.id
    assert (await root.get(f"/settings/backups/{failed_id}/download")).status_code == 404
    secret = tmp_path / "secret.txt"
    secret.write_text("not a backup")
    path = locations.backup_dir / record.filename
    path.unlink()
    path.symlink_to(secret)
    response = await root.get(f"/settings/backups/{record.id}/download")
    assert response.status_code == 404 and b"not a backup" not in response.content
    assert await audit_entries_from(app_session_factory(web_app), "backup.download") == []


async def test_every_route_of_the_tab_is_for_the_superadmin_only(web_app: FastAPI) -> None:
    """Every other group gets 403 everywhere and nothing is written, taken or audited."""
    record = await make_backup(web_app)
    before = len(await backup_records(app_session_factory(web_app)))
    for user in NON_SUPERADMINS:
        async with browser_as(web_app, user) as http:
            gets = ["/settings/backup", f"/settings/backups/{record.id}/download"]
            gets.append(f"/settings/backups/{record.id}/restore")
            for path in gets:
                assert (await http.get(path)).status_code == 403, (user, path)
            for path in (
                "/settings/backup",
                "/settings/backups",
                f"/settings/backups/{record.id}/restore",
            ):
                assert (await post(http, path, DEFAULT_FORM)).status_code == 403, (user, path)
    assert len(await backup_records(app_session_factory(web_app))) == before
    for action in ("backup.download", "backup.restore"):
        assert await audit_entries_from(app_session_factory(web_app), action) == []
    assert await stored_rows(web_app) == {}


async def test_an_anonymous_visitor_cannot_download(
    web_client: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """Signed out, the download sends the visitor to sign in and serves nothing."""
    record = await make_backup(web_app)
    response = await web_client.get(f"/settings/backups/{record.id}/download")
    assert response.status_code == 303 and response.headers["location"] == "/login"


async def test_the_restore_dialog_names_the_backup_and_warns(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The dialog states the time, that data is replaced and the safety backup and sign out."""
    record = await make_backup(web_app)
    when = (await root.get("/settings/backup")).text
    assert f"/settings/backups/{record.id}/restore" in when
    response = await root.get(f"/settings/backups/{record.id}/restore")
    assert response.status_code == 200
    assert_never_cached(response)
    page = response.text
    shown = re.search(r"Restore backup ([0-9-]+ [0-9:]+)</h2>", page)
    assert shown is not None
    assert f"Changes made since {shown.group(1)} are lost." in page
    assert WARNING_TEXT in page and "safety backup" in page and "everyone is signed out" in page
    assert f'hx-post="/settings/backups/{record.id}/restore"' in page
    assert "Restore backup</button>" in page and "Cancel" in page


async def test_the_restore_dialog_of_a_missing_file_has_no_button(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A backup without its file says so, and the unknown id is a 404."""
    record = await make_backup(web_app)
    (app_locations(web_app).backup_dir / record.filename).unlink()
    response = await root.get(f"/settings/backups/{record.id}/restore")
    assert response.status_code == 409
    assert "no longer available" in response.text and "hx-post" not in response.text
    assert (await root.get("/settings/backups/9999/restore")).status_code == 404
    refused = await post(root, f"/settings/backups/{record.id}/restore")
    assert refused.status_code == 409 and "no longer available" in refused.text


async def test_a_confirmed_restore_replaces_the_data_ends_sessions_and_sends_to_sign_in(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The answer redirects, the cookie is dropped, every session is gone and the data is back."""
    factory = app_session_factory(web_app)
    async with factory() as db:
        db.add(AppSetting(key="marker.value", value="before"))
        await db.commit()
    record = await make_backup(web_app)
    async with factory() as db:
        row = await db.get(AppSetting, "marker.value")
        assert row is not None
        row.value = "after"
        await db.commit()

    response = await post(root, f"/settings/backups/{record.id}/restore")

    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/login"
    assert_never_cached(response)
    assert (
        'session=""' in response.headers["set-cookie"]
        or "Max-Age=0" in response.headers["set-cookie"]
    )
    async with factory() as db:
        assert (await db.get(AppSetting, "marker.value")).value == "before"  # type: ignore[union-attr]
        assert (await db.execute(select(func.count()).select_from(UserSession))).scalar_one() == 0
    (entry,) = await audit_entries_from(factory, "backup.restore")
    assert (entry.actor_name, entry.result, entry.target_name) == (
        "root",
        AuditResult.SUCCESS,
        record.filename,
    )
    assert (await root.get("/projects")).status_code == 303
    await sign_in(root, "root")
    page = (await root.get("/settings/backup")).text
    assert "Before a restore" in page and "Manual" in page


async def test_a_backup_made_with_another_key_is_refused_in_the_dialog(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The dialog says the key does not match, nothing changes and the session stays alive."""
    record = await make_backup(web_app)
    path = app_locations(web_app).backup_dir / record.filename
    other = CryptoService(parse_master_key(generate_master_key()))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE app_settings SET value = ? WHERE key = 'key_check'", (other.make_key_check(),)
        )
    response = await post(root, f"/settings/backups/{record.id}/restore")
    assert response.status_code == 422
    assert "different master key" in response.text and "hx-post" not in response.text
    assert (await root.get("/projects")).status_code == 200
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.restore")
    assert entry.result is AuditResult.BLOCKED and entry.details["reason"] == "wrong_key"


async def test_a_restore_during_a_backup_is_refused_in_the_dialog(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """A busy lock answers 409 with the reason and keeps the session."""
    record = await make_backup(web_app)
    with hold_backup_lock(app_locations(web_app).lock_dir):
        response = await post(root, f"/settings/backups/{record.id}/restore")
    assert response.status_code == 409 and "Another backup or restore is running" in response.text
    assert (await root.get("/projects")).status_code == 200


async def test_the_list_shows_why_each_backup_was_taken(
    root: httpx.AsyncClient, web_app: FastAPI
) -> None:
    """The trigger sits under the time: manual, scheduled, command line and the safety kinds."""
    factory = app_session_factory(web_app)
    async with factory() as db:
        for trigger in BackupTrigger:
            db.add(
                Backup(
                    filename=f"envmanager-20261018T0000{len(trigger.value):02d}000000Z-{trigger.value}.db",
                    trigger=trigger,
                    status=BackupStatus.SUCCESS,
                    size_bytes=2048,
                )
            )
        await db.commit()
    page = main_of((await root.get("/settings/backup")).text)
    labels = (
        "Manual",
        "Scheduled",
        "Command line",
        "Before a migration",
        "Before a restore",
        "Before a key rotation",
    )
    for label in labels:
        assert label in page
