"""The command line sends the standard notification when SMTP is configured.

The commands run in their own process and deliver before they end, so these tests drive the real
commands against a seeded database in a data folder, with the SMTP client of aiosmtplib faked.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.db import create_engine, create_session_factory
from app.models import AuditEntry, NotificationStatus, User
from app.services.app_settings import BackupSettings, SmtpSettings, save_section
from app.services.audit import AuditActor
from app.tests.dist_helpers import relocate_deployments
from app.tests.notify_helpers import (
    SMTP,
    FakeSMTP,
    configure_smtp,
    decoded_text,
    install_fake_smtp,
    switch,
)
from app.tests.template_databases import SeededTemplate, copy_database

runner = CliRunner()


@pytest.fixture
def cli_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seeded_template: SeededTemplate,
    master_key: str,
    crypto: CryptoService,
) -> Iterator[dict[str, Path]]:
    """A seeded database in the CLI's data folder with SMTP configured and the client faked."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(data_path))
    monkeypatch.setenv("ENVMANAGER_KEY", master_key)
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    monkeypatch.setenv("ENVMANAGER_DOCKER_SOCKET", str(tmp_path / "no-docker.sock"))
    get_settings.cache_clear()
    settings = get_settings()
    copy_database(seeded_template.path, settings.database_path)
    install_fake_smtp(monkeypatch)

    async def prepare() -> dict[str, Path]:
        engine = create_engine(settings.database_path)
        try:
            await configure_smtp(engine, crypto)
            async with create_session_factory(engine)() as db:
                return await relocate_deployments(db, tmp_path / "host")
        finally:
            await engine.dispose()

    yield asyncio.run(prepare())
    get_settings.cache_clear()


def database[T](work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    """Run a coroutine function on the CLI's database and return what it returns."""

    async def go() -> T:
        engine = create_engine(get_settings().database_path)
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def mails() -> list[tuple[str, str, str]]:
    """The mails the fake server received: recipient, subject and the decoded text."""
    found = []
    for client in FakeSMTP.instances:
        for message in client.messages:
            found.append((message["To"], message["Subject"], decoded_text(message)))
    return found


def test_creating_a_user_sends_the_standard_notification_without_the_password(
    cli_world: dict[str, Path],
) -> None:
    """The superadmin is told, through the SMTP settings in the database, and the one-time
    password reaches the console and never the mail."""
    result = runner.invoke(
        app,
        ["user", "create", "--email", "bob@example.test", "--name", "bob", "--group", "viewer"],
    )
    assert result.exit_code == 0, result.output
    password = next(
        line.removeprefix("password: ")
        for line in result.output.splitlines()
        if line.startswith("password: ")
    )
    ((to, subject, text),) = mails()
    assert (to, subject) == ("root@example.test", "[ENV Manager] User created: bob")
    assert "Done by: cli" in text and "Group: viewer" in text
    assert password not in text
    (client,) = FakeSMTP.instances
    assert client.logins == [("mailer", "smtp-password-sentinel")]
    assert client.arguments["hostname"] == SMTP.host


def test_the_other_user_commands_send_theirs(cli_world: dict[str, Path]) -> None:
    """Disable, password reset and two-factor reset each send one mail; enable sends none."""
    assert runner.invoke(app, ["user", "disable", "viewer@example.test"]).exit_code == 0
    assert runner.invoke(app, ["user", "enable", "viewer@example.test"]).exit_code == 0
    reset = runner.invoke(app, ["user", "passwd", "admin@example.test"])
    assert reset.exit_code == 0, reset.output
    assert runner.invoke(app, ["user", "reset-2fa", "totp_user@example.test"]).exit_code == 0
    assert [subject for _to, subject, _text in mails()] == [
        "[ENV Manager] User disabled: viewer",
        "[ENV Manager] Password reset: admin",
        "[ENV Manager] Two-factor reset: totp_user",
    ]
    password = next(
        line.removeprefix("password: ")
        for line in reset.output.splitlines()
        if line.startswith("password: ")
    )
    assert not any(password in text for _to, _subject, text in mails())


def test_a_mail_server_that_is_down_never_changes_the_command(cli_world: dict[str, Path]) -> None:
    """The user is created, the password is printed, the exit code is 0 and the failure counts."""
    FakeSMTP.connect_error = ConnectionRefusedError("[Errno 111]")
    result = runner.invoke(
        app,
        ["user", "create", "--email", "carol@example.test", "--name", "carol", "--group", "admin"],
    )
    assert result.exit_code == 0, result.output
    assert "created user carol@example.test (carol, admin)" in result.output
    assert "password: " in result.output

    async def read(engine):  # type: ignore[no-untyped-def]
        async with create_session_factory(engine)() as db:
            names = (await db.execute(select(User.name))).scalars().all()
            status = (await db.execute(select(NotificationStatus))).scalars().all()
            return names, status

    names, status = database(read)
    assert "carol" in names
    (row,) = status
    assert (row.channel, row.failures, row.reason) == (
        "email",
        1,
        "Could not connect to the mail server.",
    )
    assert len(FakeSMTP.instances) == 1  # no retries from the command line


def test_a_disabled_switch_sends_nothing_from_the_command_line(
    cli_world: dict[str, Path],
) -> None:
    """The switches of the settings apply to the CLI the same as to the web server."""
    database(lambda engine: switch(engine, user_changed=False))
    assert runner.invoke(app, ["user", "disable", "viewer@example.test"]).exit_code == 0
    assert FakeSMTP.instances == []


def test_nothing_is_sent_when_smtp_is_not_configured(
    cli_world: dict[str, Path], crypto: CryptoService
) -> None:
    """With no host the command does what it always did and never opens a connection."""
    database(lambda engine: configure_smtp(engine, crypto, SmtpSettings(), password=None))
    assert runner.invoke(app, ["user", "disable", "viewer@example.test"]).exit_code == 0
    assert FakeSMTP.instances == []


def test_a_command_without_a_usable_key_sends_nothing_and_still_works(
    cli_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that cannot read the master key cannot read the SMTP password, so no mail; the
    command that notifies still does its work, as it did before it could notify."""
    monkeypatch.delenv("ENVMANAGER_KEY", raising=False)
    get_settings.cache_clear()
    result = runner.invoke(app, ["user", "disable", "viewer@example.test"])
    assert result.exit_code == 0, result.output
    assert "disabled user viewer" in result.output
    assert FakeSMTP.instances == []

    async def read(engine: AsyncEngine) -> bool:
        async with create_session_factory(engine)() as db:
            viewer = (await db.execute(select(User).where(User.name == "viewer"))).scalar_one()
            return viewer.is_active

    assert database(read) is False


def test_a_key_file_that_does_not_exist_sends_nothing_and_does_not_crash_the_command(
    cli_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same for a key file the process cannot read."""
    monkeypatch.delenv("ENVMANAGER_KEY", raising=False)
    monkeypatch.setenv("ENVMANAGER_KEY_FILE", str(tmp_path / "missing.key"))
    get_settings.cache_clear()
    result = runner.invoke(app, ["user", "disable", "viewer@example.test"])
    assert result.exit_code == 0, result.output
    assert FakeSMTP.instances == []


def test_a_halted_deploy_from_the_command_line_notifies_the_project_admins(
    cli_world: dict[str, Path],
) -> None:
    """The deploy command passes the notifier to the engine: a halt is a problem kind."""
    target = cli_world["staging"] / "environments" / ".env.web"
    target.write_text("EDITED=1\n", encoding="utf-8")
    halted = runner.invoke(app, ["deploy", "Fancy Proj. Name/staging/web", "--env", ".env.web"])
    assert halted.exit_code == 1
    sent = mails()
    assert sorted(to for to, _subject, _text in sent) == [
        "admin@example.test",
        "project_admin@example.test",
    ]
    assert all(subject == "[ENV Manager] Deploy halted: web (staging)" for _to, subject, _t in sent)
    assert all("EDITED" not in text for _to, _subject, text in sent)
    forced = runner.invoke(
        app, ["deploy", "Fancy Proj. Name/staging/web", "--env", ".env.web", "--force"]
    )
    assert forced.exit_code == 0
    assert len(mails()) == len(sent)  # a success is off by default


def test_a_backup_that_could_not_be_copied_is_announced_from_the_command_line(
    cli_world: dict[str, Path], tmp_path: Path
) -> None:
    """backup create reports through the CLI notifier instead of a silent one."""
    outside = tmp_path / "unallowed"
    outside.mkdir()

    async def configure(engine):  # type: ignore[no-untyped-def]
        async with create_session_factory(engine)() as db:
            await save_section(
                db, "backup", BackupSettings(extra_destination=str(outside)), actor=AuditActor.cli()
            )
            await db.commit()

    database(configure)
    result = runner.invoke(app, ["backup", "create"])
    assert result.exit_code == 1
    ((to, subject, text),) = mails()
    assert (to, subject) == ("root@example.test", "[ENV Manager] Backup incomplete")
    assert "Trigger: cli" in text and str(outside) not in text


def test_each_notified_command_still_writes_its_audit_entry(cli_world: dict[str, Path]) -> None:
    """The notification is in addition to the audit entry with actor cli."""
    assert runner.invoke(app, ["user", "disable", "viewer@example.test"]).exit_code == 0

    async def read(engine):  # type: ignore[no-untyped-def]
        async with create_session_factory(engine)() as db:
            return list(
                (
                    await db.execute(select(AuditEntry).where(AuditEntry.action == "user.disable"))
                ).scalars()
            )

    (entry,) = database(read)
    assert entry.actor_name == "cli"
