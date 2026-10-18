"""Saving the post-deploy settings of a service and the compose check: who, when and what."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AuditResult, HookType, ServiceHook
from app.services.audit import AuditActor
from app.services.docker_cli import DAEMON_UNREACHABLE_REASON, NOT_MOUNTED_REASON, DockerCli
from app.services.hook_settings import (
    CHECK_FAILED_MESSAGE,
    CHECK_OK_MESSAGE,
    CHECK_UNAVAILABLE_SUFFIX,
    TIMEOUT_MESSAGE,
    UNAVAILABLE_SUFFIX,
    ComposeCheck,
    HookForbiddenError,
    HookForm,
    HookSettingsError,
    check_compose_setup,
    save_post_deploy_hook,
)
from app.tests.deploy_helpers import Harness, audit_of
from app.tests.fake_docker import FakeDocker

SECRET_COMMAND = "docker compose up -d --no-deps --force-recreate {service_name}"


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker whose commands succeed."""
    return FakeDocker(tmp_path / "fake")


def form(
    commands: str = SECRET_COMMAND,
    working_directory: str = "",
    timeout: str = "120",
    run_only_if_changed: bool = True,
) -> HookForm:
    """A valid form with some fields replaced."""
    return HookForm(working_directory, commands, timeout, run_only_if_changed)


async def save(
    h: Harness, cli: DockerCli, hook_form: HookForm, *, user: str = "superadmin", service: int = 0
) -> ServiceHook | None:
    """Save the settings of the staging web service as a seeded user."""
    return await save_post_deploy_hook(
        h.db,
        cli,
        await h.scope_of(user),
        AuditActor.for_user(getattr(h.seed.users, user)),
        service or h.seed.services.staging_web.id,
        hook_form,
    )


async def stored(h: Harness, service: int = 0) -> ServiceHook | None:
    """The stored post-deploy hook of the staging web service, read fresh."""
    await h.db.rollback()
    return await h.db.scalar(
        select(ServiceHook)
        .where(
            ServiceHook.service_id == (service or h.seed.services.staging_web.id),
            ServiceHook.type == HookType.POST_DEPLOY,
        )
        .execution_options(populate_existing=True)
    )


async def test_the_superadmin_saves_normalized_settings_and_the_change_is_audited(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    other = h.roots["staging"] / "compose"
    other.mkdir()
    hook = await save(
        h,
        fake.cli(),
        form(
            commands=(
                "  docker compose rm -sf {service_name}  \r\n\r\n"
                "docker compose up -d {service_name}\n"
            ),
            working_directory=f" {other} ",
            timeout="45",
            run_only_if_changed=False,
        ),
    )
    assert hook is not None
    saved = await stored(h)
    assert saved is not None
    assert saved.commands == (
        "docker compose rm -sf {service_name}\ndocker compose up -d {service_name}"
    )
    assert saved.working_directory == str(other)
    assert (saved.timeout_seconds, saved.run_only_if_changed) == (45, False)

    (entry,) = await audit_of(h.db, "service.hook_save")
    assert entry.result is AuditResult.SUCCESS and entry.actor_name == "root"
    assert entry.details == {
        "hook": "post_deploy",
        "changed": ["commands", "working_directory", "timeout", "run_only_if_changed"],
        "commands": 2,
    }
    assert entry.service_id == h.seed.services.staging_web.id
    audited = json.dumps(entry.details)
    assert str(other) not in audited and "compose" not in audited


async def test_a_blank_working_directory_means_the_deployment_root(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    hook = await save(h, fake.cli(), form(working_directory="   "))
    assert hook is not None and hook.working_directory is None


async def test_saving_the_same_settings_again_changes_and_audits_nothing(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    await save(h, fake.cli(), form(timeout="60"))
    await save(h, fake.cli(), form(timeout="60"))
    entries = await audit_of(h.db, "service.hook_save")
    assert len(entries) == 1
    await save(h, fake.cli(), form(timeout="90"))
    entries = await audit_of(h.db, "service.hook_save")
    assert [entry.details["changed"] for entry in entries] == [
        ["commands", "timeout"],
        ["timeout"],
    ]


async def test_a_new_service_gets_a_hook_row_and_an_empty_save_creates_none(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    api = h.seed.services.api
    assert await stored(h, api.id) is None
    assert await save(h, fake.cli(), form(commands=""), service=api.id) is None
    assert await stored(h, api.id) is None
    assert await audit_of(h.db, "service.hook_save") == []
    created = await save(h, fake.cli(), form(), service=api.id)
    assert created is not None and created.type is HookType.POST_DEPLOY
    cleared = await save(h, fake.cli(), form(commands=" \n "), service=api.id)
    assert cleared is not None and cleared.commands == ""


@pytest.mark.parametrize("user", ["admin", "viewer", "project_admin", "env_scoped_admin"])
async def test_only_the_superadmin_may_save_commands(
    harness: Harness, fake: FakeDocker, user: str
) -> None:
    h = harness
    before = await stored(h)
    with pytest.raises(HookForbiddenError):
        await save(h, fake.cli(), form(commands="docker ps"), user=user)
    after = await stored(h)
    assert before is not None and after is not None and after.commands == before.commands
    assert await audit_of(h.db, "service.hook_save") == []


@pytest.mark.parametrize(
    ("situation", "reason"),
    [("no-socket", NOT_MOUNTED_REASON), ("no-daemon", DAEMON_UNREACHABLE_REASON)],
)
async def test_a_save_is_refused_while_docker_is_not_usable(
    harness: Harness, fake: FakeDocker, situation: str, reason: str
) -> None:
    h = harness
    before = await stored(h)
    assert before is not None
    cli = fake.cli(mounted=situation != "no-socket")
    if situation == "no-daemon":
        fake.switch("no-daemon")
    with pytest.raises(HookSettingsError) as raised:
        await save(h, cli, form(commands="docker ps"))
    assert raised.value.errors == {"form": f"{reason} {UNAVAILABLE_SUFFIX}"}
    after = await stored(h)
    assert after is not None and after.commands == before.commands
    assert await audit_of(h.db, "service.hook_save") == []


@pytest.mark.parametrize(
    ("commands", "message"),
    [
        ("sh -c id", "Line 1: the first word must be docker."),
        ("docker ps\npodman ps", "Line 2: the first word must be docker."),
        ("docker ps | grep x", "Line 1: shell operators are not supported."),
        ("docker ps && docker ps", "Line 1: shell operators are not supported."),
        ("docker ps > out", "Line 1: shell operators are not supported."),
        ("docker 'unclosed", "Line 1: a quote is not closed."),
    ],
)
async def test_every_line_is_validated_and_nothing_is_stored_on_a_problem(
    harness: Harness, fake: FakeDocker, commands: str, message: str
) -> None:
    h = harness
    before = await stored(h)
    assert before is not None
    with pytest.raises(HookSettingsError) as raised:
        await save(h, fake.cli(), form(commands=commands))
    assert raised.value.errors["commands"].startswith(message)
    after = await stored(h)
    assert after is not None and after.commands == before.commands


@pytest.mark.parametrize("kind", ["outside", "relative", "traversal", "missing", "file"])
async def test_the_working_directory_must_be_a_folder_inside_the_allowed_paths(
    harness: Harness, fake: FakeDocker, tmp_path: Path, kind: str
) -> None:
    h = harness
    outside = tmp_path / "outside"
    outside.mkdir()
    (h.roots["staging"] / "a-file").write_text("x", encoding="utf-8")
    directory = {
        "outside": str(outside),
        "relative": "compose",
        "traversal": f"{h.roots['staging']}/../../outside",
        "missing": str(h.roots["staging"] / "not-there"),
        "file": str(h.roots["staging"] / "a-file"),
    }[kind]
    with pytest.raises(HookSettingsError) as raised:
        await save(h, fake.cli(), form(working_directory=directory))
    assert set(raised.value.errors) == {"working_directory"}


@pytest.mark.parametrize("timeout", ["0", "-1", "abc", "1.5", "3601", "", " "])
async def test_the_timeout_must_be_a_whole_number_of_seconds_in_range(
    harness: Harness, fake: FakeDocker, timeout: str
) -> None:
    with pytest.raises(HookSettingsError) as raised:
        await save(harness, fake.cli(), form(timeout=timeout))
    assert raised.value.errors == {"timeout": TIMEOUT_MESSAGE}


async def test_every_problem_is_reported_at_once(harness: Harness, fake: FakeDocker) -> None:
    with pytest.raises(HookSettingsError) as raised:
        await save(
            harness,
            fake.cli(),
            form(commands="ls", working_directory="relative", timeout="x"),
        )
    assert set(raised.value.errors) == {"commands", "working_directory", "timeout"}


async def test_the_compose_check_runs_compose_config_in_the_working_directory(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    result = await check_compose_setup(
        h.db,
        fake.cli(),
        await h.scope_of("superadmin"),
        AuditActor.for_user(h.seed.users.superadmin),
        h.seed.services.staging_web.id,
        "",
    )
    assert result == ComposeCheck(True, CHECK_OK_MESSAGE)
    (call,) = fake.calls()
    assert call.arguments == ("compose", "config", "--quiet")
    assert call.working_directory == str(h.roots["staging"])
    (entry,) = await audit_of(h.db, "service.hook_check")
    assert entry.result is AuditResult.SUCCESS and entry.details == {
        "hook": "post_deploy",
        "ok": True,
    }


async def test_a_failing_compose_check_returns_its_truncated_output(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    other = h.roots["staging"] / "compose"
    other.mkdir()
    fake.behavior("echo 'no configuration file provided' >&2; exit 1")
    result = await check_compose_setup(
        h.db,
        fake.cli(),
        await h.scope_of("superadmin"),
        AuditActor.for_user(h.seed.users.superadmin),
        h.seed.services.staging_web.id,
        str(other),
    )
    assert result == ComposeCheck(False, CHECK_FAILED_MESSAGE, "no configuration file provided\n")
    assert fake.calls()[0].working_directory == str(other)
    (entry,) = await audit_of(h.db, "service.hook_check")
    assert entry.result is AuditResult.FAILURE and entry.details["ok"] is False


@pytest.mark.parametrize("kind", ["outside", "missing"])
async def test_the_compose_check_refuses_a_directory_it_may_not_use(
    harness: Harness, fake: FakeDocker, tmp_path: Path, kind: str
) -> None:
    h = harness
    directory = str(tmp_path if kind == "outside" else h.roots["staging"] / "nope")
    result = await check_compose_setup(
        h.db,
        fake.cli(),
        await h.scope_of("superadmin"),
        AuditActor.for_user(h.seed.users.superadmin),
        h.seed.services.staging_web.id,
        directory,
    )
    assert result.ok is False and result.output is None
    assert fake.calls() == []


async def test_the_compose_check_reports_unavailable_docker_without_running_it(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    result = await check_compose_setup(
        h.db,
        fake.cli(mounted=False),
        await h.scope_of("superadmin"),
        AuditActor.for_user(h.seed.users.superadmin),
        h.seed.services.staging_web.id,
        "",
    )
    assert result == ComposeCheck(False, f"{NOT_MOUNTED_REASON} {CHECK_UNAVAILABLE_SUFFIX}")
    assert fake.calls() == []


async def test_only_the_superadmin_may_run_the_compose_check(
    harness: Harness, fake: FakeDocker
) -> None:
    h = harness
    with pytest.raises(HookForbiddenError):
        await check_compose_setup(
            h.db,
            fake.cli(),
            await h.scope_of("admin"),
            AuditActor.for_user(h.seed.users.admin),
            h.seed.services.staging_web.id,
            "",
        )
    assert fake.calls() == []


async def test_an_unknown_service_is_refused(harness: Harness, fake: FakeDocker) -> None:
    with pytest.raises(HookSettingsError) as raised:
        await save(harness, fake.cli(), form(), service=999999)
    assert raised.value.errors == {"form": "Service not found."}
