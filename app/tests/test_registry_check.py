"""Testing a stored registry login: the Docker command, the config it gets, the answer, the audit.

The fake docker is a real script, so the arguments, the environment and the config folder of the
call are read back from what the process saw.
"""

import base64
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditEntry, AuditResult, DockerRegistry
from app.seeds import SeedData
from app.services import registry_check
from app.services.hook_process import ProcessResult
from app.services.registries import RegistryNotFoundError, add_registry
from app.services.registry_check import (
    ACTION_TEST,
    NOT_STARTED_MESSAGE,
    OK_MESSAGE,
    REFUSED_MESSAGE,
    TIMED_OUT_MESSAGE,
    UNAVAILABLE_SUFFIX,
    check_registry_login,
    login_arguments,
)
from app.services.run_config import RunConfigError
from app.tests.deploy_helpers import audit_of
from app.tests.fake_docker import FakeDocker

TOKEN = "dckr_pat_Zq81-secret.token_value"


def encoded(username: str = "bot", token: str = TOKEN) -> str:
    """The base64 of ``username:token``, the form a Docker config file keeps."""
    return base64.b64encode(f"{username}:{token}".encode()).decode("ascii")


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker whose commands succeed and print one line."""
    return FakeDocker(tmp_path / "fake")


async def add(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    host: str = "registry.example.com",
    username: str = "bot",
    token: str = TOKEN,
) -> int:
    """Add a registry as the seeded superadmin and give its id."""
    registry = await add_registry(
        db, crypto, host, username, token, actor=seed_data.users.superadmin
    )
    return registry.id


async def check(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    fake: FakeDocker,
    registry_id: int,
    *,
    mounted: bool = True,
) -> registry_check.LoginCheck:
    """Test a login as the seeded superadmin on the fake docker."""
    return await check_registry_login(
        db,
        crypto,
        fake.cli(mounted=mounted),
        registry_id,
        actor=seed_data.users.superadmin,
        ip_address="203.0.113.9",
    )


@pytest.mark.parametrize(
    ("host", "arguments"),
    [
        ("registry.example.com", ("login", "registry.example.com")),
        ("localhost:5000", ("login", "localhost:5000")),
        ("ghcr.io", ("login", "ghcr.io")),
        ("docker.io", ("login",)),
        ("index.docker.io", ("login",)),
        ("registry-1.docker.io", ("login",)),
    ],
)
def test_docker_hub_is_logged_in_to_without_a_server_and_every_other_host_by_name(
    host: str, arguments: tuple[str, ...]
) -> None:
    assert login_arguments(host) == arguments


@pytest.mark.parametrize(
    ("host", "arguments"),
    [("registry.example.com", ("login", "registry.example.com")), ("docker.io", ("login",))],
)
async def test_docker_gets_the_server_and_never_the_username_or_the_token(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    fake: FakeDocker,
    host: str,
    arguments: tuple[str, ...],
) -> None:
    registry_id = await add(db, crypto, seed_data, host=host)
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert result == registry_check.LoginCheck(True, OK_MESSAGE, "ok")
    (call,) = fake.calls()
    assert call.arguments == arguments
    environment = " ".join(f"{name}={value}" for name, value in call.environment.items())
    assert TOKEN not in environment and encoded() not in environment
    assert set(call.environment) >= {"PATH", "HOME", "LANG", "DOCKER_CONFIG", "DOCKER_HOST"}


async def test_the_config_holds_only_the_login_under_test_and_the_folder_goes_away(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    registry_id = await add(db, crypto, seed_data)
    await add(db, crypto, seed_data, host="ghcr.io", username="robot", token="other-token-9")
    await add(db, crypto, seed_data, host="docker.io", username="hub", token="hub-token-77")
    fake.capture_config()
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert result.ok
    captured = fake.captured_config()
    assert captured is not None
    assert captured.content == {"auths": {"registry.example.com": {"auth": encoded()}}}
    assert (captured.file_mode, captured.folder_mode) == (0o600, 0o700)
    (call,) = fake.calls()
    folder = Path(call.environment["DOCKER_CONFIG"])
    assert Path(call.working_directory).name == folder.name and not folder.exists()
    assert list((fake.folder / "scratch").iterdir()) == []


async def test_the_docker_hub_login_is_stored_under_the_key_docker_looks_up(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    registry_id = await add(db, crypto, seed_data, host="registry-1.docker.io", username="hub")
    fake.capture_config()
    await check(db, crypto, seed_data, fake, registry_id)
    captured = fake.captured_config()
    assert captured is not None
    assert captured.content == {"auths": {"https://index.docker.io/v1/": {"auth": encoded("hub")}}}


async def test_a_non_zero_exit_is_a_refusal_with_the_scrubbed_and_truncated_output(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    registry_id = await add(db, crypto, seed_data)
    noise = "x" * 6000
    fake.behavior(
        f'echo "unauthorized: bad credentials {TOKEN} {encoded()}"\n'
        f'echo "{noise}"\necho "last line"\nexit 1'
    )
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert (result.ok, result.message, result.outcome) == (False, REFUSED_MESSAGE, "refused")
    assert result.output is not None
    assert "unauthorized: bad credentials **** ****" in result.output
    assert TOKEN not in result.output and encoded() not in result.output
    assert "last line" in result.output and "bytes omitted" in result.output
    assert len(result.output) < 5000
    assert list((fake.folder / "scratch").iterdir()) == []


async def test_a_refusal_keeps_the_registrys_reason_and_drops_the_prompt_lines_docker_adds(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    """What Docker 29 printed for a stored token the registry refused, with stdin closed."""
    registry_id = await add(db, crypto, seed_data)
    fake.behavior(
        """echo "Authenticating with existing credentials... [Username: bot]"
echo ""
echo " Info -> To login with a different account, run 'docker logout' followed by 'docker login'"
echo ""
echo ""
echo "Login did not succeed, error: Error response from daemon: login attempt to \
https://registry.example.com/v2/ failed with status: 401 Unauthorized"
echo "error: cannot perform an interactive login from a non-TTY device"
exit 1"""
    )
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert (result.ok, result.message, result.outcome) == (False, REFUSED_MESSAGE, "refused")
    assert result.output == (
        "Authenticating with existing credentials... [Username: bot]\n"
        "Login did not succeed, error: Error response from daemon: login attempt to "
        "https://registry.example.com/v2/ failed with status: 401 Unauthorized"
    )


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            "Error response from daemon: unauthorized\nError: Cannot perform an interactive "
            "login from a non TTY device\n",
            "Error response from daemon: unauthorized",
        ),
        ("one\n\n  \ntwo\n", "one\ntwo"),
        ("Error: Cannot perform an interactive login from a non TTY device", None),
    ],
)
def test_only_the_lines_about_prompting_are_dropped_and_never_all_of_the_text(
    text: str, shown: str | None
) -> None:
    assert registry_check.without_prompt_noise(text) == (text if shown is None else shown)


async def test_a_check_that_takes_too_long_is_stopped_and_its_folder_removed(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    fake: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_id = await add(db, crypto, seed_data)
    monkeypatch.setattr(registry_check, "CHECK_TIMEOUT_SECONDS", 0.5)
    fake.behavior("echo waiting\nexec sleep 30")
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert (result.ok, result.message, result.outcome) == (False, TIMED_OUT_MESSAGE, "timed_out")
    assert result.output == "waiting"
    assert list((fake.folder / "scratch").iterdir()) == []


async def test_a_command_that_cannot_be_started_is_a_refusal_and_the_folder_is_removed(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    fake: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_id = await add(db, crypto, seed_data)

    async def cannot_start(*args: object, **kwargs: object) -> ProcessResult:
        raise OSError("no such program")

    monkeypatch.setattr(registry_check, "run_process", cannot_start)
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert (result.ok, result.message) == (False, NOT_STARTED_MESSAGE)
    assert result.outcome == "not_started"
    assert list((fake.folder / "scratch").iterdir()) == []


async def test_a_config_that_cannot_be_written_is_a_refusal_that_runs_nothing(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    fake: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_id = await add(db, crypto, seed_data)

    def unwritable(*args: object, **kwargs: object) -> NoReturn:
        raise RunConfigError("The Docker config of the run could not be written.")

    monkeypatch.setattr(registry_check, "private_docker_config", unwritable)
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert (result.ok, result.outcome) == (False, "config_unwritable")
    assert result.message == "The Docker config of the run could not be written."
    assert fake.calls() == []


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no-socket", "The Docker socket is not mounted."),
        ("no-daemon", "The Docker daemon does not answer through the socket."),
        ("no-compose", "Docker Compose is not available in the app image."),
    ],
)
async def test_without_a_usable_docker_nothing_runs_and_the_answer_gives_the_reason(
    db: AsyncSession,
    crypto: CryptoService,
    seed_data: SeedData,
    fake: FakeDocker,
    case: str,
    reason: str,
) -> None:
    registry_id = await add(db, crypto, seed_data)
    if case != "no-socket":
        fake.switch(case)
    result = await check(db, crypto, seed_data, fake, registry_id, mounted=case != "no-socket")
    assert (result.ok, result.outcome) == (False, "unavailable")
    assert result.message == f"{reason} {UNAVAILABLE_SUFFIX}"
    assert fake.calls() == []
    (entry,) = await audit_of(db, ACTION_TEST)
    assert entry.result == AuditResult.FAILURE and entry.details["outcome"] == "unavailable"


async def test_a_token_that_cannot_be_opened_is_refused_with_the_registry_named_and_runs_nothing(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    registry_id = await add(db, crypto, seed_data)
    registry = await db.get(DockerRegistry, registry_id)
    assert registry is not None
    registry.token_encrypted = "not a sealed value"
    await db.commit()
    result = await check(db, crypto, seed_data, fake, registry_id)
    assert (result.ok, result.outcome) == (False, "token_unreadable")
    assert "registry.example.com" in result.message and "Enter it again" in result.message
    assert fake.calls() == []


async def test_a_registry_that_is_gone_raises_and_is_not_audited(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    with pytest.raises(RegistryNotFoundError):
        await check(db, crypto, seed_data, fake, 4242)
    assert await audit_of(db, ACTION_TEST) == []
    assert fake.calls() == []


async def test_every_attempt_is_audited_with_the_host_and_the_result_and_never_the_output(
    db: AsyncSession, crypto: CryptoService, seed_data: SeedData, fake: FakeDocker
) -> None:
    registry_id = await add(db, crypto, seed_data)
    await check(db, crypto, seed_data, fake, registry_id)
    fake.behavior(f'echo "denied {TOKEN}"\nexit 1')
    await check(db, crypto, seed_data, fake, registry_id)
    succeeded, refused = await audit_of(db, ACTION_TEST)
    for entry in (succeeded, refused):
        assert isinstance(entry, AuditEntry)
        assert entry.action == "registry.test" and entry.target_type == "registry"
        assert (entry.target_id, entry.target_name) == (registry_id, "registry.example.com")
        assert entry.actor_user_id == seed_data.users.superadmin.id
        assert entry.ip_address == "203.0.113.9"
    assert succeeded.result == AuditResult.SUCCESS
    assert succeeded.details == {
        "host": "registry.example.com",
        "username": "bot",
        "ok": True,
        "outcome": "ok",
    }
    assert refused.result == AuditResult.FAILURE
    assert refused.details == {
        "host": "registry.example.com",
        "username": "bot",
        "ok": False,
        "outcome": "refused",
    }
    assert "denied" not in str(refused.details) and TOKEN not in str(refused.details)
