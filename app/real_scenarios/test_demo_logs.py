"""The Logs tab on the real daemon: Compose's own line format, the cursor that Docker's ``--since``
honours down to the nanosecond, the filters and the masking of a value a container prints.

The web application runs in this process on the throwaway database of the ``world`` fixture and
the host Docker daemon. The ``db`` demo container prints a line that holds the value of its engine
setting, which is also stored in the deployment's files, and then the names of its variables; the
``api`` container prints the names of its own. The deployment has no linked Compose files, so every
read runs on the Compose project alone.
"""

import re
import time

import httpx

from app.real_scenarios.conftest import DemoStack, Workloads, World
from app.real_scenarios.test_demo_containers import start_stack
from app.services.container_logs import timestamp_ns
from app.tests.conftest import sign_in
from app.tests.web_helpers import HTMX

PRINTED = "starting with engine"
MASKED = f"{PRINTED} [masked]"
WAIT_SECONDS = 30.0


def lines_url(world: World, **query: str) -> str:
    """The address of the lines request of the demo deployment."""
    suffix = "&".join(f"{name}={value}" for name, value in query.items())
    return f"/deployments/{world.demo_seed.deployment.id}/logs/lines" + (
        f"?{suffix}" if suffix else ""
    )


def texts(markup: str) -> list[str]:
    """The text of each line of an answer, a masked value written as ``[masked]``."""
    found = re.findall(r"<div data-log-line.*?</div>", markup, re.DOTALL)
    pieces = [re.findall(r'<span class="min-w-0.*?>(.*?)</span></div>', piece) for piece in found]
    return [re.sub(r"<[^>]+>", "", "".join(message)) for message in pieces]


def services(markup: str) -> list[str]:
    """The service of each line of an answer."""
    return re.findall(r'<div data-log-line data-service="([^"]*)"', markup)


def cursor_of(markup: str) -> str:
    """The cursor of the poller of an answer."""
    found = re.search(r'name="cursor" value="([^"]*)"', markup)
    assert found is not None
    return found.group(1)


def engine_value(demo: DemoStack) -> str:
    """The value the db container reads from the file on disk, which it prints at its start."""
    for line in (demo.targets / ".env.db").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name == "DEMO_DB_ENGINE":
            return value
    raise AssertionError("the baseline .env.db has no DEMO_DB_ENGINE")


def wait_for_printed_lines(workloads: Workloads, count: int) -> str:
    """Wait until the db container printed its line ``count`` times and return its raw output."""
    (container,) = workloads.container_ids("db")
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        output = workloads.docker("logs", container).stdout
        if output.count(PRINTED) >= count:
            return output
        time.sleep(0.5)
    raise AssertionError(f"the db container never printed its line {count} times")


async def test_the_lines_of_the_real_containers_are_parsed_masked_and_followed_by_a_cursor(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    value = engine_value(demo)
    start_stack(workloads, demo)
    assert value in wait_for_printed_lines(workloads, 1)
    await sign_in(web, "root")

    first = await web.get(lines_url(world), headers=HTMX)
    assert first.status_code == 200 and "data-log-notice" not in first.text, first.text
    shown = texts(first.text)
    assert MASKED in shown and value not in first.text
    assert "DEMO_API_TOKEN" in shown and "DEMO_DB_USER" in shown
    assert {"api", "db"} <= set(services(first.text))
    assert first.text.count("data-log-mask") == 1
    stamps = re.findall(r'<time datetime="([^"]+)"', first.text)
    assert stamps == sorted(stamps) and all(timestamp_ns(stamp) for stamp in stamps)
    cursor = cursor_of(first.text)
    assert cursor == stamps[-1]

    quiet = await web.get(lines_url(world, cursor=cursor), headers=HTMX)
    assert quiet.status_code == 200 and "data-log-notice" not in quiet.text, quiet.text
    assert texts(quiet.text) == [] and cursor_of(quiet.text) == cursor

    (db,) = workloads.container_ids("db")
    restarted = workloads.docker("restart", db)
    assert restarted.returncode == 0, restarted.stderr
    assert value in wait_for_printed_lines(workloads, 2)
    time.sleep(1)
    appended = await web.get(lines_url(world, cursor=cursor), headers=HTMX)
    assert "data-log-notice" not in appended.text, appended.text
    assert texts(appended.text).count(MASKED) == 1
    assert set(services(appended.text)) == {"db"}
    assert timestamp_ns(cursor_of(appended.text)) > timestamp_ns(cursor)  # type: ignore[operator]
    assert value not in appended.text


async def test_a_service_and_a_text_filter_narrow_what_the_real_daemon_gave(
    demo: DemoStack, world: World, web: httpx.AsyncClient, workloads: Workloads
) -> None:
    value = engine_value(demo)
    start_stack(workloads, demo)
    assert value in wait_for_printed_lines(workloads, 1)
    await sign_in(web, "root")

    only_api = await web.get(lines_url(world, service="api"), headers=HTMX)
    assert set(services(only_api.text)) == {"api"}
    assert "DEMO_API_PORT" in texts(only_api.text) and MASKED not in texts(only_api.text)
    found = await web.get(lines_url(world, q="ENGINE"), headers=HTMX)
    assert texts(found.text) == [MASKED, "DEMO_DB_ENGINE"]
    by_mask = await web.get(lines_url(world, q="%5Bmasked%5D"), headers=HTMX)
    assert texts(by_mask.text) == [MASKED]
    by_value = await web.get(lines_url(world, q=value), headers=HTMX)
    assert texts(by_value.text) == []

    download = await web.get(
        f"/deployments/{world.demo_seed.deployment.id}/logs/download", params={"service": "db"}
    )
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    assert MASKED in download.text and value not in download.text
    assert all(" db " in line for line in download.text.splitlines())
