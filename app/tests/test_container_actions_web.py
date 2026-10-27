"""Start, stop, restart and recreate a container from the Overview: the confirmation, the command
each action runs (with and without linked Compose files), the refusals, who may use it, the
audit entries and what the answer does to the page, on the seeded application with a fake Docker.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.csrf import CSRF_COOKIE_NAME
from app.models import (
    AuditEntry,
    AuditResult,
    Deployment,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    Service,
    User,
)
from app.services import container_actions
from app.services.container_actions import (
    FILES_UNUSABLE_MESSAGE,
    NO_FILES_MESSAGE,
    NO_PROJECT_MESSAGE,
)
from app.services.deploy import LOCK_UNAVAILABLE_MESSAGE, LOCKED_MESSAGE, REGISTRY_UNREADABLE_REASON
from app.services.deploy_lock import hold_deploy_lock
from app.services.docker_cli import DAEMON_UNREACHABLE_REASON
from app.services.registries import add_registry
from app.services.versions import create_version
from app.tests.conftest import sign_in
from app.tests.fake_docker import CAPTURE_CONFIG, FakeDocker
from app.tests.runtime_helpers import BEHAVIOR, inspect_entry, ps_entry, serve
from app.tests.web_helpers import HTMX, environment_ids, events_of, post, stage_deployments

PROJECT = "fancy"
LONG_AGO = "2000-01-01T00:00:00Z"
COMPOSE = "compose.yaml"
COMPOSE_TEXT = "services:\n  mysql:\n    image: mysql:9\n  web:\n    image: nginx:1\n"
SECRET = "s3cr3t-value-4711"
REGISTRY = "registry.example.test"
TOKEN = "registry-token-4711"
ACTIONS = ("start", "stop", "restart", "recreate")
OK = {"x-container-test": "1"}


@dataclass(frozen=True)
class Stack:
    """The prod deployment on a real folder with a project, and the fake Docker of its stack."""

    prod: int
    root: Path
    fake: FakeDocker
    app: FastAPI

    def path(self, service: str, action: str, *, deployment: int | None = None) -> str:
        """The address of the dialog and of the post of an action."""
        return f"/deployments/{deployment or self.prod}/containers/{service}/{action}"

    def action_calls(self) -> list[tuple[str, ...]]:
        """The arguments of the commands that changed something, the reads left out."""
        reads = ("ps", "inspect", "network")
        return [
            call.arguments
            for call in self.fake.calls()
            if not (call.arguments[0] in reads or call.arguments[3:4] == ("ps",))
        ]

    def reads(self) -> int:
        """How many times the containers were listed."""
        return sum(1 for call in self.fake.calls() if "ps" in call.arguments[:4])


def behave(fake: FakeDocker, *, code: int = 0, script: str = "") -> None:
    """Answer the reads of the stack and give the action commands a behavior of their own."""
    fake.behavior(
        BEHAVIOR
        + 'case "$*" in\n  *" restart "*|*" start "*|*" stop "*|*" up "*)\n'
        + script
        + f"    exit {code} ;;\nesac\n"
    )


async def change(app: FastAPI, deployment: int, **fields: object) -> None:
    """Set fields of the deployment."""
    async with app.state.session_factory() as db:
        found = await db.get(Deployment, deployment)
        assert found is not None
        for name, value in fields.items():
            setattr(found, name, value)
        await db.commit()


@pytest.fixture
async def stack(web_app: FastAPI, tmp_path: Path) -> Stack:
    """The prod deployment with a project on a real folder: ``mysql`` running, ``web`` exited and
    marked to be recreated (its files were written after its container), ``redis`` running with
    no stored service."""
    staged = await stage_deployments(web_app, tmp_path, "docker compose up -d")
    fake = staged.docker
    serve(
        fake,
        ps=[
            ps_entry("mysql", 1, image="mysql:9"),
            ps_entry("web", 2, "exited", exit_code=137, image="nginx:1"),
            ps_entry("redis", 3, image="redis:8"),
        ],
        inspect=[
            inspect_entry(1, created="2026-10-27T08:00:00Z"),
            inspect_entry(
                2, status="exited", exit_code=137, finished="2026-10-27T09:30:00Z", created=LONG_AGO
            ),
            inspect_entry(3, created="2026-10-27T08:00:00Z"),
        ],
        network_ls=[],
        network_inspect=[],
    )
    behave(fake)
    prod = staged.deployments["prod"]
    await change(web_app, prod, compose_project=PROJECT)
    async with web_app.state.session_factory() as db:
        service = await db.get(Service, staged.services["prod/web"])
        assert service is not None
        run = DeployRun(
            service_id=service.id,
            service_name="web",
            deployment_name="prod",
            trigger=DeployTrigger.UI,
            status=DeployStatus.SUCCESS,
            started_at=datetime(2026, 10, 27, 9, 0, tzinfo=UTC),
        )
        db.add(run)
        await db.flush()
        db.add(
            DeployResult(
                run_id=run.id,
                environment_name=".env.web",
                target_path=".env.web",
                status=DeployResultStatus.WRITTEN,
                created_at=datetime(2026, 10, 27, 9, 5, tzinfo=UTC),
            )
        )
        await db.commit()
    return Stack(prod, staged.roots["prod"], fake, web_app)


async def link_files(stack: Stack) -> Path:
    """Write the Compose file into the prod folder and link it to the deployment."""
    (stack.root / COMPOSE).write_text(COMPOSE_TEXT, encoding="utf-8")
    await change(stack.app, stack.prod, compose_files=[COMPOSE])
    return stack.root


async def audited(app: FastAPI) -> list[AuditEntry]:
    """The audit entries of container actions, oldest first."""
    async with app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action.like("container.%")).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


def text_of(markup: str) -> str:
    """The visible text of a piece of markup, with the white space collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip()


def chips_of(markup: str) -> list[str]:
    """The arguments of the command the dialog shows, one per chip."""
    section = re.search(r'data-fragment="container-command".*?</div>', markup, re.DOTALL)
    assert section is not None
    return re.findall(r"<code[^>]*>(.*?)</code>", section.group(0))


@pytest.mark.parametrize(
    ("service", "action"),
    [("web", "start"), ("mysql", "stop"), ("mysql", "restart")],
)
async def test_without_linked_files_start_stop_and_restart_run_on_the_project_alone(
    web_client: httpx.AsyncClient, stack: Stack, service: str, action: str
) -> None:
    """The command has no file and no project directory, and runs in the private scratch folder
    so no Compose file of the application's own folder is picked up."""
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path(service, action))
    assert answered.status_code == 200
    assert stack.action_calls() == [("compose", "-p", PROJECT, action, service)]
    call = next(call for call in stack.fake.calls() if call.arguments[3] == action)
    assert call.working_directory == str((stack.fake.folder / "scratch").resolve())
    assert call.environment["COMPOSE_EXPERIMENTAL_GIT_REMOTE"] == "false"
    assert call.environment["DOCKER_HOST"] == f"unix://{stack.fake.socket_path}"
    assert "DOCKER_CONFIG" in call.environment and "ENVMANAGER_KEY" not in call.environment


@pytest.mark.parametrize(
    ("action", "verb"),
    [
        ("start", ("start", "mysql")),
        ("stop", ("stop", "mysql")),
        ("restart", ("restart", "mysql")),
        ("recreate", ("up", "-d", "--no-deps", "--force-recreate", "mysql")),
    ],
)
async def test_with_linked_files_every_action_runs_on_them(
    web_client: httpx.AsyncClient, stack: Stack, action: str, verb: tuple[str, ...]
) -> None:
    """Compose sees the same model as the user's own commands: the project directory, the project
    and each file, in the project directory."""
    root = await link_files(stack)
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("mysql", action))
    assert answered.status_code == 200
    expected = ("compose", "--project-directory", str(root), "-p", PROJECT)
    assert stack.action_calls() == [(*expected, "-f", str(root / COMPOSE), *verb)]
    call = next(call for call in stack.fake.calls() if call.arguments[-1] == "mysql")
    assert call.working_directory == str(root)


async def test_recreate_runs_in_a_private_docker_config_with_the_registry_logins(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The logins are written for the one command (file 0600 in a 0700 folder), the folder is
    gone afterwards, and the other actions get no login file."""
    async with stack.app.state.session_factory() as db:
        admin = await db.scalar(select(User).where(User.name == "root"))
        assert admin is not None
        await add_registry(
            db, stack.app.state.crypto, REGISTRY, "robot", TOKEN, actor=admin, ip_address=None
        )
    await link_files(stack)
    behave(stack.fake, script=CAPTURE_CONFIG)
    await sign_in(web_client, "root")
    await post(web_client, stack.path("mysql", "restart"))
    await post(web_client, stack.path("mysql", "recreate"))
    calls = stack.fake.calls()
    restart = next(i for i, call in enumerate(calls, 1) if call.arguments[-2] == "restart")
    recreate = next(i for i, call in enumerate(calls, 1) if "--force-recreate" in call.arguments)
    assert stack.fake.captured_config(restart) is None
    captured = stack.fake.captured_config(recreate)
    assert captured is not None
    assert list(captured.content["auths"]) == [REGISTRY]
    assert captured.file_mode == 0o600 and captured.folder_mode == 0o700
    folder = calls[recreate - 1].environment["DOCKER_CONFIG"]
    assert folder != str(stack.fake.folder / "scratch") and not Path(folder).exists()


async def test_an_unreadable_registry_token_stops_a_recreate_with_the_registry_reason(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    async with stack.app.state.session_factory() as db:
        admin = await db.scalar(select(User).where(User.name == "root"))
        assert admin is not None
        registry = await add_registry(
            db, stack.app.state.crypto, REGISTRY, "robot", TOKEN, actor=admin, ip_address=None
        )
        registry.token_encrypted = "not-a-token"
        await db.commit()
    await link_files(stack)
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("mysql", "recreate"))
    assert answered.status_code == 409 and REGISTRY_UNREADABLE_REASON in answered.text
    assert stack.action_calls() == []
    (entry,) = await audited(stack.app)
    assert entry.result is AuditResult.BLOCKED and entry.details["reason"] == (
        REGISTRY_UNREADABLE_REASON
    )


async def test_a_deploy_running_on_the_folder_refuses_the_action_with_the_deploy_sentence(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await sign_in(web_client, "root")
    with hold_deploy_lock(stack.app.state.lock_dir, str(stack.root)):
        opened = await web_client.get(stack.path("mysql", "restart"), headers=HTMX)
        answered = await post(web_client, stack.path("mysql", "restart"))
    assert opened.status_code == 200 and "container-command" in opened.text
    assert answered.status_code == 409 and LOCKED_MESSAGE in answered.text
    assert events_of(answered).keys() >= {"close-dialogs", "containers-changed"}
    assert stack.action_calls() == []
    (entry,) = await audited(stack.app)
    assert entry.action == "container.restart" and entry.result is AuditResult.BLOCKED
    assert entry.details["reason"] == LOCKED_MESSAGE and entry.details["service"] == "mysql"
    again = await post(web_client, stack.path("mysql", "restart"))
    assert again.status_code == 200 and len(stack.action_calls()) == 1


async def test_a_lock_folder_that_cannot_be_used_refuses_the_action(
    web_client: httpx.AsyncClient, stack: Stack, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    stack.app.state.lock_dir = blocker / "locks"
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("mysql", "stop"))
    assert answered.status_code == 409 and LOCK_UNAVAILABLE_MESSAGE in answered.text
    assert stack.action_calls() == []


async def test_each_refusal_says_why_in_the_dialog_and_the_post_and_runs_nothing(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """No project, Docker that does not answer and Recreate without usable Compose files."""
    await sign_in(web_client, "root")

    async def refused(service: str, action: str, reason: str) -> None:
        dialog = await web_client.get(stack.path(service, action), headers=HTMX)
        assert dialog.status_code == 200 and reason in dialog.text
        assert "data-container-refusal" in dialog.text and "<form" not in dialog.text
        assert 'data-js="dialog-close"' in dialog.text
        answered = await post(web_client, stack.path(service, action))
        assert answered.status_code == 409 and reason in answered.text
        assert 'data-fragment="toast"' in answered.text and 'role="alert"' in answered.text
        assert stack.action_calls() == []

    await refused("mysql", "recreate", NO_FILES_MESSAGE)
    (stack.root / COMPOSE).write_text(COMPOSE_TEXT, encoding="utf-8")
    await change(stack.app, stack.prod, compose_files=[COMPOSE])
    (stack.root / COMPOSE).unlink()
    await refused("mysql", "recreate", FILES_UNUSABLE_MESSAGE)
    await refused("mysql", "restart", FILES_UNUSABLE_MESSAGE)
    await change(stack.app, stack.prod, compose_files=None)

    stack.fake.switch("no-daemon")
    stack.app.state.docker_cli = stack.fake.cli()
    await refused("mysql", "restart", DAEMON_UNREACHABLE_REASON)
    stack.fake.switch("no-daemon", on=False)
    stack.app.state.docker_cli = stack.fake.cli()

    await change(stack.app, stack.prod, compose_project=None)
    await refused("mysql", "restart", NO_PROJECT_MESSAGE)
    assert [entry.result for entry in await audited(stack.app)] == [AuditResult.BLOCKED] * 5


@pytest.mark.parametrize("service", ["nope", "-rf", "a;b", "a b", "mysql%0Aweb", "web.", ".web"])
async def test_a_name_that_is_not_a_service_of_the_project_is_a_404_and_runs_nothing(
    web_client: httpx.AsyncClient, stack: Stack, service: str
) -> None:
    await sign_in(web_client, "root")
    for action in ACTIONS:
        dialog = await web_client.get(stack.path(service, action), headers=HTMX)
        answered = await post(web_client, stack.path(service, action))
        assert dialog.status_code == 404 and answered.status_code == 404
    assert stack.action_calls() == [] and await audited(stack.app) == []


async def test_an_action_that_does_not_exist_is_a_404(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await sign_in(web_client, "root")
    assert (await web_client.get(stack.path("mysql", "kill"), headers=HTMX)).status_code == 404
    assert (await post(web_client, stack.path("mysql", "remove"))).status_code == 404
    assert stack.action_calls() == []


@pytest.mark.parametrize(
    ("user", "code"),
    [
        ("root", 200),
        ("admin", 200),
        ("project_admin", 200),
        ("deployment_admin", 200),
        ("env_scoped_admin", 403),
        ("viewer", 403),
        ("env_scoped_viewer", 403),
        ("ungranted_admin", 404),
    ],
)
async def test_who_may_control_the_containers(
    web_client: httpx.AsyncClient, stack: Stack, user: str, code: int
) -> None:
    """A project or a deployment grant is needed: the admin who reaches the deployment through a
    file only is refused, the viewer too, and a deployment the caller cannot see is not there."""
    await sign_in(web_client, user)
    dialog = await web_client.get(stack.path("mysql", "restart"), headers=HTMX)
    answered = await post(web_client, stack.path("mysql", "restart"))
    assert (dialog.status_code, answered.status_code) == (code, code)
    assert len(stack.action_calls()) == (1 if code == 200 else 0)
    assert len(await audited(stack.app)) == (1 if code == 200 else 0)


async def test_a_request_with_no_session_is_refused(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await web_client.get("/healthz")
    headers = {**HTMX, "X-CSRFToken": web_client.cookies[CSRF_COOKIE_NAME]}
    opened = await web_client.get(stack.path("mysql", "restart"), headers=HTMX)
    assert opened.status_code in (303, 401)
    answered = await web_client.post(stack.path("mysql", "restart"), headers=headers)
    assert answered.status_code in (303, 401, 403) and stack.action_calls() == []


async def test_the_dialog_tells_the_effect_the_command_and_the_audit_of_each_action(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await link_files(stack)
    await sign_in(web_client, "root")
    base = ["docker", "compose", "--project-directory", str(stack.root), "-p", PROJECT]
    base += ["-f", str(stack.root / COMPOSE)]
    shown = {}
    for service, action in (
        ("mysql", "restart"),
        ("mysql", "stop"),
        ("web", "start"),
        ("mysql", "recreate"),
    ):
        response = await web_client.get(stack.path(service, action), headers=HTMX)
        assert response.status_code == 200 and "<html" not in response.text
        assert response.headers["cache-control"] == "no-store"
        shown[action] = response.text
        assert f"{action.capitalize()} {service}?" in response.text
        assert "It is recorded in the audit log." in text_of(response.text)
        assert "hx-config='{\"timeout\": 300000}'" in response.text
        assert f'hx-post="{stack.path(service, action)}"' in response.text
        assert 'hx-target="#container-toast"' in response.text
        assert chips_of(response.text)[: len(base)] == base
    assert chips_of(shown["restart"])[-2:] == ["restart", "mysql"]
    forced = ["up", "-d", "--no-deps", "--force-recreate", "mysql"]
    assert chips_of(shown["recreate"])[-5:] == forced
    restart = text_of(shown["restart"])
    assert "The mysql container of fancy stops and starts again" in restart
    assert "configuration it already has" in restart and "Recreate loads them" not in restart
    stop = text_of(shown["stop"])
    assert "stays stopped until someone starts it or a deploy recreates it" in stop
    assert "Services that call it get errors" in stop and "border-danger" in shown["stop"]
    assert "border-danger" not in shown["restart"] and "bg-accent" in shown["restart"]
    start = text_of(shown["start"])
    assert "starts with the configuration it already has. It exited with code 137." in start
    assert "does not load the files deployed" in start and "Recreate loads them." in start
    recreate = text_of(shown["recreate"])
    assert "replaces the mysql container of fancy using the linked Compose files" in recreate
    assert "Volumes are kept." in recreate and "unavailable for a few seconds" in recreate


async def test_without_linked_files_the_warning_of_a_stale_container_does_not_offer_recreate(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await sign_in(web_client, "root")
    start = text_of((await web_client.get(stack.path("web", "start"), headers=HTMX)).text)
    assert "does not load the files deployed" in start and "Recreate loads them" not in start
    assert "needs the Compose files linked" in start
    shown = (await web_client.get(stack.path("mysql", "restart"), headers=HTMX)).text
    assert chips_of(shown) == ["docker", "compose", "-p", PROJECT, "restart", "mysql"]
    assert "does not load" not in shown


async def test_a_success_closes_the_dialog_fires_the_events_and_drops_the_kept_read(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """The answer is a toast for the page, the dialog closes, the Overview is asked to refresh,
    and the containers are read again at the next refresh although five seconds have not passed."""
    await sign_in(web_client, "root")
    first = await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)
    again = await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)
    assert first.status_code == again.status_code == 200
    reads = stack.reads()
    assert reads == 1
    answered = await post(web_client, stack.path("mysql", "restart"))
    assert answered.status_code == 200
    assert events_of(answered) == {"close-dialogs": True, "containers-changed": True}
    assert answered.headers["cache-control"] == "no-store"
    toast = text_of(answered.text)
    assert 'data-fragment="toast"' in answered.text and 'role="status"' in answered.text
    assert "mysql restarted" in toast and "The mysql container of fancy was restarted." in toast
    reads_after = stack.reads()
    assert reads_after > reads
    await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)
    assert stack.reads() == reads_after + 1


@pytest.mark.parametrize("action", ACTIONS)
async def test_the_audit_entry_of_each_action_names_the_service_and_the_exit_code_only(
    web_client: httpx.AsyncClient, stack: Stack, action: str
) -> None:
    await link_files(stack)
    behave(stack.fake, script='    echo "secret output line"\n')
    await sign_in(web_client, "root")
    await post(web_client, stack.path("mysql", action))
    (entry,) = await audited(stack.app)
    assert entry.action == f"container.{action}" and entry.result is AuditResult.SUCCESS
    assert (entry.target_type, entry.target_id, entry.target_name) == (
        "deployment",
        stack.prod,
        "prod",
    )
    assert entry.deployment_id == stack.prod and entry.service_id is None
    assert entry.actor_name == "root" and entry.ip_address is not None
    assert entry.details == {"service": "mysql", "project": PROJECT, "exit_code": 0}
    assert "secret output" not in json.dumps(entry.details)


async def test_a_failed_command_shows_the_scrubbed_tail_and_is_audited_without_it(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """A value of the deployment's files and the registry token printed by Compose never reach the
    toast, the audit entry or the log; the exit code does."""
    async with stack.app.state.session_factory() as db:
        admin = await db.scalar(select(User).where(User.name == "root"))
        assert admin is not None
        await create_version(
            db,
            stack.app.state.crypto,
            (await environment_ids(stack.app)).db,
            f"DB_PASSWORD={SECRET}\n",
            author=admin,
        )
        await add_registry(
            db, stack.app.state.crypto, REGISTRY, "robot", TOKEN, actor=admin, ip_address=None
        )
        await db.commit()
    await link_files(stack)
    behave(
        stack.fake,
        code=3,
        script=f'    echo "pull access denied {TOKEN}"; echo "bad value {SECRET}" >&2\n',
    )
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("mysql", "recreate"))
    assert answered.status_code == 200 and 'role="alert"' in answered.text
    toast = text_of(answered.text)
    assert "Recreate of mysql failed" in toast and "docker compose exited with code 3." in toast
    assert "pull access denied ****" in toast and "bad value ****" in toast
    assert SECRET not in answered.text and TOKEN not in answered.text
    assert "hx-ignore" in answered.text
    (entry,) = await audited(stack.app)
    assert entry.result is AuditResult.FAILURE and entry.details["exit_code"] == 3
    kept = json.dumps(entry.details)
    assert "pull access" not in kept and SECRET not in kept


async def test_a_long_output_is_cut_to_its_tail(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    behave(stack.fake, code=1, script="    seq 1 5000 | sed 's/^/line /'\n")
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("mysql", "restart"))
    shown = re.search(r"<pre[^>]*>(.*?)</pre>", answered.text, re.DOTALL)
    assert shown is not None
    assert "line 5000" in shown.group(1) and "line 1\n" not in shown.group(1)
    assert len(shown.group(1)) <= container_actions.TAIL_CHARACTERS


async def test_a_command_that_runs_too_long_is_stopped_and_reported(
    web_client: httpx.AsyncClient, stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_actions, "SHORT_TIMEOUT_SECONDS", 1.0)
    behave(stack.fake, script="    echo started; exec sleep 30\n")
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("mysql", "restart"))
    assert answered.status_code == 200
    toast = text_of(answered.text)
    assert "Restart of mysql failed" in toast and "Timed out after 1 seconds" in toast
    (entry,) = await audited(stack.app)
    assert entry.result is AuditResult.FAILURE
    assert entry.details["timed_out"] is True and entry.details["exit_code"] is None


async def test_recreate_has_a_longer_deadline_than_the_other_actions() -> None:
    timeouts = {action: container_actions.ContainerAction(action).timeout for action in ACTIONS}
    assert timeouts == {"start": 60.0, "stop": 60.0, "restart": 60.0, "recreate": 180.0}
    assert container_actions.CONTROL_TIMEOUT_MS > 180_000


def rows_of(fragment: str) -> dict[str, str]:
    """The markup of each row of the services table, by service name."""
    return dict(
        re.findall(
            r'<tr data-fragment="service-summary-row" data-service="([^"]+)">(.*?)</tr>',
            fragment,
            re.DOTALL,
        )
    )


def controls_of(markup: str) -> list[str]:
    """The keys of the row actions of a row or an item."""
    return re.findall(r'data-row-action="([a-z]+)"', markup)


async def test_the_overview_rows_offer_the_controls_that_fit_each_container(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Restart and Stop for a running container, Start for an exited one, Recreate only when
    Compose files are linked and then the primary action of the row marked to be recreated."""
    await sign_in(web_client, "root")
    fragment = (await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)).text
    rows = rows_of(fragment)
    assert controls_of(rows["mysql"]) == ["open", "restart", "stop"]
    assert controls_of(rows["redis"]) == ["restart", "stop"]
    assert controls_of(rows["web"]) == ["open", "start"]
    assert f'hx-get="{stack.path("mysql", "restart")}"' in rows["mysql"]
    assert 'data-target="container-action"' in rows["mysql"]
    assert 'data-tone="danger"' in rows["mysql"].split('data-row-action="stop"')[1][:80]
    assert 'id="container-action"' not in fragment and 'id="container-toast"' not in fragment

    await link_files(stack)
    fragment = (await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)).text
    rows = rows_of(fragment)
    assert controls_of(rows["mysql"]) == ["open", "restart", "stop", "recreate"]
    assert controls_of(rows["web"]) == ["open", "start", "recreate"]
    web = rows["web"]
    assert re.search(r'<button[^>]*data-row-action="recreate"[^>]*class="[^"]*text-link', web)
    assert re.search(r'<a [^>]*data-row-action="open"[^>]*class="[^"]*size-ctl-md', web)
    mysql = rows["mysql"]
    assert not re.search(r'<button[^>]*data-row-action="recreate"[^>]*class="[^"]*text-link', mysql)


async def test_the_page_holds_the_dialog_and_the_toast_place_for_those_who_may_use_them(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{stack.prod}")).text
    assert 'id="container-action"' in page and 'id="container-toast"' in page
    refresh = re.search(r'<div id="runtime"[^>]*hx-trigger="([^"]*)"', page)
    assert refresh is not None and "containers-changed from:body" in refresh.group(1)
    await sign_in(web_client, "viewer")
    page = (await web_client.get(f"/deployments/{stack.prod}")).text
    assert 'id="container-action"' not in page and 'id="container-toast"' not in page


@pytest.mark.parametrize("user", ["viewer", "env_scoped_admin"])
async def test_those_who_may_not_control_the_containers_get_no_controls(
    web_client: httpx.AsyncClient, stack: Stack, user: str
) -> None:
    await link_files(stack)
    await sign_in(web_client, user)
    response = await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)
    if response.status_code == 200:
        assert 'data-target="container-action"' not in response.text
        assert not set(controls_of(response.text)) & {"start", "stop", "restart", "recreate"}


async def test_no_controls_without_a_usable_docker(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    stack.fake.switch("no-daemon")
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)
    assert response.status_code == 200 and "could not be read" in response.text
    assert not set(controls_of(response.text)) & {"start", "stop", "restart", "recreate"}


async def test_the_phone_list_carries_the_controls_in_the_row_menu(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await link_files(stack)
    await sign_in(web_client, "root")
    response = await web_client.get(f"/deployments/{stack.prod}/runtime", headers=HTMX)
    items = dict(
        re.findall(
            r'<li data-fragment="service-summary-item" data-service="([^"]+)"[^>]*>(.*?)</li>',
            response.text,
            re.DOTALL,
        )
    )
    assert "data-row-menu-button" in items["web"]
    assert controls_of(items["web"]) == ["open", "start", "recreate"]
    assert controls_of(items["redis"]) == ["restart", "stop", "recreate"]
    ids = re.findall(r' id="(service-summary[^"]*)"', response.text)
    assert len(ids) == len(set(ids))


async def test_opening_the_confirmation_runs_and_records_nothing(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    """Opening the confirmation reads the containers once and changes nothing."""
    await sign_in(web_client, "root")
    await web_client.get(stack.path("mysql", "restart"), headers=HTMX)
    assert stack.action_calls() == [] and await audited(stack.app) == []


async def test_a_compose_service_without_a_stored_service_can_be_controlled_too(
    web_client: httpx.AsyncClient, stack: Stack
) -> None:
    await sign_in(web_client, "root")
    answered = await post(web_client, stack.path("redis", "restart"))
    assert answered.status_code == 200
    assert stack.action_calls() == [("compose", "-p", PROJECT, "restart", "redis")]
