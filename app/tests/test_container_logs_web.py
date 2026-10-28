"""The Logs tab: who may open it, its states, the lines request (first read, cursor, filters, caps),
the masking of the deployment's own values, the download and the audit entries, on the seeded
application with a fake Docker."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditEntry, AuditResult, Deployment
from app.models.types import utc_now
from app.services.container_logs import (
    CUT_MARKER,
    MASK,
    MAX_LINE_CHARACTERS,
    MAX_LINES,
    NO_PROJECT_MESSAGE,
    format_ns,
    level_of,
    parse_log_line,
    service_of,
    timestamp_ns,
)
from app.services.docker_cli import DAEMON_UNREACHABLE_REASON
from app.services.docker_query import FAILED_MESSAGE, TOO_LARGE_MESSAGE
from app.tests.conftest import sign_in
from app.tests.fake_docker import FakeDocker
from app.tests.runtime_helpers import BEHAVIOR, LOGS_CASE, log_row, say_logs, serve, serve_logs
from app.tests.web_helpers import HTMX, add_version, environment_ids, stage_deployments

PROJECT = "fancy"
SECRET = "s3cr3t-value-4711"
FIRST = "2026-10-27T10:00:01.000000100Z"
SECOND = "2026-10-27T10:00:02.500000000Z"
THIRD = "2026-10-27T10:00:03.999999999Z"


@dataclass(frozen=True)
class Logs:
    """The prod deployment with a project, and the fake Docker that holds its log output."""

    prod: int
    fake: FakeDocker
    app: FastAPI

    def say(self, text: str) -> None:
        """Make ``docker compose logs`` print the text."""
        say_logs(self.fake, text)

    def forget_docker(self) -> None:
        """Give the application a new docker command, so the status and the containers it kept
        for a few seconds are read again."""
        self.app.state.docker_cli = self.fake.cli()

    def lines_url(self, **query: str) -> str:
        """The address of the lines request."""
        suffix = "&".join(f"{name}={value}" for name, value in query.items())
        return f"/deployments/{self.prod}/logs/lines" + (f"?{suffix}" if suffix else "")

    def log_calls(self) -> list[tuple[str, ...]]:
        """The arguments of the log reads."""
        return [call.arguments for call in self.fake.calls() if "logs" in call.arguments[:4]]


async def audited(app: FastAPI) -> list[AuditEntry]:
    """The audit entries of log views, oldest first."""
    async with app.state.session_factory() as db:
        rows = await db.execute(
            select(AuditEntry)
            .where(AuditEntry.action == "deployment.logs_view")
            .order_by(AuditEntry.id)
        )
        return list(rows.scalars())


@pytest.fixture
async def logs(web_app: FastAPI, tmp_path: Path) -> Logs:
    """The prod deployment with a project and a fake Docker with two services."""
    staged = await stage_deployments(web_app, tmp_path, "docker compose up -d")
    serve_logs(staged.docker)
    prod = staged.deployments["prod"]
    async with web_app.state.session_factory() as db:
        deployment = await db.get(Deployment, prod)
        assert deployment is not None
        deployment.compose_project = PROJECT
        await db.commit()
    found = Logs(prod, staged.docker, web_app)
    found.say("")
    return found


def lines_of(markup: str) -> list[str]:
    """The text of each line of an answer, with a masked value written as ``[masked]``."""
    found = re.findall(r"<div data-log-line.*?</div>", markup, re.DOTALL)
    texts = []
    for piece in found:
        message = re.findall(r"<span class=\"min-w-0.*?>(.*?)</span></div>", piece, re.DOTALL)
        text = re.sub(r"<[^>]+>", "", message[0] if message else "")
        texts.append(text)
    return texts


def services_of(markup: str) -> list[str]:
    """The service of each line of an answer."""
    return re.findall(r'<div data-log-line data-service="([^"]*)"', markup)


def cursor_of(markup: str) -> str:
    """The cursor the poller of an answer carries."""
    found = re.search(r'name="cursor" value="([^"]*)"', markup)
    assert found is not None
    return found.group(1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (log_row("api-1", FIRST, "started"), ("api", "started")),
        (log_row("api-1 ", FIRST, "started"), ("api", "started")),
        (log_row("web-frontend-12", FIRST, "two words"), ("web-frontend", "two words")),
        (log_row("api-1", FIRST, "a | pipe | in the text"), ("api", "a | pipe | in the text")),
        (log_row("custom_name", FIRST, "kept"), ("custom_name", "kept")),
        (f"api-1  | {FIRST}\n", ("api", "")),
        (f"api-1  | {FIRST}   indented", ("api", "  indented")),
    ],
)
def test_a_log_line_gives_its_service_its_time_and_its_text(
    raw: str, expected: tuple[str, str]
) -> None:
    parsed = parse_log_line(raw, ())
    assert parsed is not None
    moment, service, text = parsed
    assert (service, text) == expected
    assert moment == timestamp_ns(FIRST)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no prefix at all",
        "api-1  | no timestamp here",
        "api-1  | 2026-10-27 10:00:01 not rfc 3339",
        "  | 2026-10-27T10:00:01Z empty prefix",
        "Container demo-api-1 Started",
    ],
)
def test_a_line_that_is_not_a_log_line_is_left_out(raw: str) -> None:
    assert parse_log_line(raw, ()) is None


def test_a_prefix_is_the_service_when_it_names_one_and_is_cut_of_its_number_otherwise() -> None:
    assert service_of("web-2", ("web-2", "db")) == "web-2"
    assert service_of("web-2-1", ("web-2", "db")) == "web-2"
    assert service_of("db-3", ()) == "db"
    assert service_of("standalone", ()) == "standalone"


def test_stamps_round_trip_with_nine_digits_and_order_by_time() -> None:
    assert timestamp_ns("2026-10-27T10:00:01Z") == timestamp_ns("2026-10-27T10:00:01.000000000Z")
    assert timestamp_ns(FIRST) < timestamp_ns(SECOND) < timestamp_ns(THIRD)  # type: ignore[operator]
    assert format_ns(timestamp_ns(THIRD) or 0) == THIRD
    assert timestamp_ns("2026-13-45T10:00:01Z") is None
    assert timestamp_ns("yesterday") is None


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("ERROR: boom", "error"),
        ("fatal: out of memory", "error"),
        ("WARN slow query", "warn"),
        ("Warning: deprecated", "warn"),
        ("terrors are fine", ""),
        ("all good", ""),
    ],
)
def test_error_and_warning_lines_are_told_apart(text: str, level: str) -> None:
    assert level_of(text) == level


@pytest.mark.parametrize(
    ("user", "code"),
    [
        ("root", 200),
        ("project_admin", 200),
        ("deployment_admin", 200),
        ("env_scoped_admin", 403),
        ("viewer", 403),
        ("env_scoped_viewer", 403),
        ("ungranted_admin", 404),
    ],
)
async def test_who_may_open_the_logs(
    web_client: httpx.AsyncClient, logs: Logs, user: str, code: int
) -> None:
    """A project or a deployment grant is needed: the admin who reaches the deployment through a
    file only is refused, the viewer too, and a deployment the caller cannot see is not there."""
    logs.say(log_row("mysql-1", FIRST, "hello"))
    await sign_in(web_client, user)
    base = f"/deployments/{logs.prod}/logs"
    page = await web_client.get(base)
    lines = await web_client.get(logs.lines_url(), headers=HTMX)
    download = await web_client.get(f"{base}/download")
    assert (page.status_code, lines.status_code, download.status_code) == (code, code, code)
    assert len(logs.log_calls()) == (2 if code == 200 else 0)
    assert len(await audited(logs.app)) == (2 if code == 200 else 0)


async def test_a_request_with_no_session_is_refused(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    for path in ("", "/lines", "/download"):
        answered = await web_client.get(f"/deployments/{logs.prod}/logs{path}", headers=HTMX)
        assert answered.status_code == 401, path
    assert logs.log_calls() == []


@pytest.mark.parametrize(
    ("user", "shown"),
    [("root", True), ("project_admin", True), ("env_scoped_admin", False), ("viewer", False)],
)
async def test_the_tab_is_in_the_tabs_of_those_who_may_open_it(
    web_client: httpx.AsyncClient, logs: Logs, user: str, shown: bool
) -> None:
    await sign_in(web_client, user)
    page = await web_client.get(f"/deployments/{logs.prod}/files")
    assert page.status_code == 200
    assert (f'href="/deployments/{logs.prod}/logs"' in page.text) is shown


async def test_the_page_lists_the_services_with_a_container_and_asks_for_the_lines_when_shown(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{logs.prod}/logs")).text
    assert re.findall(r'data-log-service="([^"]*)"', page) == ["", "mysql", "web"]
    assert 'data-log-service=""' in page and "All services" in page
    assert "masked in the output" in page and "recorded in the audit log" in page
    poller = re.search(r'<div id="log-poller"[^>]*>', page)
    assert poller is not None and 'hx-trigger="load"' in poller.group(0)
    assert f'hx-get="/deployments/{logs.prod}/logs/lines"' in poller.group(0)
    assert 'name="cursor" value=""' in page
    assert 'id="log-lines" hx-ignore' in page
    assert '<option value="200" selected>Last 200 lines</option>' in page
    assert '<option value="1000">Last 1000 lines</option>' in page
    assert logs.log_calls() == [] and await audited(logs.app) == []


async def test_the_page_keeps_the_filter_of_its_address_and_ignores_a_service_it_does_not_have(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await sign_in(web_client, "root")
    address = f"/deployments/{logs.prod}/logs?service=web&q=boom&tail=1000"
    page = (await web_client.get(address)).text
    assert re.search(r'value="web" checked', page)
    assert 'value="boom"' in page and '<option value="1000" selected>' in page
    assert re.search(r'name="service" value="web"', page.split('id="log-poller"')[1])
    other = (await web_client.get(f"/deployments/{logs.prod}/logs?service=nope&tail=7")).text
    assert re.search(r'<input type="radio" name="service" value="" checked', other)
    assert '<option value="200" selected>' in other


async def test_the_page_says_so_without_a_project_without_docker_and_without_containers(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await sign_in(web_client, "root")
    url = f"/deployments/{logs.prod}/logs"
    page = (await web_client.get(url)).text
    assert "data-logs-state" not in page

    logs.fake.switch("no-daemon")
    logs.forget_docker()
    page = (await web_client.get(url)).text
    assert 'data-logs-state="unavailable"' in page and DAEMON_UNREACHABLE_REASON in page
    assert 'id="log-poller"' not in page
    logs.fake.switch("no-daemon", on=False)
    logs.forget_docker()

    serve(logs.fake, ps=[], inspect=[], network_ls=[], network_inspect=[])
    logs.forget_docker()
    page = (await web_client.get(url)).text
    assert 'data-logs-state="empty"' in page and "No containers in fancy" in page
    assert 'id="log-poller"' not in page

    async with logs.app.state.session_factory() as db:
        deployment = await db.get(Deployment, logs.prod)
        assert deployment is not None
        deployment.compose_project = None
        await db.commit()
    page = (await web_client.get(url)).text
    assert 'data-logs-state="no_project"' in page and "no Docker Compose project name" in page
    assert "Set the project name in Settings" in page
    assert 'id="log-poller"' not in page


async def test_the_first_read_asks_for_the_tail_of_each_container_and_is_audited_once(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.say(log_row("mysql-1", FIRST, "one") + log_row("web-1", SECOND, "two"))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(), headers=HTMX)
    assert answered.status_code == 200
    assert logs.log_calls() == [
        ("compose", "-p", PROJECT, "logs", "--timestamps", "--no-color", "--tail", "200")
    ]
    assert lines_of(answered.text) == ["one", "two"]
    assert services_of(answered.text) == ["mysql", "web"]
    assert cursor_of(answered.text) == SECOND
    assert 'hx-swap-oob="innerHTML:#log-lines"' in answered.text
    assert 'id="log-follow"' in answered.text and 'id="log-download"' in answered.text
    (entry,) = await audited(logs.app)
    assert entry.result is AuditResult.SUCCESS and entry.deployment_id == logs.prod
    assert entry.details == {"service": None, "tail": 200, "download": False}


async def test_a_poll_asks_since_the_cursor_gets_only_newer_lines_and_is_not_audited(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    """``--since`` includes the cursor's own moment, so the line stamped with it comes back and is
    dropped; what is older is dropped too."""
    logs.say(
        log_row("mysql-1", FIRST, "old")
        + log_row("web-1", SECOND, "at the cursor")
        + log_row("mysql-1", THIRD, "new")
    )
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(cursor=SECOND), headers=HTMX)
    assert lines_of(answered.text) == ["new"]
    assert cursor_of(answered.text) == THIRD
    assert 'hx-swap-oob="beforeend:#log-lines"' in answered.text
    assert 'id="log-download"' not in answered.text and 'id="log-follow"' not in answered.text
    assert logs.log_calls() == [
        (
            "compose",
            "-p",
            PROJECT,
            "logs",
            "--timestamps",
            "--no-color",
            "--tail",
            str(MAX_LINES),
            "--since",
            SECOND,
        )
    ]
    assert await audited(logs.app) == []


async def test_a_poll_with_nothing_new_keeps_the_cursor_and_appends_nothing(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.say(log_row("mysql-1", FIRST, "old") + log_row("mysql-1", THIRD, "at the cursor"))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(cursor=THIRD), headers=HTMX)
    assert lines_of(answered.text) == [] and cursor_of(answered.text) == THIRD
    assert 'hx-swap-oob="beforeend' not in answered.text
    logs.say("")
    empty = await web_client.get(logs.lines_url(), headers=HTMX)
    assert cursor_of(empty.text) == "" and 'hx-swap-oob="innerHTML:#log-lines"' in empty.text


async def test_lines_that_compose_prints_out_of_order_are_sorted_by_time(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.say(
        log_row("web-1", THIRD, "c")
        + log_row("mysql-1", FIRST, "a")
        + "Container demo-web-1 Started\n"
        + log_row("web-1", SECOND, "b")
    )
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(), headers=HTMX)
    assert lines_of(answered.text) == ["a", "b", "c"]
    assert cursor_of(answered.text) == THIRD


async def test_the_first_read_keeps_the_newest_lines_up_to_the_count_asked(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    base = timestamp_ns(FIRST) or 0
    logs.say("".join(log_row("mysql-1", format_ns(base + n), f"line {n}") for n in range(300)))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(tail="200"), headers=HTMX)
    shown = lines_of(answered.text)
    assert len(shown) == 200 and shown[0] == "line 100" and shown[-1] == "line 299"
    assert "data-log-omitted" not in answered.text
    assert "--tail 200" in " ".join(logs.log_calls()[0])
    wide = await web_client.get(logs.lines_url(tail="1000"), headers=HTMX)
    assert len(lines_of(wide.text)) == 300 and "--tail 1000" in " ".join(logs.log_calls()[1])
    other = await web_client.get(logs.lines_url(tail="77"), headers=HTMX)
    assert len(lines_of(other.text)) == 200


async def test_a_service_filter_names_the_service_to_docker(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.say(log_row("web-1", FIRST, "only web"))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(service="web"), headers=HTMX)
    assert logs.log_calls()[0][-1] == "web" and lines_of(answered.text) == ["only web"]
    assert 'name="service" value="web"' in answered.text
    (entry,) = await audited(logs.app)
    assert entry.details == {"service": "web", "tail": 200, "download": False}


@pytest.mark.parametrize("service", ["-rf", "a b", "../x", "x;y", "--tail"])
async def test_a_service_name_that_cannot_be_one_never_reaches_docker(
    web_client: httpx.AsyncClient, logs: Logs, service: str
) -> None:
    await sign_in(web_client, "root")
    for path in ("lines", "download"):
        answered = await web_client.get(
            f"/deployments/{logs.prod}/logs/{path}", params={"service": service}, headers=HTMX
        )
        assert answered.status_code == 404
    assert logs.log_calls() == []


@pytest.mark.parametrize(
    "cursor", ["yesterday", "2026-10-27", "1; rm", "2026-10-27T10:00:01+02:00"]
)
async def test_a_cursor_that_is_not_a_stamp_is_refused(
    web_client: httpx.AsyncClient, logs: Logs, cursor: str
) -> None:
    await sign_in(web_client, "root")
    answered = await web_client.get(
        f"/deployments/{logs.prod}/logs/lines", params={"cursor": cursor}, headers=HTMX
    )
    assert answered.status_code == 400 and logs.log_calls() == []


async def test_the_text_filter_ignores_case_and_runs_on_the_masked_text(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await add_version(logs.app, (await environment_ids(logs.app)).db, f"DB_PASSWORD={SECRET}\n")
    logs.say(
        log_row("mysql-1", FIRST, "Connection ESTABLISHED")
        + log_row("mysql-1", SECOND, f"password is {SECRET}")
        + log_row("web-1", THIRD, "established too")
    )
    await sign_in(web_client, "root")
    found = await web_client.get(logs.lines_url(q="established"), headers=HTMX)
    assert lines_of(found.text) == ["Connection ESTABLISHED", "established too"]
    assert cursor_of(found.text) == THIRD
    by_value = await web_client.get(logs.lines_url(q=SECRET), headers=HTMX)
    assert "data-log-line" not in by_value.text
    by_mask = await web_client.get(logs.lines_url(q="[masked]"), headers=HTMX)
    assert lines_of(by_mask.text) == ["password is [masked]"]
    quiet = await web_client.get(logs.lines_url(q="nothing", cursor=FIRST), headers=HTMX)
    assert lines_of(quiet.text) == [] and cursor_of(quiet.text) == THIRD


async def test_a_value_of_the_deployment_is_a_chip_and_never_in_the_clear(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await add_version(
        logs.app,
        (await environment_ids(logs.app)).db,
        f"DB_PASSWORD={SECRET}\nSHORT=ab\nPOOL=4\n",
    )
    logs.say(
        log_row("mysql-1", FIRST, f"login {SECRET} and again {SECRET}!")
        + log_row("mysql-1", SECOND, "pool 4 short ab")
        + log_row("web-1", THIRD, f"{SECRET}")
        + log_row("web-1", "2026-10-27T10:00:04Z", f"{SECRET[:-1]} is not the value")
        + log_row("web-1", "2026-10-27T10:00:05Z", f"private use {MASK} character")
    )
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(), headers=HTMX)
    assert SECRET not in answered.text and MASK not in answered.text
    assert lines_of(answered.text) == [
        "login [masked] and again [masked]!",
        "pool 4 short ab",
        "[masked]",
        f"{SECRET[:-1]} is not the value",
        "private use � character",
    ]
    assert answered.text.count("data-log-mask") == 3
    assert "data-log-mask" in answered.text and ">[masked]</span>" in answered.text


async def test_the_chip_replaces_the_value_of_any_environment_newest_and_previous_version(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    """The newest and the previous version of every file of the deployment count, whichever
    service it belongs to, since a container not recreated after a change still prints the old
    value. A value only an older version held is not masked: the documented limit."""
    ids = await environment_ids(logs.app)
    older, previous, newest = "old-value-1111", "prev-value-2222", "new-value-3333"
    for value in (older, previous, newest):
        await add_version(logs.app, ids.db, f"DB_PASSWORD={value}\n")
    logs.say(log_row("web-1", FIRST, f"{older} {previous} {newest}"))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(), headers=HTMX)
    assert lines_of(answered.text) == [f"{older} [masked] [masked]"]


async def test_a_long_line_is_cut_with_a_marker_after_it_was_masked(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await add_version(logs.app, (await environment_ids(logs.app)).db, f"DB_PASSWORD={SECRET}\n")
    long_text = SECRET + "x" * (MAX_LINE_CHARACTERS + 500)
    logs.say(log_row("web-1", FIRST, long_text))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(), headers=HTMX)
    (text,) = lines_of(answered.text)
    assert text.startswith("[masked]x") and text.endswith(CUT_MARKER)
    assert len(text) == len("[masked]") + MAX_LINE_CHARACTERS - 1 + len(CUT_MARKER)
    assert SECRET not in answered.text


async def test_an_update_has_at_most_the_cap_of_lines_and_says_how_many_it_left_out(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    base = timestamp_ns(FIRST) or 0
    total = MAX_LINES + 40
    logs.say("".join(log_row("web-1", format_ns(base + n), f"n{n}") for n in range(1, total + 1)))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(cursor=FIRST), headers=HTMX)
    shown = lines_of(answered.text)
    assert len(shown) == MAX_LINES and shown[-1] == f"n{total}"
    assert shown[0] == f"n{total - MAX_LINES + 1}"
    assert "40 older lines were left out" in answered.text


async def test_output_larger_than_the_read_limit_is_a_notice_and_keeps_the_box(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.fake.behavior(BEHAVIOR + LOGS_CASE.format('yes "web-1 | line" | head -c 5000000'))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(cursor=FIRST), headers=HTMX)
    assert answered.status_code == 200 and TOO_LARGE_MESSAGE in answered.text
    assert "data-log-notice" in answered.text and 'hx-swap-oob="beforeend' not in answered.text
    assert cursor_of(answered.text) == FIRST and "data-following" in answered.text


async def test_a_failed_read_is_a_notice_in_the_footer_and_the_poll_goes_on(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.fake.behavior(BEHAVIOR + LOGS_CASE.format('echo "secret detail" >&2; exit 1'))
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(cursor=THIRD), headers=HTMX)
    assert answered.status_code == 200
    assert f"The logs could not be read: {FAILED_MESSAGE}" in answered.text
    assert "secret detail" not in answered.text
    assert cursor_of(answered.text) == THIRD
    assert 'hx-trigger="every 2s"' in answered.text
    assert 'hx-swap-oob="beforeend' not in answered.text
    assert "innerHTML:#log-lines" not in answered.text
    assert await audited(logs.app) == []


async def test_a_failed_first_read_empties_the_box_asks_again_and_is_not_audited(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.fake.switch("no-daemon")
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(service="web"), headers=HTMX)
    assert DAEMON_UNREACHABLE_REASON in answered.text and cursor_of(answered.text) == ""
    assert 'hx-swap-oob="innerHTML:#log-lines"' in answered.text
    assert 'hx-trigger="every 2s"' in answered.text
    assert await audited(logs.app) == []


async def test_without_a_project_the_lines_request_says_so(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    async with logs.app.state.session_factory() as db:
        deployment = await db.get(Deployment, logs.prod)
        assert deployment is not None
        deployment.compose_project = None
        await db.commit()
    await sign_in(web_client, "root")
    answered = await web_client.get(logs.lines_url(), headers=HTMX)
    assert NO_PROJECT_MESSAGE in answered.text and logs.log_calls() == []


async def test_following_polls_every_two_seconds_and_paused_has_no_request_of_its_own(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.say(log_row("web-1", FIRST, "x"))
    await sign_in(web_client, "root")
    following = await web_client.get(logs.lines_url(cursor=FIRST, follow="1"), headers=HTMX)
    poller = re.search(r'<div id="log-poller"[^>]*>', following.text)
    assert poller is not None
    for expected in (
        f'hx-get="/deployments/{logs.prod}/logs/lines"',
        'hx-trigger="every 2s"',
        'hx-include="this"',
        'hx-sync="this:drop"',
        'hx-status:4xx="swap:none"',
        'hx-status:5xx="swap:none"',
        "data-background-refresh",
        "data-following",
    ):
        assert expected in poller.group(0), expected
    assert "Following, new lines every 2 s" in following.text
    assert 'name="follow" value="1"' in following.text

    resume = logs.lines_url(cursor=FIRST, follow="0", toggle="1")
    paused = await web_client.get(resume, headers=HTMX)
    poller = re.search(r'<div id="log-poller"[^>]*>', paused.text)
    assert poller is not None
    assert "hx-get" not in poller.group(0) and "hx-trigger" not in poller.group(0)
    assert "data-following" not in poller.group(0)
    assert "Paused" in paused.text and 'name="follow" value="0"' in paused.text
    assert cursor_of(paused.text) == FIRST
    button = re.search(r'<button type="button" id="log-follow"[^>]*>', paused.text)
    assert button is not None
    assert 'aria-pressed="false"' in button.group(0) and '"follow": "1"' in button.group(0)
    assert 'hx-swap-oob="true"' in button.group(0)
    assert await audited(logs.app) == []


async def test_the_follow_button_of_the_page_asks_for_the_lines_since_the_cursor_and_pauses(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await sign_in(web_client, "root")
    page = (await web_client.get(f"/deployments/{logs.prod}/logs")).text
    button = re.search(r'<button type="button" id="log-follow"[^>]*>', page)
    assert button is not None
    for expected in (
        'aria-pressed="true"',
        f'hx-get="/deployments/{logs.prod}/logs/lines"',
        'hx-include="#log-poller"',
        '"follow": "0"',
        '"toggle": "1"',
        'hx-sync="#log-poller:replace"',
    ):
        assert expected in button.group(0), expected
    form = re.search(r'<form id="log-form"[^>]*>', page)
    assert form is not None
    assert "input changed delay:400ms from:#log-q" in form.group(0)
    assert "change from:#log-chips" in form.group(0) and "change from:#log-tail" in form.group(0)
    assert 'hx-sync="#log-poller:replace"' in form.group(0)


async def test_the_download_is_a_masked_text_file_that_is_never_stored_by_a_cache(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await add_version(logs.app, (await environment_ids(logs.app)).db, f"DB_PASSWORD={SECRET}\n")
    logs.say(
        log_row("web-1", SECOND, f"token {SECRET}")
        + log_row("mysql-1", FIRST, "hello <b>world</b>")
        + log_row("web-1", THIRD, "bye")
    )
    await sign_in(web_client, "root")
    answered = await web_client.get(
        f"/deployments/{logs.prod}/logs/download", params={"q": "e", "tail": "1000"}
    )
    assert answered.status_code == 200
    assert answered.headers["content-type"].startswith("text/plain")
    assert answered.headers["content-disposition"] == (
        f'attachment; filename="deployment-{logs.prod}-logs.txt"'
    )
    assert answered.headers["cache-control"] == "no-store"
    assert answered.text == (
        f"{FIRST} mysql hello <b>world</b>\n{SECOND} web token [masked]\n{THIRD} web bye\n"
    )
    assert SECRET not in answered.text and MASK not in answered.text
    assert "--tail" in logs.log_calls()[0] and "1000" in logs.log_calls()[0]
    (entry,) = await audited(logs.app)
    assert entry.details == {"service": None, "tail": 1000, "download": True}


async def test_the_download_of_a_service_names_it_and_a_failed_read_is_a_409_with_no_entry(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    logs.say(log_row("web-1", FIRST, "x"))
    await sign_in(web_client, "root")
    answered = await web_client.get(f"/deployments/{logs.prod}/logs/download?service=web")
    assert 'filename="deployment-' in answered.headers["content-disposition"]
    assert answered.headers["content-disposition"].endswith('-logs-web.txt"')
    assert len(await audited(logs.app)) == 1
    logs.fake.switch("no-daemon")
    logs.forget_docker()
    failed = await web_client.get(f"/deployments/{logs.prod}/logs/download")
    assert failed.status_code == 409 and DAEMON_UNREACHABLE_REASON in failed.text
    assert failed.headers["cache-control"] == "no-store"
    assert "content-disposition" not in failed.headers
    assert len(await audited(logs.app)) == 1


async def test_a_session_of_polls_writes_one_entry_and_it_holds_no_line_and_no_value(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await add_version(logs.app, (await environment_ids(logs.app)).db, f"DB_PASSWORD={SECRET}\n")
    logs.say(log_row("web-1", FIRST, f"secret line {SECRET}"))
    await sign_in(web_client, "root")
    first = await web_client.get(logs.lines_url(q="secret"), headers=HTMX)
    cursor = cursor_of(first.text)
    for _ in range(3):
        await web_client.get(logs.lines_url(cursor=cursor, q="secret"), headers=HTMX)
    (entry,) = await audited(logs.app)
    kept = f"{entry.details} {entry.target_name} {entry.action}"
    assert SECRET not in kept and "secret line" not in kept and "secret" not in str(entry.details)
    assert len(logs.log_calls()) == 4


async def test_the_overview_rows_link_to_the_logs_of_their_service_for_those_who_may_read_them(
    web_client: httpx.AsyncClient, logs: Logs
) -> None:
    await sign_in(web_client, "root")
    fragment = (await web_client.get(f"/deployments/{logs.prod}/runtime", headers=HTMX)).text
    links = re.findall(r'<a href="([^"]*)"[^>]*data-row-action="logs"', fragment)
    assert sorted(set(links)) == [
        f"/deployments/{logs.prod}/logs?service=mysql",
        f"/deployments/{logs.prod}/logs?service=web",
    ]
    for user in ("viewer", "env_scoped_admin"):
        await sign_in(web_client, user)
        answered = await web_client.get(f"/deployments/{logs.prod}/runtime", headers=HTMX)
        assert answered.status_code == 200 and 'data-row-action="logs"' not in answered.text
    logs.fake.switch("no-daemon")
    logs.forget_docker()
    await sign_in(web_client, "root")
    down = (await web_client.get(f"/deployments/{logs.prod}/runtime", headers=HTMX)).text
    assert 'data-row-action="logs"' not in down


class MovingClock:
    """The application clock with an offset a test moves forward."""

    def __init__(self) -> None:
        """Start on the real time."""
        self.offset = timedelta()

    def __call__(self) -> datetime:
        """The real time plus the offset."""
        return utc_now() + self.offset

    def pass_minutes(self, minutes: float) -> None:
        """Move the clock forward."""
        self.offset += timedelta(minutes=minutes)


@pytest.fixture
def clock(logs: Logs) -> MovingClock:
    """The clock of the application, which the test moves."""
    moving = MovingClock()
    logs.app.state.clock = moving
    return moving


async def test_one_entry_per_user_deployment_and_service_filter_in_ten_minutes(
    web_client: httpx.AsyncClient, logs: Logs, clock: MovingClock
) -> None:
    """The first open audits; the same filter inside ten minutes does not; another service does;
    after ten minutes the same filter does again."""
    logs.say(log_row("web-1", FIRST, "x"))
    await sign_in(web_client, "root")
    await web_client.get(logs.lines_url(), headers=HTMX)
    assert len(await audited(logs.app)) == 1
    clock.pass_minutes(9)
    await web_client.get(logs.lines_url(), headers=HTMX)
    assert len(await audited(logs.app)) == 1
    await web_client.get(logs.lines_url(service="web"), headers=HTMX)
    assert [entry.details["service"] for entry in await audited(logs.app)] == [None, "web"]
    clock.pass_minutes(2)
    await web_client.get(logs.lines_url(), headers=HTMX)
    assert [entry.details["service"] for entry in await audited(logs.app)] == [None, "web", None]
    await web_client.get(logs.lines_url(service="web"), headers=HTMX)
    assert len(await audited(logs.app)) == 3
    clock.pass_minutes(10)
    await web_client.get(logs.lines_url(service="web"), headers=HTMX)
    assert len(await audited(logs.app)) == 4


async def test_a_text_filter_or_a_line_count_never_adds_an_entry(
    web_client: httpx.AsyncClient, logs: Logs, clock: MovingClock
) -> None:
    logs.say(log_row("web-1", FIRST, "x"))
    await sign_in(web_client, "root")
    await web_client.get(logs.lines_url(), headers=HTMX)
    for query in ("q=x", "q=xy", "tail=1000", "tail=1000&q=x"):
        clock.pass_minutes(1)
        answered = await web_client.get(f"{logs.lines_url()}?{query}", headers=HTMX)
        assert answered.status_code == 200
    (entry,) = await audited(logs.app)
    assert entry.details == {"service": None, "tail": 200, "download": False}
    assert len(logs.log_calls()) == 5


async def test_a_download_always_adds_an_entry_and_does_not_stand_for_an_open(
    web_client: httpx.AsyncClient, logs: Logs, clock: MovingClock
) -> None:
    logs.say(log_row("web-1", FIRST, "x"))
    await sign_in(web_client, "root")
    download = f"/deployments/{logs.prod}/logs/download"
    await web_client.get(download)
    await web_client.get(download)
    assert [entry.details["download"] for entry in await audited(logs.app)] == [True, True]
    await web_client.get(logs.lines_url(), headers=HTMX)
    await web_client.get(download)
    await web_client.get(logs.lines_url(), headers=HTMX)
    assert [entry.details["download"] for entry in await audited(logs.app)] == [
        True,
        True,
        False,
        True,
    ]


async def test_another_user_opening_the_same_filter_adds_an_entry_of_their_own(
    web_client: httpx.AsyncClient, logs: Logs, clock: MovingClock
) -> None:
    logs.say(log_row("web-1", FIRST, "x"))
    await sign_in(web_client, "root")
    await web_client.get(logs.lines_url(), headers=HTMX)
    await sign_in(web_client, "project_admin")
    await web_client.get(logs.lines_url(), headers=HTMX)
    await web_client.get(logs.lines_url(), headers=HTMX)
    entries = await audited(logs.app)
    assert len(entries) == 2 and entries[0].actor_user_id != entries[1].actor_user_id
