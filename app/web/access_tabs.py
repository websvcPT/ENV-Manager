"""Access tab routes (superadmin only) for projects, deployments, services and environments.

An Access tab lists the users with access to one item, direct or inherited, and lets the
superadmin add a user's grant on the item and remove a direct grant. Each item's module mounts
the routes next to its own pages with ``mount_access_routes`` and supplies a dependency that
loads the item through the same access check as its other routes with the grant capability: an
item outside the caller's scope answers 404, any group but the superadmin 403.

Changes go through the audited grants service and apply at once. A change answers 204 with
``HX-Trigger: grants-changed`` (the list refreshes itself) and ``close-dialogs``. A refusal (the
page is out of date, an item above already includes the access, the account is disabled) answers
409 with the reason in the dialog and redraws the list as stored now, so what the page shows is
what is stored.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import render
from app.models import User
from app.services.access_grid import NodeKind
from app.services.access_list import (
    STALE_NOTICE,
    AccessEntry,
    AccessList,
    change_access,
    load_access_list,
)
from app.services.audit import AuditActor
from app.services.grants import MAX_ID_DIGITS
from app.web.dependencies import AuthContext, get_db

LIST_FRAGMENT = "fragments/access_list.html"
ADD_FRAGMENT = "fragments/access_add.html"
REMOVE_FRAGMENT = "fragments/access_remove.html"
CHOOSE_USER = "Choose a user."
CHOOSE_PLACEHOLDER = "Choose a user…"
PLURALS = {
    NodeKind.PROJECT: "projects",
    NodeKind.DEPLOYMENT: "deployments",
    NodeKind.SERVICE: "services",
    NodeKind.ENVIRONMENT: "environments",
}

Database = Annotated[AsyncSession, Depends(get_db)]


@dataclass(frozen=True)
class AccessSubject:
    """The item an Access tab is about, as the caller reaches it.

    ``page`` is the template of the full tab and ``frame`` what that template needs besides
    the list: the header, breadcrumbs and tabs of the item's own page.
    """

    kind: NodeKind
    item_id: int
    project_id: int
    auth: AuthContext
    page: str
    frame: dict[str, Any]

    @property
    def node_id(self) -> str:
        """The row id of the item in the access grid tree, such as ``environment-7``."""
        return f"{self.kind.value}-{self.item_id}"

    @property
    def base(self) -> str:
        """The path of the item's Access tab."""
        return f"/{PLURALS[self.kind]}/{self.item_id}/access"


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _user_id(raw: str) -> int | None:
    """The user id a form value names, or None when it is empty or not a plain number."""
    if raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS:
        return int(raw) or None
    return None


def _changed() -> Response:
    """The answer to a change that was made: refresh every panel that lists grants, close."""
    events = json.dumps({"grants-changed": True, "close-dialogs": True})
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"HX-Trigger": events})


async def _load(db: AsyncSession, subject: AccessSubject) -> AccessList:
    """The Access tab of the item, or 404 when it vanished since the dependency loaded it."""
    access = await load_access_list(db, subject.project_id, subject.node_id)
    if access is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return access


def _add_view(
    subject: AccessSubject,
    access: AccessList,
    *,
    chosen: str = "",
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """What the add dialog needs; a refusal also redraws the list out of band."""
    users = [
        (str(user.id), f"{user.name} ({user.group.value.capitalize()})")
        for user in access.candidates
    ]
    return {
        "access": access,
        "base": subject.base,
        "chosen": chosen,
        "user_options": [("", CHOOSE_PLACEHOLDER), *users],
        "error": error,
        "notice": notice,
        "refresh": notice is not None,
        "oob": True,
    }


def _remove_view(
    subject: AccessSubject,
    access: AccessList,
    user_id: int,
    user_name: str,
    *,
    entry: AccessEntry | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """What the remove dialog needs; a refusal also redraws the list out of band."""
    return {
        "access": access,
        "base": subject.base,
        "user_id": user_id,
        "user_name": user_name,
        "entry": entry,
        "notice": notice,
        "refresh": notice is not None,
        "oob": True,
    }


def mount_access_routes(
    router: APIRouter,
    kind: NodeKind,
    subject_dependency: Callable[..., Awaitable[AccessSubject]],
) -> None:
    """Add the Access tab routes of one item type to a router.

    Every item type gets the tab and its refreshable list. Types that are a grant level (all but
    the service) also get the add dialog, the add post, the remove dialog and the remove post.
    ``subject_dependency`` loads the item and checks the grant capability, and its path parameter
    is ``<kind>_id``.
    """
    base = f"/{PLURALS[kind]}/{{{kind.value}_id:int}}/access"
    Subject = Annotated[AccessSubject, Depends(subject_dependency)]

    @router.get(base, include_in_schema=False, name=f"{kind.value}_access_tab")
    async def access_tab(request: Request, db: Database, subject: Subject) -> Response:
        """The full Access tab inside the frame of the item's own page."""
        access = await _load(db, subject)
        context = {**subject.frame, "access": access, "base": subject.base}
        return render(request, subject.page, context)

    @router.get(f"{base}/list", include_in_schema=False, name=f"{kind.value}_access_list")
    async def access_list(request: Request, db: Database, subject: Subject) -> Response:
        """The list panel, refreshed when a grant changes."""
        access = await _load(db, subject)
        return render(request, LIST_FRAGMENT, {"access": access, "base": subject.base})

    if kind is NodeKind.SERVICE:
        return

    @router.get(f"{base}/add", include_in_schema=False, name=f"{kind.value}_access_add_dialog")
    async def add_dialog(request: Request, db: Database, subject: Subject) -> Response:
        """The add dialog, with the users a grant on the item would give something new."""
        return render(request, ADD_FRAGMENT, _add_view(subject, await _load(db, subject)))

    @router.post(f"{base}/grants", include_in_schema=False, name=f"{kind.value}_access_add")
    async def add_access(
        request: Request,
        db: Database,
        subject: Subject,
        user: Annotated[str, Form()] = "",
    ) -> Response:
        """Add the user's grant on the item; a refusal swaps the dialog with its reason."""
        user_id = _user_id(user)
        if user_id is None:
            view = _add_view(subject, await _load(db, subject), error=CHOOSE_USER)
            return render(request, ADD_FRAGMENT, view, status_code=422)
        actor = AuditActor.for_user(subject.auth.user, _client_ip(request))
        change = await change_access(
            db, subject.project_id, subject.node_id, user_id, grant=True, actor=actor
        )
        if change.notice is None:
            return _changed()
        view = _add_view(subject, await _load(db, subject), chosen=user, notice=change.notice)
        return render(request, ADD_FRAGMENT, view, status_code=status.HTTP_409_CONFLICT)

    @router.get(
        f"{base}/remove/{{user_id:int}}",
        include_in_schema=False,
        name=f"{kind.value}_access_remove_dialog",
    )
    async def remove_dialog(
        request: Request, db: Database, subject: Subject, user_id: int
    ) -> Response:
        """The confirm dialog of removing a direct grant, or why there is none to remove."""
        account = await db.get(User, user_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        access = await _load(db, subject)
        entry = access.entry_of(user_id)
        if entry is not None and entry.direct:
            view = _remove_view(subject, access, user_id, account.name, entry=entry)
        else:
            view = _remove_view(subject, access, user_id, account.name, notice=STALE_NOTICE)
        return render(request, REMOVE_FRAGMENT, view)

    @router.post(
        f"{base}/remove/{{user_id:int}}",
        include_in_schema=False,
        name=f"{kind.value}_access_remove",
    )
    async def remove_access(
        request: Request, db: Database, subject: Subject, user_id: int
    ) -> Response:
        """Remove the user's direct grant on the item; a refusal swaps the dialog."""
        actor = AuditActor.for_user(subject.auth.user, _client_ip(request))
        change = await change_access(
            db, subject.project_id, subject.node_id, user_id, grant=False, actor=actor
        )
        if change.notice is None:
            return _changed()
        view = _remove_view(
            subject,
            await _load(db, subject),
            user_id,
            change.user_name or "this user",
            notice=change.notice,
        )
        return render(request, REMOVE_FRAGMENT, view, status_code=status.HTTP_409_CONFLICT)
