"""API token routes: the list, the create dialog with its one-time secret and revoking.

Admins manage their own tokens and the superadmin also sees and revokes everyone's. A viewer
holds no tokens and gets 403 everywhere here. A token that belongs to someone else answers like a
missing one to an admin.
"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.client_address import client_address
from app.core.templating import render
from app.models import ApiToken, User, UserGroup
from app.services.audit import AuditActor
from app.services.permissions import AccessScope, Capability
from app.services.tokens import (
    OPERATIONS,
    TokenError,
    create_token,
    get_manageable_token,
    list_tokens,
    restriction_choices,
    revoke_token,
    usage_example,
)
from app.web.dependencies import AuthContext, get_db, get_scope, require_capability

router = APIRouter()

CREATE_FRAGMENT = "fragments/token_create.html"
CREATED_FRAGMENT = "fragments/token_created.html"
REVOKE_FRAGMENT = "fragments/token_revoke.html"
TABLE_FRAGMENT = "fragments/tokens_table.html"
FORM_FIELDS = ("name", "operations", "restriction", "expires")
DEFAULT_OPERATIONS = frozenset({OPERATIONS[0].key})

ManageTokens = Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_OWN_TOKENS))]
Scope = Annotated[AccessScope, Depends(get_scope)]
Database = Annotated[AsyncSession, Depends(get_db)]


def _events(*names: str) -> str:
    """An ``HX-Trigger`` value firing the named events."""
    return json.dumps(dict.fromkeys(names, True))


async def _list_context(db: AsyncSession, user: User, scope: AccessScope) -> dict[str, Any]:
    """What the tokens page and its table need for the caller."""
    return {
        "rows": await list_tokens(db, user, scope),
        "show_owner": user.group is UserGroup.SUPERADMIN,
    }


async def _form_context(
    db: AsyncSession,
    scope: AccessScope,
    *,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the create form needs; every field has an entry in the errors, empty when fine."""
    return {
        "operations": OPERATIONS,
        "choices": await restriction_choices(db, scope),
        "values": {
            "name": "",
            "operations": DEFAULT_OPERATIONS,
            "restriction": "",
            "expires": "",
            **(values or {}),
        },
        "errors": {name: (errors or {}).get(name) for name in FORM_FIELDS},
        "today": datetime.now(UTC).date().isoformat(),
    }


@router.get("/tokens", include_in_schema=False)
async def tokens_page(
    request: Request, db: Database, context: ManageTokens, scope: Scope
) -> Response:
    """The tokens page: the caller's tokens, or everyone's for the superadmin."""
    return render(request, "pages/tokens.html", await _list_context(db, context.user, scope))


@router.get("/tokens/table", include_in_schema=False)
async def tokens_table(
    request: Request, db: Database, context: ManageTokens, scope: Scope
) -> Response:
    """The tokens table fragment, refreshed after a token is created or revoked."""
    return render(request, TABLE_FRAGMENT, await _list_context(db, context.user, scope))


@router.get("/tokens/new", include_in_schema=False)
async def create_dialog(
    request: Request, db: Database, _context: ManageTokens, scope: Scope
) -> Response:
    """The content of the create token dialog, with the items the caller may restrict to."""
    return render(request, CREATE_FRAGMENT, await _form_context(db, scope))


@router.post("/tokens", include_in_schema=False)
async def create_token_route(
    request: Request,
    db: Database,
    context: ManageTokens,
    scope: Scope,
    name: Annotated[str, Form()] = "",
    operation: Annotated[list[str] | None, Form()] = None,
    restriction: Annotated[str, Form()] = "",
    expires: Annotated[str, Form()] = "",
) -> Response:
    """Create a token for the caller and show its secret once.

    A refused form swaps the dialog again with the reasons next to the fields and the choices
    kept. Success answers the result with the secret only in this response, never in a header,
    URL or log, and tells the tokens table to refresh.
    """
    chosen = set(operation or [])
    try:
        created = await create_token(
            db,
            context.user,
            scope,
            name=name,
            operations=chosen,
            restriction=restriction,
            expires=expires,
            actor=AuditActor.for_user(context.user, client_address(request)),
        )
    except TokenError as error:
        page = await _form_context(
            db,
            scope,
            values={
                "name": name.strip(),
                "operations": chosen,
                "restriction": restriction,
                "expires": expires.strip(),
            },
            errors={error.field or "name": str(error)},
        )
        return render(request, CREATE_FRAGMENT, page, status_code=422)
    response = render(
        request,
        CREATED_FRAGMENT,
        {
            "token_name": created.token.name,
            "secret": created.secret,
            "example": usage_example(
                get_settings().base_url, created.token.operations, created.service_id
            ),
        },
    )
    response.headers["HX-Trigger"] = _events("token-changed")
    return response


async def _manageable(db: AsyncSession, user: User, token_id: int) -> ApiToken:
    """The token the caller may manage, or a 404 the same for a missing and a foreign token."""
    token = await get_manageable_token(db, user, token_id)
    if token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "token not found")
    return token


async def _revoke_view(db: AsyncSession, viewer: User, token: ApiToken) -> dict[str, Any]:
    """The confirm dialog for a token: the buttons, or the reason there is nothing to revoke."""
    owner = await db.get(User, token.owner_id)
    return {
        "token_id": token.id,
        "token_name": token.name,
        "owner_name": owner.name if owner is not None and owner.id != viewer.id else None,
        "refusal": "revoked" if token.revoked_at is not None else None,
    }


@router.get("/tokens/{token_id:int}/revoke", include_in_schema=False)
async def revoke_dialog(
    request: Request, token_id: int, db: Database, context: ManageTokens
) -> Response:
    """The confirm dialog of revoking a token."""
    token = await _manageable(db, context.user, token_id)
    return render(request, REVOKE_FRAGMENT, await _revoke_view(db, context.user, token))


@router.post("/tokens/{token_id:int}/revoke", include_in_schema=False)
async def revoke_token_route(
    request: Request, token_id: int, db: Database, context: ManageTokens
) -> Response:
    """Revoke a token, effective for its next use.

    A token that was already revoked meanwhile answers 409 with the dialog saying so, and the
    tables refresh to show it.
    """
    token = await _manageable(db, context.user, token_id)
    actor = AuditActor.for_user(context.user, client_address(request))
    if not await revoke_token(db, token, actor):
        response = render(
            request,
            REVOKE_FRAGMENT,
            await _revoke_view(db, context.user, token),
            status_code=409,
        )
        response.headers["HX-Trigger"] = _events("token-changed")
        return response
    return Response(
        status_code=204, headers={"HX-Trigger": _events("token-changed", "close-dialogs")}
    )
