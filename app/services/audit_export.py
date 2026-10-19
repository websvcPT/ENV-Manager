"""CSV export of the audit log: the same scope and filters as the screen, safe for spreadsheets."""

import csv
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditResult, User
from app.services.audit import AuditActor, record_actor_audit
from app.services.audit_filters import AuditFilters, Cursor
from app.services.audit_log import AuditRow, count_entries, list_page, newest_entry_id
from app.services.permissions import Visibility

EXPORT_BATCH = 500
EXPORT_ACTION = "audit.export"
CSV_COLUMNS = (
    "id",
    "time_utc",
    "actor",
    "actor_type",
    "source_ip",
    "action",
    "object_type",
    "object",
    "result",
    "version_ids",
    "details",
)

# A cell that starts with one of these is read as a formula by spreadsheet programs.
_FORMULA_FIRST = ("=", "+", "-", "@")
_FORMULA_RAW_FIRST = ("\t", "\r")


def csv_cell(value: object) -> str:
    """A cell as text that a spreadsheet will not run as a formula.

    A cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return (also after
    leading spaces) gets a leading apostrophe, which spreadsheets treat as the text marker.
    """
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_RAW_FIRST) or text.lstrip(" ").startswith(_FORMULA_FIRST):
        return f"'{text}"
    return text


def _cells(row: AuditRow) -> list[str]:
    """The cells of one entry, in column order."""
    return [
        csv_cell(value)
        for value in (
            row.id,
            row.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            row.actor,
            row.actor_kind,
            row.ip_address,
            row.action,
            row.object_type,
            row.object_name,
            row.result.value,
            " ".join(str(version_id) for version_id in row.version_ids),
            row.details_json,
        )
    ]


def _lines(rows: list[list[str]]) -> str:
    """CSV text for rows of cells, with CRLF line ends."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    return buffer.getvalue()


async def export_chunks(
    session_factory: async_sessionmaker[AsyncSession],
    visibility: Visibility,
    filters: AuditFilters,
    up_to_id: int | None = None,
) -> AsyncIterator[str]:
    """The CSV in pieces, newest entry first, read in batches on a session of its own.

    ``up_to_id`` fixes the set of entries: nothing written after it is read.

    The session is opened here because the request's session is not guaranteed to outlive the
    response; ``visibility`` was computed for the caller before the stream started.
    """
    yield _lines([list(CSV_COLUMNS)])
    cursor: Cursor | None = None
    async with session_factory() as db:
        while True:
            page = await list_page(
                db, visibility, filters, cursor=cursor, limit=EXPORT_BATCH, up_to_id=up_to_id
            )
            if page.rows:
                yield _lines([_cells(row) for row in page.rows])
            if page.next_cursor is None:
                return
            cursor = Cursor.parse(page.next_cursor)


@dataclass(frozen=True)
class ExportPlan:
    """What an audited export covers: how many entries, and the newest id it may read."""

    rows: int
    up_to_id: int


async def record_export(
    db: AsyncSession,
    visibility: Visibility,
    user: User,
    filters: AuditFilters,
    ip_address: str | None,
) -> ExportPlan:
    """Audit an export before it starts and return what it covers.

    The entry has no item scope, so only the superadmin sees that an admin exported. Its details
    are the filters and the row count, never entries. The export reads only up to the newest
    entry that existed before this one, so the file matches the recorded count. The caller
    commits.
    """
    up_to_id = await newest_entry_id(db)
    rows = await count_entries(db, visibility, filters, up_to_id=up_to_id)
    await record_actor_audit(
        db,
        AuditActor.for_user(user, ip_address),
        action=EXPORT_ACTION,
        result=AuditResult.SUCCESS,
        target_type="audit",
        target_name="audit log",
        details={"filters": filters.params(), "rows": rows},
    )
    return ExportPlan(rows, up_to_id)
