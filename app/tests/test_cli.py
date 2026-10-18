"""Tests for the envmanager CLI against a real temporary database."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.db import create_engine, create_session_factory
from app.core.passwords import verify_password
from app.models import ApiToken, AuditEntry, User
from app.services import totp
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy
from app.services.permissions import load_user_scope
from app.services.sessions import create_session
from app.services.tokens import create_token

runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_data_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at an empty data folder."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(data_path))
    get_settings.cache_clear()
    yield data_path
    get_settings.cache_clear()


def _read_rows[Row: (User, AuditEntry, ApiToken)](data_path: Path, model: type[Row]) -> list[Row]:
    """Rows of one model read back from the CLI's database."""

    async def read() -> list[Row]:
        engine = create_engine(data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                return list((await db.execute(select(model))).scalars())
        finally:
            await engine.dispose()

    return asyncio.run(read())


def _users(data_path: Path) -> list[User]:
    """Users read back from the CLI's database."""
    return _read_rows(data_path, User)


def _audit(data_path: Path) -> list[AuditEntry]:
    """Audit entries read back from the CLI's database."""
    return _read_rows(data_path, AuditEntry)


def _password(output: str) -> str:
    """The password printed by a create or passwd command."""
    prefix = "password: "
    lines = [line for line in output.splitlines() if line.startswith(prefix)]
    return lines[0].removeprefix(prefix)


def test_create_prints_a_working_password_once_and_audits_as_cli(cli_data_path: Path) -> None:
    result = runner.invoke(
        app, ["user", "create", "root", "root@example.test", "--group", "superadmin"]
    )
    assert result.exit_code == 0, result.output
    password = _password(result.output)
    assert len(password) == 16
    (user,) = _users(cli_data_path)
    assert user.group.value == "superadmin"
    assert verify_password(password, user.password_hash)[0]
    (entry,) = _audit(cli_data_path)
    assert (entry.action, entry.actor_type.value, entry.actor_name) == ("user.create", "cli", "cli")
    assert password not in repr(entry.details)


def test_create_rejects_a_duplicate_and_a_bad_group() -> None:
    args = ["user", "create", "root", "root@example.test", "--group", "admin"]
    assert runner.invoke(app, args).exit_code == 0
    duplicate = runner.invoke(app, args)
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.output
    bad_group = runner.invoke(app, ["user", "create", "x", "x@example.test", "--group", "owner"])
    assert bad_group.exit_code == 2


def test_create_refuses_an_email_another_user_has_ignoring_case(cli_data_path: Path) -> None:
    first = ["user", "create", "ann", "Ann@Example.test", "--group", "admin"]
    assert runner.invoke(app, first).exit_code == 0
    duplicate = runner.invoke(
        app, ["user", "create", "ann2", "ann@example.TEST", "--group", "viewer"]
    )
    assert duplicate.exit_code == 1
    assert "Another user already has this email address." in duplicate.output
    assert [user.name for user in _users(cli_data_path)] == ["ann"]


def test_list_shows_group_status_and_last_login() -> None:
    runner.invoke(app, ["user", "create", "root", "root@example.test", "--group", "superadmin"])
    runner.invoke(app, ["user", "create", "view", "view@example.test", "--group", "viewer"])
    runner.invoke(app, ["user", "disable", "view"])
    result = runner.invoke(app, ["user", "list"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].split() == ["NAME", "GROUP", "STATUS", "LAST", "LOGIN"]
    assert lines[1].split() == ["root", "superadmin", "active", "never"]
    assert lines[2].split() == ["view", "viewer", "disabled", "never"]


def test_disable_refuses_the_last_superadmin_then_enable_round_trips(
    cli_data_path: Path,
) -> None:
    runner.invoke(app, ["user", "create", "root", "root@example.test", "--group", "superadmin"])
    runner.invoke(app, ["user", "create", "adm", "adm@example.test", "--group", "admin"])
    refused = runner.invoke(app, ["user", "disable", "root"])
    assert refused.exit_code == 1
    assert "last active superadmin" in refused.output
    assert runner.invoke(app, ["user", "disable", "adm"]).exit_code == 0
    assert not {user.name: user.is_active for user in _users(cli_data_path)}["adm"]
    assert runner.invoke(app, ["user", "enable", "ADM"]).exit_code == 0
    assert {user.name: user.is_active for user in _users(cli_data_path)}["adm"]


def test_passwd_sets_a_new_password_and_unknown_user_fails(cli_data_path: Path) -> None:
    created = runner.invoke(app, ["user", "create", "adm", "adm@example.test", "--group", "admin"])
    first = _password(created.output)
    result = runner.invoke(app, ["user", "passwd", "adm"])
    assert result.exit_code == 0, result.output
    second = _password(result.output)
    assert first != second
    (user,) = _users(cli_data_path)
    assert verify_password(second, user.password_hash)[0]
    assert not verify_password(first, user.password_hash)[0]
    missing = runner.invoke(app, ["user", "passwd", "ghost"])
    assert missing.exit_code == 1
    assert "no user named" in missing.output


def test_disable_revokes_the_users_api_tokens_and_enable_does_not_bring_them_back(
    cli_data_path: Path,
) -> None:
    runner.invoke(app, ["user", "create", "root", "root@example.test", "--group", "superadmin"])
    runner.invoke(app, ["user", "create", "adm", "adm@example.test", "--group", "admin"])

    async def make_token() -> None:
        engine = create_engine(cli_data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                owner = (await db.execute(select(User).where(User.name == "adm"))).scalar_one()
                await create_token(
                    db,
                    owner,
                    await load_user_scope(db, owner),
                    name="ci",
                    operations=["deploy"],
                    restriction="",
                    expires="",
                    actor=AuditActor.for_user(owner),
                )
        finally:
            await engine.dispose()

    asyncio.run(make_token())
    assert runner.invoke(app, ["user", "disable", "adm"]).exit_code == 0
    assert runner.invoke(app, ["user", "enable", "adm"]).exit_code == 0
    (token,) = _read_rows(cli_data_path, ApiToken)
    assert token.revoked_at is not None
    revoked = [entry for entry in _audit(cli_data_path) if entry.action == "token.revoke"]
    assert [(entry.actor_name, entry.target_name) for entry in revoked] == [("cli", "ci")]
    assert revoked[0].details == {"owner": "adm", "reason": "user_disabled"}


def test_reset_2fa_turns_the_factor_off_and_audits_as_cli(
    cli_data_path: Path, crypto: CryptoService
) -> None:
    """The superadmin's recovery for a lost authenticator, from the command line."""
    runner.invoke(app, ["user", "create", "adm", "adm@example.test", "--group", "admin"])

    async def turn_on() -> None:
        engine = create_engine(cli_data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                user = (await db.execute(select(User).where(User.name == "adm"))).scalar_one()
                totp.activate(user, totp.generate_secret(), 5, crypto)
                await create_session(db, user, AuthPolicy())
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(turn_on())
    result = runner.invoke(app, ["user", "reset-2fa", "ADM"])
    assert result.exit_code == 0, result.output
    assert "two-factor sign-in reset for ADM, 1 session(s) ended" in result.output
    (user,) = _users(cli_data_path)
    assert (user.totp_enabled, user.totp_secret, user.totp_enabled_at) == (False, None, None)
    (entry,) = [e for e in _audit(cli_data_path) if e.action == "user.totp_reset"]
    assert (entry.actor_type.value, entry.actor_name, entry.target_name) == ("cli", "cli", "adm")
    assert entry.details == {"sessions_ended": 1, "pending_logins_ended": 0}
    again = runner.invoke(app, ["user", "reset-2fa", "adm"])
    assert again.exit_code == 1
    assert "adm has no two-factor sign-in to reset." in again.output


def test_reset_2fa_of_an_unknown_user_fails() -> None:
    missing = runner.invoke(app, ["user", "reset-2fa", "ghost"])
    assert missing.exit_code == 1
    assert "no user named" in missing.output
