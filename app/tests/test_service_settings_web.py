"""The post-deploy command settings of the service page: who sees them and every state of them."""

import html
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, Deployment, HookType, Service, ServiceHook
from app.services.app_settings import PostDeploySettings, save_section
from app.services.audit import AuditActor
from app.services.docker_cli import NOT_MOUNTED_REASON, DockerStatus
from app.services.hook_settings import (
    CHECK_FAILED_MESSAGE,
    CHECK_OK_MESSAGE,
    CHECK_UNAVAILABLE_SUFFIX,
    TIMEOUT_MESSAGE,
    UNAVAILABLE_SUFFIX,
)
from app.tests.conftest import sign_in
from app.tests.dist_helpers import relocate_deployments
from app.tests.fake_docker import FakeDocker
from app.tests.web_helpers import HTMX, assert_never_cached, post
from app.web.service_detail import NOTHING_STORED_MESSAGE, SAVED_EMPTY_MESSAGE, SAVED_MESSAGE

STORED_COMMAND = "docker compose up -d --no-deps web"
NOT_MOUNTED_COPY = (
    "The Docker socket is not mounted, so post-deploy commands are off. These fields are "
    "disabled and saving is refused. Mount {socket} and recreate the container to enable the "
    "feature."
)
NON_SUPERADMINS = (
    "admin",
    "project_admin",
    "deployment_admin",
    "viewer",
    "env_scoped_admin",
    "env_scoped_viewer",
)
FORM = re.compile(r'<form id="postdeploy-settings".*?</form>', re.DOTALL)
STATUS_LINE = re.compile(r'<p data-fragment="docker-status".*?</p>', re.DOTALL)
ERROR_IDS = re.compile(r'<p id="([\w-]+-e)" role="alert"')
DIGEST = re.compile(r'name="reviewed" value="([0-9a-f]{64})"')


@dataclass(frozen=True)
class Ids:
    """Ids of the seeded services these tests use."""

    staging_web: int
    mysql: int
    web: int


class ForbiddenCli:
    """A docker CLI stand-in that fails the test when anything probes Docker."""

    socket_path = Path("/never/probed.sock")

    async def status(self) -> DockerStatus:
        """Fail: only the superadmin's view may ask for the status."""
        raise AssertionError("Docker was probed for a caller who may not see the settings")


@pytest.fixture
def fake(tmp_path: Path) -> FakeDocker:
    """A fake docker whose commands succeed and print one line."""
    return FakeDocker(tmp_path / "fake")


@pytest.fixture
def docker(web_app: FastAPI, fake: FakeDocker) -> FakeDocker:
    """The application on a usable fake Docker."""
    web_app.state.docker_cli = fake.cli()
    return fake


@pytest.fixture
async def roots(web_app: FastAPI, tmp_path: Path) -> dict[str, Path]:
    """The seeded deployments moved below a real folder that is the only allowed path."""
    async with web_app.state.session_factory() as db:
        return await relocate_deployments(db, tmp_path / "host")


@pytest.fixture
async def ids(web_app: FastAPI) -> Ids:
    """The seeded service ids."""
    async with web_app.state.session_factory() as db:
        rows = (
            await db.execute(
                select(Deployment.name, Service.name, Service.id).join(
                    Service, Service.deployment_id == Deployment.id
                )
            )
        ).all()
    found = {(deployment, service): service_id for deployment, service, service_id in rows}
    return Ids(
        staging_web=found[("staging", "web")],
        mysql=found[("prod", "mysql")],
        web=found[("prod", "web")],
    )


def service_for(ids: Ids, user: str) -> int:
    """A service the user can reach: prod mysql for the deployment and file scoped users."""
    scoped = {"deployment_admin", "env_scoped_admin", "env_scoped_viewer"}
    return ids.mysql if user in scoped else ids.staging_web


def form_of(page: str) -> str:
    """The command settings form of a page or panel."""
    found = FORM.search(page)
    assert found is not None, "the settings form is missing"
    return found.group(0)


def disabled_count(form: str) -> int:
    """How many elements of the form carry the disabled attribute."""
    return len(re.findall(r"<(?:input|textarea|button)\b[^>]*\sdisabled[\s>]", form))


def status_line_of(page: str) -> str:
    """The Docker status line of the panel header."""
    found = STATUS_LINE.search(page)
    assert found is not None, "the status line is missing"
    return found.group(0)


async def stored(web_app: FastAPI, service_id: int) -> ServiceHook | None:
    """The stored post-deploy hook of a service."""
    async with web_app.state.session_factory() as db:
        hook: ServiceHook | None = await db.scalar(
            select(ServiceHook).where(
                ServiceHook.service_id == service_id, ServiceHook.type == HookType.POST_DEPLOY
            )
        )
    return hook


async def entries(web_app: FastAPI, *actions: str) -> list[AuditEntry]:
    """The audit entries of the actions, oldest first."""
    async with web_app.state.session_factory() as db:
        return list(
            (
                await db.execute(
                    select(AuditEntry).where(AuditEntry.action.in_(actions)).order_by(AuditEntry.id)
                )
            ).scalars()
        )


def settings_data(**fields: str) -> dict[str, str]:
    """A valid form post with some fields replaced."""
    return {
        "working_directory": "",
        "commands": STORED_COMMAND,
        "timeout": "120",
        "run_only_if_changed": "on",
        **fields,
    }


async def test_the_superadmin_sees_the_status_line_and_the_enabled_form(
    web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    await sign_in(web_client, "root")
    response = await web_client.get(f"/services/{ids.staging_web}")
    page = response.text
    assert_never_cached(response)
    line = status_line_of(page)
    assert "Docker socket mounted" in line and 'data-status="deployed"' in line
    assert "daemon reachable · compose v2.40.0" in line
    assert "Open post-deploy settings" not in line

    form = form_of(page)
    assert disabled_count(form) == 0
    assert f'hx-post="/services/{ids.staging_web}/postdeploy"' in form
    assert f'hx-post="/services/{ids.staging_web}/postdeploy/test"' in form
    assert 'hx-include="closest form"' in form and 'hx-target="#test-result"' in form
    assert 'hx-status:422="swap:outerHTML target:#postdeploy-settings"' in form
    assert 'name="working_directory"' in form and 'placeholder="/opt/rentals/staging"' in form
    assert 'value=""' in form
    assert ">docker compose up -d --no-deps web</textarea>" in form
    assert 'name="timeout" type="number" inputmode="numeric" value="120"' in form
    assert 'name="run_only_if_changed" checked' in form
    assert ">Test command</button>" in form and ">Save settings</button>" in form
    assert "docker compose config --quiet checks the path and the Compose file." in form
    assert 'data-fragment="toast"' not in form and "Mount " not in form
    assert "Command settings" in form and "Superadmin" in form


async def test_a_service_without_a_command_shows_the_defaults(
    web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    await sign_in(web_client, "root")
    form = form_of((await web_client.get(f"/services/{ids.web}")).text)
    assert 'placeholder="/opt/rentals/prod"' in form
    assert ' rows="2"' in form and "></textarea>" in form
    assert 'value="120"' in form and 'name="run_only_if_changed" checked' in form


@pytest.mark.parametrize(
    ("case", "badge", "line", "note"),
    [
        ("no-socket", "Docker socket not mounted", None, NOT_MOUNTED_COPY),
        (
            "no-daemon",
            "Docker socket mounted",
            "daemon not reachable",
            "The Docker daemon does not answer through the socket. Post-deploy commands are off. "
            "These fields are disabled and saving is refused.",
        ),
        (
            "no-compose",
            "Docker socket mounted",
            "compose not available",
            "Docker Compose is not available in the app image. Post-deploy commands are off. "
            "These fields are disabled and saving is refused.",
        ),
    ],
)
async def test_without_usable_docker_the_form_is_disabled_and_explains_why(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    fake: FakeDocker,
    case: str,
    badge: str,
    line: str | None,
    note: str,
) -> None:
    cli = fake.cli(mounted=case != "no-socket")
    web_app.state.docker_cli = cli
    if case != "no-socket":
        fake.switch(case)
    await sign_in(web_client, "root")
    for url, headers in (
        (f"/services/{ids.staging_web}", {}),
        (f"/services/{ids.staging_web}/postdeploy", HTMX),
    ):
        page = (await web_client.get(url, headers=headers)).text
        status_line = status_line_of(page)
        assert badge in status_line and "Open post-deploy settings" in status_line
        assert 'href="/settings/post-deploy"' in status_line
        if line:
            assert line in status_line
        form = form_of(page)
        assert disabled_count(form) == 6, "four fields and two buttons"
        assert note.replace("{socket}", str(cli.socket_path)) in form
        assert 'role="note"' in form
        assert ">docker compose up -d --no-deps web</textarea>" in form


@pytest.mark.parametrize("user", NON_SUPERADMINS)
async def test_no_other_role_sees_the_status_or_the_form_or_makes_docker_run(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, user: str
) -> None:
    web_app.state.docker_cli = ForbiddenCli()
    service = service_for(ids, user)
    await sign_in(web_client, user)
    for url, headers in ((f"/services/{service}", {}), (f"/services/{service}/postdeploy", HTMX)):
        response = await web_client.get(url, headers=headers)
        assert response.status_code == 200
        body = response.text
        assert "Docker socket" not in body, url
        assert "postdeploy-settings" not in body and "Command settings" not in body
        assert "Open post-deploy settings" not in body and "Test command" not in body
        assert "docker compose up" not in body


@pytest.mark.parametrize("user", NON_SUPERADMINS)
async def test_no_other_role_may_save_or_test_and_nothing_changes(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    docker: FakeDocker,
    roots: dict[str, Path],
    user: str,
) -> None:
    service = service_for(ids, user)
    before = await stored(web_app, service)
    await sign_in(web_client, user)
    saved = await post(
        web_client, f"/services/{service}/postdeploy", settings_data(commands="docker ps")
    )
    tested = await post(
        web_client, f"/services/{service}/postdeploy/test", {"working_directory": ""}
    )
    assert (saved.status_code, tested.status_code) == (403, 403)
    after = await stored(web_app, service)
    assert (after.commands if after else None) == (before.commands if before else None)
    assert await entries(web_app, "service.hook_save", "service.hook_check") == []
    assert docker.calls() == []


async def test_a_service_outside_the_scope_answers_404_to_save_and_test(
    web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    await sign_in(web_client, "ungranted_admin")
    saved = await post(web_client, f"/services/{ids.mysql}/postdeploy", settings_data())
    tested = await post(web_client, f"/services/{ids.mysql}/postdeploy/test", {})
    assert (saved.status_code, tested.status_code) == (404, 404)


async def test_an_unknown_service_answers_404_to_the_superadmin(
    web_client: httpx.AsyncClient, docker: FakeDocker
) -> None:
    await sign_in(web_client, "root")
    saved = await post(web_client, "/services/999999/postdeploy", settings_data())
    tested = await post(web_client, "/services/999999/postdeploy/test", {})
    assert (saved.status_code, tested.status_code) == (404, 404)


async def test_the_superadmin_saves_the_settings_and_gets_a_toast_and_the_stored_values_back(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    docker: FakeDocker,
    roots: dict[str, Path],
) -> None:
    folder = roots["staging"] / "compose"
    folder.mkdir()
    await sign_in(web_client, "root")
    typed = {
        "working_directory": str(folder),
        "commands": "docker compose up -d web\r\n\r\n  docker ps  \r\n",
        "timeout": "300",
    }
    review = await post(web_client, f"/services/{ids.staging_web}/postdeploy", typed)
    assert review.status_code == 200
    assert_never_cached(review)
    asked = form_of(review.text)
    assert 'data-fragment="postdeploy-review"' in asked and "Review the commands" in asked
    assert [html.unescape(word) for word in re.findall(r"<code [^>]*>(.*?)</code>", asked)] == [
        "docker",
        "compose",
        "up",
        "-d",
        "web",
        "docker",
        "ps",
    ]
    assert "Confirm and save" in asked and "Save settings" not in asked
    assert 'data-fragment="toast"' not in asked
    before_review = await stored(web_app, ids.staging_web)
    assert before_review is not None and before_review.commands == STORED_COMMAND
    assert await entries(web_app, "service.hook_save") == []
    digest = DIGEST.search(asked)
    assert digest is not None

    response = await post(
        web_client,
        f"/services/{ids.staging_web}/postdeploy",
        {**typed, "reviewed": digest.group(1)},
    )
    assert response.status_code == 200
    assert_never_cached(response)
    form = form_of(response.text)
    assert 'data-fragment="postdeploy-review"' not in form and "Save settings" in form
    assert 'data-fragment="toast"' in form and "Post-deploy settings saved" in form
    assert SAVED_MESSAGE in form
    assert ">docker compose up -d web\ndocker ps</textarea>" in form
    assert f'value="{folder}"' in form and 'value="300"' in form
    assert 'name="run_only_if_changed" checked' not in form
    hook = await stored(web_app, ids.staging_web)
    assert hook is not None
    assert (hook.commands, hook.working_directory, hook.timeout_seconds) == (
        "docker compose up -d web\ndocker ps",
        str(folder),
        300,
    )
    assert hook.run_only_if_changed is False
    (entry,) = await entries(web_app, "service.hook_save")
    assert entry.actor_name == "root" and entry.service_id == ids.staging_web
    assert entry.details["changed"] == [
        "commands",
        "working_directory",
        "timeout",
        "run_only_if_changed",
    ]
    assert entry.details["previous_commands"] == STORED_COMMAND
    assert entry.details["new_commands"] == "docker compose up -d web\ndocker ps"
    assert str(folder) not in str(entry.details)

    unchanged = settings_data(
        working_directory=str(folder),
        commands="docker compose up -d web\ndocker ps",
        timeout="300",
    )
    del unchanged["run_only_if_changed"]
    again = await post(web_client, f"/services/{ids.staging_web}/postdeploy", unchanged)
    assert again.status_code == 200
    assert len(await entries(web_app, "service.hook_save")) == 1, "nothing changed, nothing audited"


async def test_commands_edited_after_the_review_or_a_made_up_digest_are_asked_again(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    await sign_in(web_client, "root")
    url = f"/services/{ids.staging_web}/postdeploy"
    first = await post(web_client, url, settings_data(commands="docker ps"))
    digest = DIGEST.search(first.text)
    assert digest is not None
    edited = await post(
        web_client, url, settings_data(commands="docker ps -a", reviewed=digest.group(1))
    )
    assert 'data-fragment="postdeploy-review"' in edited.text
    assert 'data-fragment="toast"' not in edited.text
    renewed = DIGEST.search(edited.text)
    assert renewed is not None and renewed.group(1) != digest.group(1)
    forged = await post(web_client, url, settings_data(commands="docker ps", reviewed="x" * 64))
    assert 'data-fragment="postdeploy-review"' in forged.text
    stored_hook = await stored(web_app, ids.staging_web)
    assert stored_hook is not None and stored_hook.commands == STORED_COMMAND
    assert await entries(web_app, "service.hook_save") == []


async def test_the_review_shows_every_argument_escaped_and_quoting_visible(
    web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    await sign_in(web_client, "root")
    typed = 'docker run --name \'a b\' --label "<b>x</b>" alpine "" {{.Names}}'
    response = await post(
        web_client, f"/services/{ids.staging_web}/postdeploy", settings_data(commands=typed)
    )
    asked = form_of(response.text)
    chips = [html.unescape(chip) for chip in re.findall(r"<code [^>]*>(.*?)</code>", asked)]
    assert chips == [
        "docker",
        "run",
        "--name",
        "a b",
        "--label",
        "<b>x</b>",
        "alpine",
        '<span aria-label="empty argument">""</span>',
        "{{.Names}}",
    ]
    assert "<b>x</b>" not in asked.replace("&lt;b&gt;x&lt;/b&gt;", "")


async def test_clearing_the_commands_is_saved_and_a_save_with_nothing_to_store_says_so(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    await sign_in(web_client, "root")
    cleared = await post(
        web_client, f"/services/{ids.staging_web}/postdeploy", settings_data(commands="  \n ")
    )
    assert cleared.status_code == 200 and SAVED_EMPTY_MESSAGE in cleared.text
    hook = await stored(web_app, ids.staging_web)
    assert hook is not None and hook.commands == ""

    nothing = await post(web_client, f"/services/{ids.web}/postdeploy", settings_data(commands=""))
    assert nothing.status_code == 200 and NOTHING_STORED_MESSAGE in nothing.text
    assert await stored(web_app, ids.web) is None
    assert [entry.service_id for entry in await entries(web_app, "service.hook_save")] == [
        ids.staging_web
    ]


@pytest.mark.parametrize(
    ("fields", "errors", "message"),
    [
        ({"commands": "ls -la"}, {"cmds-e"}, "Line 1: the first word must be docker."),
        ({"commands": "docker ps | cat"}, {"cmds-e"}, "shell operators are not supported"),
        ({"commands": "docker compose down"}, {"cmds-e"}, "Line 1: docker compose down is not"),
        ({"commands": "docker system prune -af"}, {"cmds-e"}, "prune commands are not allowed"),
        ({"commands": "docker ps\ndocker rm -fv web"}, {"cmds-e"}, "Line 2: the -v and --volumes"),
        ({"commands": "docker ps\n" * 11}, {"cmds-e"}, "Use at most 10 commands."),
        ({"timeout": "0"}, {"timeout-e"}, TIMEOUT_MESSAGE),
        ({"timeout": "3601"}, {"timeout-e"}, TIMEOUT_MESSAGE),
        ({"timeout": ""}, {"timeout-e"}, TIMEOUT_MESSAGE),
        (
            {"working_directory": "/etc"},
            {"wd-e"},
            "The working directory is outside the allowed paths.",
        ),
        (
            {"working_directory": "relative", "commands": "rm -rf", "timeout": "x"},
            {"wd-e", "cmds-e", "timeout-e"},
            TIMEOUT_MESSAGE,
        ),
    ],
)
async def test_a_refused_save_swaps_the_form_with_the_typed_values_and_the_reasons(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    docker: FakeDocker,
    roots: dict[str, Path],
    fields: dict[str, str],
    errors: set[str],
    message: str,
) -> None:
    await sign_in(web_client, "root")
    before = await stored(web_app, ids.staging_web)
    assert before is not None
    typed = settings_data(**fields)
    response = await post(web_client, f"/services/{ids.staging_web}/postdeploy", typed)
    assert response.status_code == 422
    assert_never_cached(response)
    form = form_of(response.text)
    assert set(ERROR_IDS.findall(form)) == errors
    assert message in form.replace("&#39;", "'")
    assert 'data-fragment="toast"' not in form
    assert 'aria-invalid="true"' in form
    if "commands" in fields:
        assert f">{fields['commands']}</textarea>" in form
    after = await stored(web_app, ids.staging_web)
    assert after is not None and after.commands == before.commands
    assert await entries(web_app, "service.hook_save") == []


async def test_a_save_while_docker_is_unusable_is_refused_with_the_reason(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, fake: FakeDocker
) -> None:
    web_app.state.docker_cli = fake.cli(mounted=False)
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/services/{ids.staging_web}/postdeploy", settings_data(commands="docker ps")
    )
    assert response.status_code == 422
    form = form_of(response.text)
    assert (
        'role="alert" class="flex items-center gap-1.5 text-[13px] font-semibold text-danger">'
    ) in form
    assert f"{NOT_MOUNTED_REASON} {UNAVAILABLE_SUFFIX}" in form
    assert disabled_count(form) == 6 and ">docker ps</textarea>" in form
    hook = await stored(web_app, ids.staging_web)
    assert hook is not None and hook.commands == STORED_COMMAND
    assert await entries(web_app, "service.hook_save") == []


async def test_the_compose_check_answers_a_success_line_and_audits_the_outcome(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    docker: FakeDocker,
    roots: dict[str, Path],
) -> None:
    folder = roots["staging"] / "compose"
    folder.mkdir()
    await sign_in(web_client, "root")
    response = await post(
        web_client,
        f"/services/{ids.staging_web}/postdeploy/test",
        {"working_directory": str(folder), "commands": "ignored", "timeout": "1"},
    )
    assert response.status_code == 200
    assert_never_cached(response)
    body = response.text.strip()
    assert body.startswith('<div id="test-result" data-fragment="test-result" aria-live="polite"')
    assert CHECK_OK_MESSAGE in body and "bg-ok-bg" in body and "<pre" not in body
    (call,) = docker.calls()
    assert call.arguments == ("compose", "config", "--quiet")
    assert call.working_directory == str(folder)
    (entry,) = await entries(web_app, "service.hook_check")
    assert entry.details == {"hook": "post_deploy", "ok": True}
    assert await stored(web_app, ids.staging_web) is not None


async def test_a_failed_compose_check_shows_the_reason_and_the_scrubbed_output(
    web_client: httpx.AsyncClient,
    ids: Ids,
    docker: FakeDocker,
    roots: dict[str, Path],
) -> None:
    docker.behavior('echo "invalid file with seed-secret and 3306 and db" >&2; exit 1')
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/services/{ids.mysql}/postdeploy/test", {"working_directory": ""}
    )
    assert response.status_code == 200
    body = response.text
    assert CHECK_FAILED_MESSAGE in body and "bg-err-bg" in body
    assert "invalid file with **** and **** and db" in body
    assert "seed-secret" not in body and "3306" not in body
    assert "<pre" in body and "hx-ignore" in body


async def test_the_compose_check_without_docker_runs_nothing_and_says_why(
    web_app: FastAPI,
    web_client: httpx.AsyncClient,
    ids: Ids,
    fake: FakeDocker,
    roots: dict[str, Path],
) -> None:
    web_app.state.docker_cli = fake.cli(mounted=False)
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/services/{ids.staging_web}/postdeploy/test", {"working_directory": ""}
    )
    assert response.status_code == 200
    assert f"{NOT_MOUNTED_REASON} {CHECK_UNAVAILABLE_SUFFIX}" in response.text
    assert "bg-err-bg" in response.text and "<pre" not in response.text
    assert fake.calls() == []
    (entry,) = await entries(web_app, "service.hook_check")
    assert entry.details["ok"] is False


async def test_the_compose_check_refuses_a_directory_outside_the_allowed_paths(
    web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker, roots: dict[str, Path]
) -> None:
    await sign_in(web_client, "root")
    response = await post(
        web_client, f"/services/{ids.staging_web}/postdeploy/test", {"working_directory": "/etc"}
    )
    assert "The working directory is outside the allowed paths." in response.text
    assert docker.calls() == []


async def test_the_status_line_says_when_the_settings_switch_is_off(
    web_app: FastAPI, web_client: httpx.AsyncClient, ids: Ids, docker: FakeDocker
) -> None:
    """With Docker usable the line adds a badge and a link once the switch is turned off."""
    await sign_in(web_client, "root")
    for url, headers in (
        (f"/services/{ids.staging_web}", {}),
        (f"/services/{ids.staging_web}/postdeploy", HTMX),
    ):
        assert "Switched off in settings" not in status_line_of(
            (await web_client.get(url, headers=headers)).text
        )
    async with web_app.state.session_factory() as db:
        await save_section(
            db, "post_deploy", PostDeploySettings(enabled=False), actor=AuditActor.cli()
        )
        await db.commit()
    for url, headers in (
        (f"/services/{ids.staging_web}", {}),
        (f"/services/{ids.staging_web}/postdeploy", HTMX),
    ):
        line = status_line_of((await web_client.get(url, headers=headers)).text)
        assert "Switched off in settings" in line and 'data-status="skipped"' in line
        assert 'href="/settings/post-deploy"' in line
