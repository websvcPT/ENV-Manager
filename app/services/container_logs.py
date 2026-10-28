"""Reading the logs of a deployment's containers for the Logs tab, with its own values masked
(unless masking is switched off in the security settings).

One read runs ``docker compose -p <project> logs --timestamps --no-color --tail <n>`` (``--since
<cursor>`` after the first read) through ``run_docker_query``, which keeps standard error apart
and bounds the bytes read. The project alone is enough: Compose finds the containers by the labels
they carry, so no linked Compose file is needed and none is read.

- Each line is ``<service>-<number>  | <timestamp> <message>``. The prefix names the service
  (or a custom container name, kept as it is), the timestamp is the one Docker stamped, and a line
  that has neither is not a log line and is left out.
- The cursor is the newest timestamp seen. ``--since`` includes that very moment, so the lines at
  or before the cursor are dropped, and the answer is sorted by timestamp because Compose
  interleaves the containers as they print, not in order.
- Every value of the deployment's files is replaced by a mask character before the lines are
  filtered, shortened or rendered, so the text filter never finds a value and a cut never leaves
  half of one. The scrubber (the newest and the previous version of every file) is built once per
  read, and not at all when nothing was read. With masking off nothing is decrypted and the
  lines are shown as the containers printed them. The lines are never stored and never logged.
- Hard caps: the lines of one answer, the characters of one line (a marker ends a cut line) and
  the bytes read from Docker.
"""

import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.models import AuditEntry, Deployment
from app.services.compose_project import is_compose_project_name
from app.services.docker_cli import DockerCli
from app.services.docker_query import run_docker_query
from app.services.scrub_values import CONTAINER_SCRUB_DEPTH, load_scrubber
from app.services.stack_discovery import NOT_USABLE_REASON

MASK = "\ue000"
MASK_TEXT = "[masked]"
TAIL_CHOICES = (200, 1000)
DEFAULT_TAIL = TAIL_CHOICES[0]
MAX_LINES = 1000
MAX_LINE_CHARACTERS = 2000
MAX_FILTER_CHARACTERS = 200
READ_BYTES = 4 * 1024 * 1024
READ_TIMEOUT_SECONDS = 10.0
CUT_MARKER = " [line cut]"
TONE_COUNT = 5

LOGS_VIEW_ACTION = "deployment.logs_view"
VIEW_AUDIT_WINDOW = timedelta(minutes=10)

NO_PROJECT_MESSAGE = (
    "This deployment has no Docker Compose project name, so its logs cannot be read."
)

_LINE = re.compile(
    r"^(?P<prefix>[^|\s][^|]*?)\s*\|\s(?P<stamp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z)"
    r"(?: (?P<text>.*))?$"
)
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z$")
_REPLICA = re.compile(r"^(?P<service>.+?)-(?P<number>\d+)$")
_ERROR = re.compile(r"\b(error|fatal|panic|critical|exception)\b", re.IGNORECASE)
_WARNING = re.compile(r"\bwarn(ing)?\b", re.IGNORECASE)
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class LogsUnavailableError(Exception):
    """The logs cannot be read now. The message is fixed text that is safe to show."""

    def __init__(self, message: str) -> None:
        """Keep the sentence to show."""
        super().__init__(message)
        self.message = message


def timestamp_ns(stamp: str) -> int | None:
    """The moment of an RFC 3339 UTC stamp in nanoseconds since the epoch, None when it is not
    one."""
    found = _STAMP.match(stamp)
    if found is None:
        return None
    try:
        moment = datetime.strptime(found.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    fraction = (found.group(2) or "").ljust(9, "0")
    return int(moment.timestamp()) * 1_000_000_000 + int(fraction)


def format_ns(value: int) -> str:
    """The stamp of a moment in nanoseconds, always with nine digits, as Docker writes it."""
    seconds, nanos = divmod(value, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{nanos:09d}Z"


@dataclass(frozen=True)
class LogLine:
    """One line of a container's log: when, which service, the masked text and its level.

    ``text`` holds ``MASK`` where a value of the deployment was replaced.
    """

    moment: int
    service: str
    text: str
    level: str = ""

    @property
    def stamp(self) -> str:
        """The moment as an RFC 3339 stamp with nine digits."""
        return format_ns(self.moment)

    @property
    def clock(self) -> str:
        """The time of day in UTC, to the second."""
        return self.stamp[11:19]

    @property
    def tone(self) -> int:
        """Which of the colours of the service name this line takes: the same service always
        gets the same one."""
        return zlib.crc32(self.service.encode("utf-8")) % TONE_COUNT

    @property
    def parts(self) -> list[str]:
        """The text between the masks: a mask sits between two neighbouring parts."""
        return self.text.split(MASK)

    @property
    def plain(self) -> str:
        """The text with each mask written as ``[masked]``."""
        return self.text.replace(MASK, MASK_TEXT)


def service_of(prefix: str, services: Sequence[str]) -> str:
    """The service a log prefix names: ``api-1`` is the service ``api``; a prefix that is not of
    that form (a custom container name) stays as it is."""
    if prefix in services:
        return prefix
    found = _REPLICA.match(prefix)
    return found.group("service") if found else prefix


def parse_log_line(raw: str, services: Sequence[str] = ()) -> tuple[int, str, str] | None:
    """The moment, service and text of one line of ``compose logs --timestamps``, or None for a
    line that is not one (no prefix, no timestamp)."""
    found = _LINE.match(raw.rstrip("\r\n"))
    if found is None:
        return None
    moment = timestamp_ns(found.group("stamp"))
    if moment is None:
        return None
    return moment, service_of(found.group("prefix").strip(), services), found.group("text") or ""


def level_of(text: str) -> str:
    """``error`` or ``warn`` when the text says so, else an empty string."""
    if _ERROR.search(text):
        return "error"
    return "warn" if _WARNING.search(text) else ""


def cut_text(text: str) -> str:
    """The text, ended with a marker when it is longer than a line may be."""
    if len(text) <= MAX_LINE_CHARACTERS:
        return text
    return text[:MAX_LINE_CHARACTERS].rstrip(MASK) + CUT_MARKER


def log_arguments(
    project: str, tail: int, since: int | None, service: str | None
) -> tuple[str, ...]:
    """The arguments of ``docker`` for one read. The tail is always given, so a long pause never
    makes the next read unbounded."""
    arguments = ["compose", "-p", project, "logs", "--timestamps", "--no-color"]
    arguments += ["--tail", str(tail)]
    if since is not None:
        arguments += ["--since", format_ns(since)]
    if service:
        arguments.append(service)
    return tuple(arguments)


@dataclass(frozen=True)
class LogRead:
    """What one read gave: the new lines (oldest first), the cursor to ask from next, and how
    many newer lines than the cap were left out."""

    lines: tuple[LogLine, ...]
    cursor: int | None
    omitted: int = 0


async def read_logs(
    db: AsyncSession,
    crypto: CryptoService,
    cli: DockerCli,
    deployment: Deployment,
    *,
    tail: int = DEFAULT_TAIL,
    cursor: int | None = None,
    service: str | None = None,
    query: str = "",
    services: Sequence[str] = (),
    mask: bool = True,
) -> LogRead:
    """Read the lines of the project after ``cursor`` (the last ``tail`` lines of each container
    when there is none), masked unless ``mask`` is False, filtered by ``query`` (case-insensitive,
    on the shown text) and sorted by time.

    ``cursor`` stays what it was when nothing newer was read. Raises ``LogsUnavailableError``
    when the project or Docker cannot be used and ``DockerQueryError`` when the read fails.
    """
    project = (deployment.compose_project or "").strip()
    if not project or not is_compose_project_name(project):
        raise LogsUnavailableError(NO_PROJECT_MESSAGE)
    if service is not None and SERVICE_NAME.fullmatch(service) is None:
        raise LogsUnavailableError("That service name cannot be read.")
    status = await cli.status()
    if not status.available:
        raise LogsUnavailableError(status.reason or NOT_USABLE_REASON)
    limit = tail if cursor is None else MAX_LINES
    output = await run_docker_query(
        cli,
        log_arguments(project, limit, cursor, service),
        timeout=READ_TIMEOUT_SECONDS,
        max_bytes=READ_BYTES,
    )
    parsed = []
    for raw in output.splitlines():
        line = parse_log_line(raw, services)
        if line is not None and (cursor is None or line[0] > cursor):
            parsed.append(line)
    parsed.sort(key=lambda line: line[0])
    if not parsed:
        return LogRead((), cursor)
    scrubber = None
    if mask:
        scrubber = await load_scrubber(db, crypto, deployment.id, depth=CONTAINER_SCRUB_DEPTH)
    newest = parsed[-1][0]
    needle = query.strip().lower()
    lines: list[LogLine] = []
    for moment, name, text in parsed:
        shown = text.replace(MASK, "\ufffd")
        if scrubber is not None:
            shown = scrubber.scrub_text(shown, MASK)
        shown = cut_text(shown)
        if needle and needle not in shown.replace(MASK, MASK_TEXT).lower():
            continue
        lines.append(LogLine(moment, name, shown, level_of(shown)))
    omitted = max(0, len(lines) - limit) if cursor is not None else 0
    return LogRead(tuple(lines[len(lines) - min(limit, len(lines)) :]), newest, omitted)


async def opened_recently(
    db: AsyncSession, user_id: int, deployment_id: int, service: str | None, now: datetime
) -> bool:
    """Whether the user already has an entry for opening these logs, with the same service
    filter, within ``VIEW_AUDIT_WINDOW`` before ``now``. A download has its own entries and does
    not count.

    The audit table is the memory, so the answer holds after a restart. One query reads the
    entries of the user for the deployment inside the window (the index on the time bounds it
    to the last few minutes of the table); the service is compared on the few rows it returns.
    """
    rows = await db.execute(
        select(AuditEntry.details).where(
            AuditEntry.action == LOGS_VIEW_ACTION,
            AuditEntry.actor_user_id == user_id,
            AuditEntry.deployment_id == deployment_id,
            AuditEntry.created_at > now - VIEW_AUDIT_WINDOW,
        )
    )
    return any(
        not details.get("download") and details.get("service") == service
        for details in rows.scalars()
    )
