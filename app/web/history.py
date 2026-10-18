"""File history routes: the version table, the masked diff, audited reveals and restore.

A render that is not an answer to a reveal never holds a value. Revealing the values of a diff
is a POST that is audited before the values leave, answers only the values and is never cached.
Restoring needs the edit capability and saves the chosen content as a new version; viewers can
do neither and every response to them is masked.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.templating import render
from app.services.permissions import CAPABILITY_GROUPS, Capability
from app.services.version_history import (
    RestoreError,
    VersionDiff,
    VersionNotFoundError,
    VersionRow,
    default_pair,
    load_diff,
    load_versions,
    pick_pair,
    restore_version,
    reveal_diff,
)
from app.web.dependencies import get_crypto, get_db
from app.web.editor import EnvironmentContext, environment_access

router = APIRouter()

PAGE = "pages/history.html"
DIFF_PANEL = "fragments/diff_panel.html"
DIFF_VALUES = "fragments/diff_values.html"
RESTORE_FORM = "fragments/restore_form.html"
MAX_NUMBER_DIGITS = 9


def _plain_number(value: str) -> str:
    """The value when it is a plain version number, else empty."""
    return value if value.isdecimal() and len(value) <= MAX_NUMBER_DIGITS else ""


def _requested(request: Request) -> list[int]:
    """The version numbers of the repeated ``v`` query field, ignoring anything not a number."""
    return [
        int(value)
        for value in request.query_params.getlist("v")
        if _plain_number(value) and int(value) > 0
    ]


async def _diff_context(
    db: AsyncSession,
    request: Request,
    access: EnvironmentContext,
    versions: tuple[VersionRow, ...],
    pair: tuple[int, int] | None,
) -> dict[str, Any]:
    """What the diff panel needs: the masked diff of the pair, or None when no pair is chosen."""
    diff: VersionDiff | None = None
    if pair is not None:
        diff = await load_diff(db, get_crypto(request), access.env, *pair)
    return {
        "env": access.env,
        "diff": diff,
        "single": len(versions) < 2,
        "latest": versions[0].number if versions else None,
    }


@router.get("/environments/{environment_id:int}/history", include_in_schema=False)
async def history_page(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    restored: str = "",
    source: str = "",
) -> Response:
    """The version table and the diff of the chosen pair, the two newest versions by default.

    ``restored`` and ``source`` (version numbers) show the toast after a restore redirects here.
    """
    versions = await load_versions(db, access.env)
    numbers = [version.number for version in versions]
    requested = _requested(request)
    pair = pick_pair(requested, numbers) if requested else default_pair(numbers)
    context = await _diff_context(db, request, access, versions, pair)
    return render(
        request,
        PAGE,
        {
            **context,
            "tab": "history",
            "versions": versions,
            "selected": pair or (),
            "is_viewer": access.scope.group not in CAPABILITY_GROUPS[Capability.EDIT_VALUES],
            "restored": _plain_number(restored),
            "source": _plain_number(source),
        },
    )


@router.get("/environments/{environment_id:int}/diff", include_in_schema=False)
async def diff_panel(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The diff panel for the versions checked in the table; a prompt unless exactly two."""
    versions = await load_versions(db, access.env)
    pair = pick_pair(_requested(request), [version.number for version in versions])
    return render(request, DIFF_PANEL, await _diff_context(db, request, access, versions, pair))


@router.post("/environments/{environment_id:int}/history/reveal", include_in_schema=False)
async def reveal_differences(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.REVEAL_SECRETS))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The values of the differing keys of two versions, audited before they are returned.

    The answer holds only the values, each aimed at its empty slot of the masked diff, and is
    never cached.
    """
    requested = sorted(set(_requested(request)))
    if len(requested) != 2:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "versions not found")
    try:
        revealed = await reveal_diff(
            db,
            get_crypto(request),
            access.env,
            requested[0],
            requested[1],
            actor=access.auth.user,
            ip_address=client_address(request),
        )
    except VersionNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "versions not found") from error
    return render(request, DIFF_VALUES, {"revealed": revealed.values})


@router.post("/environments/{environment_id:int}/restore/{number:int}", include_in_schema=False)
async def restore_environment(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.EDIT_VALUES))],
    db: Annotated[AsyncSession, Depends(get_db)],
    number: int,
    base: str = "",
) -> Response:
    """Save an older version's content as a new version, then reload the history.

    ``base`` is the newest version number the page was loaded with. A refusal swaps the dialog
    form with the reason (422).
    """
    base_number = int(_plain_number(base) or 0)
    try:
        version = await restore_version(
            db,
            get_crypto(request),
            access.env,
            number,
            base_number=base_number,
            actor=access.auth.user,
            ip_address=client_address(request),
        )
    except VersionNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found") from error
    except RestoreError as error:
        return render(
            request,
            RESTORE_FORM,
            {
                "env": access.env,
                "number": number,
                "latest": base_number,
                "error": str(error),
            },
            status_code=422,
        )
    return Response(
        status_code=200,
        headers={
            "HX-Redirect": (
                f"/environments/{access.env.id}/history?restored={version.number}&source={number}"
            ),
            "Cache-Control": "no-store",
            "Vary": "HX-Request",
        },
    )
