"""File editor routes: the page and its panel, masked rows, audited reveals and saving.

A render that is not an answer to a reveal never holds a value. Reveal is a POST that is audited
before the value leaves, answers only that value and is never cached. Saving needs the edit
capability; viewers cannot reveal or save and every response to them is masked.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from app.core.client_address import client_address
from app.core.templating import render
from app.services.access_grid import NodeKind
from app.services.editor import (
    EditorContent,
    EditorError,
    EditorHeader,
    KeyValueSubmission,
    VariableNotFoundError,
    VariableRow,
    load_editor_content,
    load_editor_header,
    reveal_content,
    reveal_value,
    save_version,
)
from app.services.permissions import CAPABILITY_GROUPS, AccessScope, Capability
from app.web.access_tabs import AccessSubject, mount_access_routes
from app.web.dependencies import (
    AuthContext,
    get_crypto,
    get_db,
    get_notifier,
    get_scope,
    require_auth,
)
from app.web.htmx import hx_redirect

router = APIRouter()

PAGE = "pages/editor.html"
PANEL = "fragments/editor_panel.html"
SAVE_BAR = "fragments/editor_save.html"
ROW = "fragments/editor_row.html"
ROW_EDIT = "fragments/editor_row_edit.html"
ROW_NEW = "fragments/editor_row_new.html"
VALUE = "fragments/editor_value.html"
RAW_VALUE = "fragments/editor_raw_value.html"
# A raw edit holds the whole file, and a browser sends each line break as several bytes once
# the form is url encoded; anything beyond this is refused before the body is parsed.
MAX_REQUEST_BYTES = 1024 * 1024
REQUEST_TOO_LARGE = f"The form is larger than {MAX_REQUEST_BYTES // 1024} KiB."


@dataclass(frozen=True)
class EnvironmentContext:
    """The caller, their scope and the environment as they see it."""

    auth: AuthContext
    scope: AccessScope
    env: EditorHeader


def environment_access(
    capability: Capability | None = None,
) -> Callable[..., Awaitable[EnvironmentContext]]:
    """A dependency that loads an environment for the caller and checks an optional capability.

    An environment outside the caller's scope answers 404 so its existence is not revealed; a
    capability the caller's group lacks on an environment in scope answers 403.
    """

    async def load(
        environment_id: int,
        auth: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> EnvironmentContext:
        env = await load_editor_header(db, scope, environment_id)
        if env is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "environment not found")
        if capability is not None and not scope.can(capability, env.item):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return EnvironmentContext(auth=auth, scope=scope, env=env)

    return load


async def _access_subject(
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.MANAGE_GRANTS))],
) -> AccessSubject:
    """The environment of an Access tab request, for the superadmin."""
    return AccessSubject(
        NodeKind.ENVIRONMENT,
        access.env.id,
        access.env.project_id,
        access.auth,
        "pages/environment_access.html",
        {
            "env": access.env,
            "tab": "access",
            "is_viewer": access.scope.group not in CAPABILITY_GROUPS[Capability.EDIT_VALUES],
        },
    )


mount_access_routes(router, NodeKind.ENVIRONMENT, _access_subject)


def _panel_context(
    access: EnvironmentContext,
    content: EditorContent,
    mode: str,
    error: str | None = None,
) -> dict[str, Any]:
    """What the panel needs: the header, names and sizes, the mode and the page's version.

    A file that is not env format can only be edited as raw text.
    """
    return {
        "env": access.env,
        "content": content,
        "mode": "raw" if mode == "raw" or not content.env_format else "kv",
        "version": content.number,
        "error": error,
    }


async def _content(db: AsyncSession, request: Request, access: EnvironmentContext) -> EditorContent:
    """Names and sizes of the newest version, or 404 for an environment with no version."""
    content = await load_editor_content(db, get_crypto(request), access.env.id)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no version saved")
    return content


def _saved_number(saved: str) -> str:
    """The saved version number from the query, empty unless it is a plain number."""
    return saved if saved.isdecimal() and len(saved) < 10 else ""


@router.get("/environments/{environment_id:int}", include_in_schema=False)
async def editor_page(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    mode: str = "kv",
    saved: str = "",
) -> Response:
    """The editor for one environment: masked variables or raw text, and the details panel.

    ``saved`` (a version number) shows the toast after a save redirects here.
    """
    content = await _content(db, request, access)
    return render(
        request,
        PAGE,
        {
            **_panel_context(access, content, mode),
            "tab": "edit",
            "is_viewer": access.scope.group not in CAPABILITY_GROUPS[Capability.EDIT_VALUES],
            "saved": _saved_number(saved),
        },
    )


@router.get("/environments/{environment_id:int}/panel", include_in_schema=False)
async def editor_panel(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    mode: str = "kv",
) -> Response:
    """The editor panel in one mode; also what Discard changes loads."""
    content = await _content(db, request, access)
    return render(request, PANEL, _panel_context(access, content, mode))


async def _row(
    db: AsyncSession, request: Request, access: EnvironmentContext, key: str
) -> tuple[VariableRow, int]:
    """The masked row of a variable and the version number, or 404."""
    content = await _content(db, request, access)
    row = next((row for row in content.rows if row.key == key), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variable not found")
    return row, content.number


@router.get("/environments/{environment_id:int}/rows/{key}", include_in_schema=False)
async def masked_row(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access())],
    db: Annotated[AsyncSession, Depends(get_db)],
    key: str,
) -> Response:
    """One masked row, what Cancel brings back after an edit."""
    row, number = await _row(db, request, access, key)
    return render(request, ROW, {"env": access.env, "row": row, "version": number})


@router.get("/environments/{environment_id:int}/rows/{key}/edit", include_in_schema=False)
async def edit_row(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.EDIT_VALUES))],
    db: Annotated[AsyncSession, Depends(get_db)],
    key: str,
) -> Response:
    """One row with an empty field for the new value; the old value is not sent."""
    row, number = await _row(db, request, access, key)
    return render(request, ROW_EDIT, {"env": access.env, "row": row, "version": number})


@router.get("/environments/{environment_id:int}/new-row", include_in_schema=False)
async def new_row(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.EDIT_VALUES))],
) -> Response:
    """A blank row for a new variable."""
    return render(request, ROW_NEW, {"env": access.env})


@router.post("/environments/{environment_id:int}/reveal/{key}", include_in_schema=False)
async def reveal_variable(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.REVEAL_SECRETS))],
    db: Annotated[AsyncSession, Depends(get_db)],
    key: str,
    version: int | None = None,
) -> Response:
    """The value of one variable, audited before it is returned and never cached."""
    try:
        value = await reveal_value(
            db,
            get_crypto(request),
            access.env,
            key,
            actor=access.auth.user,
            ip_address=client_address(request),
            version=version,
        )
    except VariableNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variable not found") from error
    return render(request, VALUE, {"value": value})


@router.post("/environments/{environment_id:int}/reveal-raw", include_in_schema=False)
async def reveal_raw(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.REVEAL_SECRETS))],
    db: Annotated[AsyncSession, Depends(get_db)],
    version: int | None = None,
) -> Response:
    """The whole text in an editable field, audited before it is returned and never cached."""
    try:
        text = await reveal_content(
            db,
            get_crypto(request),
            access.env,
            actor=access.auth.user,
            ip_address=client_address(request),
            version=version,
        )
    except VariableNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found") from error
    return render(
        request,
        RAW_VALUE,
        {"text": text, "line_count": text.count("\n") + 1},
    )


def _texts(form: FormData, name: str) -> list[str]:
    """Every text value of a repeated form field."""
    return [value for value in form.getlist(name) if isinstance(value, str)]


def _submission(form: FormData) -> KeyValueSubmission:
    """The key/value edits of a submitted form; the paired fields line up by position."""
    return KeyValueSubmission(
        replaced=list(zip(_texts(form, "edit_key"), _texts(form, "edit_value"), strict=False)),
        removed=_texts(form, "remove_key"),
        added=[
            (key, value)
            for key, value in zip(_texts(form, "new_key"), _texts(form, "new_value"), strict=False)
            if key.strip() or value
        ],
    )


def _save_error(
    request: Request, access: EnvironmentContext, form: FormData | None, message: str
) -> Response:
    """The save bar again with the reason, as a 422 swap that leaves the edited fields alone."""
    version = _version_field(form) if form is not None else 0
    mode = "raw" if form is not None and form.get("mode") == "raw" else "kv"
    return render(
        request,
        SAVE_BAR,
        {"env": access.env, "mode": mode, "version": version, "error": message},
        status_code=422,
    )


def _version_field(form: FormData) -> int:
    """The version number the page was loaded from, 0 when missing or not a number."""
    value = form.get("base_version")
    return int(value) if isinstance(value, str) and value.isdecimal() and len(value) < 10 else 0


@router.post("/environments/{environment_id:int}/versions", include_in_schema=False)
async def save_environment(
    request: Request,
    access: Annotated[EnvironmentContext, Depends(environment_access(Capability.EDIT_VALUES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Save the raw text or the key/value edits as a new version, then reload the editor.

    The form is read after the access check and the size check. A refusal swaps the save bar
    with the reason (422) and keeps what was typed.
    """
    length = request.headers.get("content-length", "")
    if not length.isdecimal():
        raise HTTPException(status.HTTP_411_LENGTH_REQUIRED, "content length required")
    if int(length) > MAX_REQUEST_BYTES:
        return _save_error(request, access, None, REQUEST_TOO_LARGE)
    form = await request.form()
    raw = form.get("mode") == "raw"
    content = form.get("content")
    comment = form.get("comment")
    try:
        version = await save_version(
            db,
            get_crypto(request),
            access.env,
            actor=access.auth.user,
            ip_address=client_address(request),
            base_number=_version_field(form),
            comment=comment if isinstance(comment, str) else "",
            raw_content=(content if isinstance(content, str) else None) if raw else None,
            key_values=None if raw else _submission(form),
            notifier=get_notifier(request),
        )
    except EditorError as error:
        return _save_error(request, access, form, str(error))
    return hx_redirect(f"/environments/{access.env.id}?saved={version.number}")
