"""Audit log routes: the page, its table and load more fragments, one entry and the CSV export.

Admins and the superadmin may open them, viewers get a 403. Every answer is limited to the
entries the caller may see, and filters live in the URL: the table fragment answers
``HX-Push-Url`` so a reload or the back button fetches the same view as a full page.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.models.types import utc_now
from app.services.audit_export import export_chunks, record_export
from app.services.audit_filters import AuditFilters, Cursor, parse_number
from app.services.audit_log import (
    AuditPage,
    FilterOptions,
    list_page,
    load_entry,
    load_filter_options,
)
from app.services.permissions import AccessScope, Capability, Visibility, compute_visibility
from app.web.dependencies import AuthContext, get_db, get_scope, require_capability

router = APIRouter()

AUDIT_PATH = "/audit"
EXPORT_PATH = "/audit/export.csv"
ROWS_PATH = "/audit/rows"


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _table_context(
    visibility: Visibility, filters: AuditFilters, options: FilterOptions, page: AuditPage
) -> dict[str, Any]:
    """What the filter form, the table and the export link need."""
    query = filters.query_string()
    return {
        "scoped": not visibility.unrestricted,
        "filters": filters,
        "options": options,
        "page": page,
        "export_href": f"{EXPORT_PATH}?{query}" if query else EXPORT_PATH,
        "more_href": more_href(filters, page),
    }


def more_href(filters: AuditFilters, page: AuditPage) -> str | None:
    """The address of the next page of the same view, or None on the last page."""
    if page.next_cursor is None:
        return None
    return f"{ROWS_PATH}?{filters.query_string(before=page.next_cursor)}"


@router.get(AUDIT_PATH, include_in_schema=False)
async def audit_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    _context: Annotated[AuthContext, Depends(require_capability(Capability.VIEW_AUDIT))],
) -> Response:
    """The audit log, or with ``entry`` the one entry shown expanded.

    An entry that does not exist, is outside the caller's scope or is not a number all answer
    the same not-found state, so the page never says whether an entry exists.
    """
    visibility = await compute_visibility(db, scope)
    params = request.query_params
    if "entry" in params:
        entry_id = parse_number(params.get("entry"))
        row = await load_entry(db, visibility, entry_id) if entry_id is not None else None
        return render(
            request,
            "pages/audit_entry.html",
            {"row": row},
            status_code=200 if row is not None else 404,
        )
    filters = AuditFilters.parse(params)
    options = await load_filter_options(db, visibility)
    page = await list_page(db, visibility, filters)
    return render(request, "pages/audit.html", _table_context(visibility, filters, options, page))


@router.get("/audit/table", include_in_schema=False)
async def audit_table(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    _context: Annotated[AuthContext, Depends(require_capability(Capability.VIEW_AUDIT))],
) -> Response:
    """The results panel for the filters of the form, plus the refreshed export link.

    The answer sets ``HX-Push-Url`` to the address of the same view as a full page.
    """
    visibility = await compute_visibility(db, scope)
    filters = AuditFilters.parse(request.query_params)
    page = await list_page(db, visibility, filters)
    context = _table_context(visibility, filters, FilterOptions((), (), (), ()), page)
    response = render(request, "fragments/audit_table_update.html", context)
    query = filters.query_string()
    response.headers["HX-Push-Url"] = f"{AUDIT_PATH}?{query}" if query else AUDIT_PATH
    return response


@router.get(ROWS_PATH, include_in_schema=False)
async def audit_rows(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    _context: Annotated[AuthContext, Depends(require_capability(Capability.VIEW_AUDIT))],
) -> Response:
    """The next rows after the cursor of ``before`` and the next load more row.

    A missing or malformed cursor answers the first page again.
    """
    visibility = await compute_visibility(db, scope)
    filters = AuditFilters.parse(request.query_params)
    cursor = Cursor.parse(request.query_params.get("before"))
    page = await list_page(db, visibility, filters, cursor=cursor)
    context = _table_context(visibility, filters, FilterOptions((), (), (), ()), page)
    return render(request, "fragments/audit_more.html", context)


@router.get(EXPORT_PATH, include_in_schema=False)
async def audit_export(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[AccessScope, Depends(get_scope)],
    context: Annotated[AuthContext, Depends(require_capability(Capability.VIEW_AUDIT))],
) -> Response:
    """The entries of the current filters as a CSV download.

    The export is audited first, with the filters and the row count; the file holds the same
    entries the screen would show the caller, with spreadsheet formulas neutralized.
    """
    visibility = await compute_visibility(db, scope)
    filters = AuditFilters.parse(request.query_params)
    plan = await record_export(db, visibility, context.user, filters, _client_ip(request))
    await db.commit()
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        export_chunks(request.app.state.session_factory, visibility, filters, plan.up_to_id),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="audit-log-{stamp}.csv"',
            "Cache-Control": "no-store",
            "Vary": "HX-Request",
        },
    )
