"""User administration routes (superadmin): list, create, group, enable or disable, grants."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.models import AuditActorType, User, UserGroup
from app.services import users as user_service
from app.services.audit import AuditActor
from app.services.grants import (
    GrantError,
    GrantTarget,
    StaleGrantsError,
    load_grant_tree,
    parse_targets,
    set_user_grants,
)
from app.services.permissions import Capability
from app.services.user_detail import list_user_rows, load_user_detail
from app.web.dependencies import AuthContext, get_db, require_capability

router = APIRouter()

CREATE_FRAGMENT = "fragments/user_create.html"
CREATED_FRAGMENT = "fragments/user_created.html"
CONFIRM_FRAGMENT = "fragments/user_confirm.html"
GRANTS_FRAGMENT = "fragments/user_grants.html"
TABLE_FRAGMENT = "fragments/users_table.html"
TOKENS_FRAGMENT = "fragments/user_tokens.html"
ACTIONS = ("disable", "enable", "group")
ORIGINS = ("list", "detail")
FORM_FIELDS = ("name", "email", "group", "grants")
GROUP_LABELS = {group.value: group.value.capitalize() for group in UserGroup}

ManageUsers = Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_USERS))]
ManageGrants = Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_GRANTS))]
Database = Annotated[AsyncSession, Depends(get_db)]


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _group_of(value: str) -> UserGroup | None:
    """The group a form value names, or None."""
    try:
        return UserGroup(value)
    except ValueError:
        return None


def _grant_keys(grants: frozenset[GrantTarget] | set[GrantTarget]) -> set[str]:
    """The form keys of a set of grant targets."""
    return {target.key for target in grants}


async def _user_row(db: AsyncSession, user_id: int) -> User:
    """The user, or a 404 for an id that does not exist."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


def _events(*names: str) -> str:
    """An ``HX-Trigger`` value firing the named events."""
    return json.dumps(dict.fromkeys(names, True))


def _form_context(
    tree: Any,
    *,
    values: dict[str, str] | None = None,
    granted: set[str] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the create form needs; every field has an entry in the errors, empty when fine."""
    return {
        "tree": tree,
        "granted": granted or set(),
        "values": {"name": "", "email": "", "group": UserGroup.VIEWER.value, **(values or {})},
        "errors": {field: (errors or {}).get(field) for field in FORM_FIELDS},
    }


@router.get("/users", include_in_schema=False)
async def users_page(request: Request, db: Database, _context: ManageUsers) -> Response:
    """The users page: every account with its group, status and reach."""
    return render(request, "pages/users.html", {"rows": await list_user_rows(db)})


@router.get("/users/table", include_in_schema=False)
async def users_table(request: Request, db: Database, _context: ManageUsers) -> Response:
    """The users table fragment, refreshed after any change to an account."""
    return render(request, TABLE_FRAGMENT, {"rows": await list_user_rows(db)})


@router.get("/users/new", include_in_schema=False)
async def create_dialog(request: Request, db: Database, _context: ManageUsers) -> Response:
    """The content of the create user dialog, with the current grant tree."""
    return render(request, CREATE_FRAGMENT, _form_context(await load_grant_tree(db)))


@router.post("/users", include_in_schema=False)
async def create_user_route(
    request: Request,
    db: Database,
    context: ManageUsers,
    name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    group: Annotated[str, Form()] = UserGroup.VIEWER.value,
    grant: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Create a user with their initial grants and show the generated password once.

    A refused form swaps the dialog again with the reasons next to the fields and the choices
    kept. Success answers the result with the password only in this response, never in a header,
    URL or log, and tells the users table to refresh.
    """
    chosen_keys = set(grant or [])
    errors: dict[str, str] = {}
    chosen_group = _group_of(group)
    targets: set[GrantTarget] = set()
    if chosen_group is None:
        errors["group"] = "Choose a group."
    try:
        targets = parse_targets(chosen_keys)
    except GrantError as error:
        errors["grants"] = str(error)
    if chosen_group is not None and not errors:
        try:
            created, password = await user_service.create_user(
                db,
                name=name,
                email=email,
                group=chosen_group,
                actor_type=AuditActorType.USER,
                actor_user=context.user,
                ip_address=_client_ip(request),
                grants=targets,
            )
        except (user_service.UserError, GrantError) as error:
            errors[error.field or "name"] = str(error)
        else:
            response = render(
                request,
                CREATED_FRAGMENT,
                {
                    "created_name": created.name,
                    "group_label": GROUP_LABELS[created.group.value],
                    "password": password,
                },
            )
            response.headers["HX-Trigger"] = _events("user-changed")
            return response
    return render(
        request,
        CREATE_FRAGMENT,
        _form_context(
            await load_grant_tree(db),
            values={"name": name.strip(), "email": email.strip(), "group": group},
            granted=chosen_keys,
            errors=errors,
        ),
        status_code=422,
    )


async def _detail_context(
    db: AsyncSession, user_id: int, message: str | None = None, tone: str = "info"
) -> dict[str, Any]:
    """What the grants panel needs for a user as stored now, 404 when there is no such user.

    ``base`` carries the stored grants into the form so a save can tell whether they changed.
    """
    detail = await load_user_detail(db, user_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    keys = _grant_keys(detail.grants)
    return {
        "user": detail,
        "tree": await load_grant_tree(db),
        "granted": keys,
        "base": ",".join(sorted(keys)),
        "message": message,
        "message_tone": tone,
    }


@router.get("/users/{user_id:int}", include_in_schema=False)
async def user_page(
    request: Request,
    user_id: int,
    db: Database,
    _context: ManageUsers,
    updated: str = "",
) -> Response:
    """The user detail: group, status, editable grants, tokens and recent activity."""
    context = await _detail_context(db, user_id)
    return render(request, "pages/user_detail.html", {**context, "updated": updated})


@router.get("/users/{user_id:int}/tokens", include_in_schema=False)
async def user_tokens(
    request: Request, user_id: int, db: Database, _context: ManageUsers
) -> Response:
    """The API tokens panel of a user, refreshed after one of their tokens is revoked."""
    detail = await load_user_detail(db, user_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return render(request, TOKENS_FRAGMENT, {"user": detail})


@router.post("/users/{user_id:int}/grants", include_in_schema=False)
async def save_grants_route(
    request: Request,
    user_id: int,
    db: Database,
    context: ManageGrants,
    grant: Annotated[list[str] | None, Form()] = None,
    base: Annotated[str, Form()] = "",
) -> Response:
    """Make the user's grants exactly the ticked items and answer the refreshed panel.

    A page that no longer matches the stored grants (changed meanwhile) is refused with the
    current state; an item that was deleted meanwhile is refused too. Success is effective at
    once and tells other panels with ``grants-changed``.
    """
    user = await _user_row(db, user_id)
    actor = AuditActor.for_user(context.user, _client_ip(request))
    try:
        desired = parse_targets(grant or [])
        started_from = parse_targets([key for key in base.split(",") if key])
        change = await set_user_grants(db, user, desired, actor=actor, base=started_from)
    except GrantError as error:
        stale = " The list shows the current grants." if isinstance(error, StaleGrantsError) else ""
        page = await _detail_context(db, user_id, f"{error}{stale}", "error")
        return render(request, GRANTS_FRAGMENT, page, status_code=409)
    saved = "Grants saved. They apply now." if change.changed else "There was nothing to change."
    page = await _detail_context(db, user_id, saved, "success" if change.changed else "info")
    response = render(request, GRANTS_FRAGMENT, page)
    if change.changed:
        response.headers["HX-Trigger"] = _events("grants-changed")
    return response


def _confirm_context(
    user: User, action: str, origin: str, *, target: UserGroup | None, refusal: str | None
) -> dict[str, Any]:
    """What the confirm dialog needs for one action on one user."""
    unchanged = {
        "disable": f"{user.name} is already disabled.",
        "enable": f"{user.name} is already active.",
        "group": f"{user.name} is already in the {GROUP_LABELS[user.group.value]} group.",
    }
    return {
        "action": action,
        "user_id": user.id,
        "user_name": user.name,
        "origin": origin,
        "current_group": GROUP_LABELS[user.group.value],
        "target_group": GROUP_LABELS[target.value] if target else "",
        "target_value": target.value if target else "",
        "refusal": refusal,
        "unchanged_message": unchanged[action],
        "is_self": False,
    }


async def _confirm_view(
    db: AsyncSession, actor: User, user: User, action: str, origin: str, group: str
) -> dict[str, Any]:
    """The confirm dialog for an action: the buttons, or the reason the action is refused."""
    target = _group_of(group) if action == "group" else None
    if action == "group" and target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
    refusal: str | None = None
    already = {
        "disable": not user.is_active,
        "enable": user.is_active,
        "group": target is user.group,
    }
    if already[action]:
        refusal = "unchanged"
    elif (
        action == "disable" or (action == "group" and target is not UserGroup.SUPERADMIN)
    ) and await user_service.is_last_active_superadmin(db, user):
        refusal = "last_active_superadmin"
    view = _confirm_context(user, action, origin, target=target, refusal=refusal)
    view["is_self"] = user.id == actor.id
    return view


@router.get("/users/{user_id:int}/confirm/{action}", include_in_schema=False)
async def confirm_dialog(
    request: Request,
    user_id: int,
    action: str,
    db: Database,
    context: ManageUsers,
    origin: Annotated[str, Query(alias="from")] = "list",
    group: str = "",
) -> Response:
    """The confirm dialog of disabling, enabling or changing the group of a user."""
    if action not in ACTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
    user = await _user_row(db, user_id)
    view = await _confirm_view(
        db, context.user, user, action, origin if origin in ORIGINS else "list", group
    )
    return render(request, CONFIRM_FRAGMENT, view)


def _done(user: User, actor: User, origin: str, updated: str) -> Response:
    """The answer to a confirmed change: refresh the table, or go back to the detail page.

    A change to the caller's own account sends them to the projects page, which signs them out
    when their session ended.
    """
    if user.id == actor.id:
        return Response(status_code=200, headers={"HX-Redirect": "/projects"})
    if origin == "detail":
        return Response(
            status_code=200, headers={"HX-Redirect": f"/users/{user.id}?updated={updated}"}
        )
    return Response(
        status_code=204, headers={"HX-Trigger": _events("user-changed", "close-dialogs")}
    )


async def _apply_action(
    request: Request,
    db: AsyncSession,
    actor: User,
    user_id: int,
    action: str,
    origin: str,
    group: str,
) -> Response:
    """Run a confirmed disable, enable or group change and answer for the page it came from.

    The last active superadmin cannot be disabled or demoted: the service audits the refusal
    and the dialog shows it. A change that would leave the account as it is does nothing.
    """
    user = await _user_row(db, user_id)
    origin = origin if origin in ORIGINS else "list"
    ip_address = _client_ip(request)
    try:
        if action == "group":
            target = _group_of(group)
            if target is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
            await user_service.set_user_group(
                db,
                user,
                target,
                actor_type=AuditActorType.USER,
                actor_user=actor,
                ip_address=ip_address,
            )
            updated = "group"
        else:
            active = action == "enable"
            if user.is_active is not active:
                await user_service.set_user_active(
                    db,
                    user,
                    active,
                    actor_type=AuditActorType.USER,
                    actor_user=actor,
                    ip_address=ip_address,
                )
            updated = "enabled" if active else "disabled"
    except user_service.LastSuperadminError:
        view = await _confirm_view(db, actor, user, action, origin, group)
        return render(request, CONFIRM_FRAGMENT, view, status_code=409)
    return _done(user, actor, origin, updated)


@router.post("/users/{user_id:int}/disable", include_in_schema=False)
async def disable_user_route(
    request: Request,
    user_id: int,
    db: Database,
    context: ManageUsers,
    origin: Annotated[str, Form(alias="from")] = "list",
) -> Response:
    """Disable a user: sessions end now and their API tokens are revoked."""
    return await _apply_action(request, db, context.user, user_id, "disable", origin, "")


@router.post("/users/{user_id:int}/enable", include_in_schema=False)
async def enable_user_route(
    request: Request,
    user_id: int,
    db: Database,
    context: ManageUsers,
    origin: Annotated[str, Form(alias="from")] = "list",
) -> Response:
    """Enable a disabled user."""
    return await _apply_action(request, db, context.user, user_id, "enable", origin, "")


@router.post("/users/{user_id:int}/group", include_in_schema=False)
async def change_group_route(
    request: Request,
    user_id: int,
    db: Database,
    context: ManageUsers,
    origin: Annotated[str, Form(alias="from")] = "list",
    group: Annotated[str, Form()] = "",
) -> Response:
    """Move a user to another group, effective from their next request."""
    return await _apply_action(request, db, context.user, user_id, "group", origin, group)
