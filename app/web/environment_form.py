"""Add environment routes: the form page, its submit and the live target path check.

Superadmin only. The submit reads the multipart form itself, after authentication, so an
unauthenticated or oversized request is refused before any body is parsed.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile

from app.core.client_address import client_address
from app.core.templating import render
from app.models import Deployment, Service
from app.services.content import MAX_CONTENT_BYTES
from app.services.environments import (
    EnvironmentForm,
    EnvironmentFormError,
    TargetHint,
    create_environment,
    target_hint,
    validate_environment,
)
from app.services.file_types import CERT_NAME_SUFFIXES
from app.services.paths import load_allowed_paths
from app.services.permissions import Capability
from app.web.dependencies import get_crypto, get_db, get_notifier
from app.web.htmx import hx_redirect
from app.web.service_detail import ServiceContext, service_access

router = APIRouter()

PAGE = "pages/environment_form.html"
FORM_FRAGMENT = "fragments/environment_form.html"
HINT_FRAGMENT = "fragments/target_hint.html"
# A paste and an upload can both be present, and a browser sends every pasted line break as two
# bytes; anything larger is refused before the body is parsed.
MAX_REQUEST_BYTES = 1024 * 1024
FORM_TOO_LARGE = f"The form is larger than {MAX_REQUEST_BYTES // 1024} KiB."


def _text(form: FormData, key: str) -> str:
    """A text field of a parsed form, empty when absent or sent as a file."""
    value = form.get(key)
    return value if isinstance(value, str) else ""


async def _upload_bytes(form: FormData, key: str) -> bytes | None:
    """The bytes of an uploaded file, one more than the bound at most, None when none was chosen."""
    value = form.get(key)
    if not isinstance(value, UploadFile):
        return None
    data = await value.read(MAX_CONTENT_BYTES + 1)
    return data or None


async def _deployment_of(db: AsyncSession, access: ServiceContext) -> Deployment:
    """The stored deployment of the service, or 404."""
    deployment = await db.get(Deployment, access.service.deployment_id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return deployment


async def _form_context(
    db: AsyncSession,
    access: ServiceContext,
    deployment: Deployment,
    form: EnvironmentForm,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the form needs: values, errors, the deployment's services and file defaults."""
    rows = await db.execute(
        select(Service.id, Service.name)
        .where(Service.deployment_id == deployment.id)
        .order_by(Service.name)
    )
    allowed = await load_allowed_paths(db)
    return {
        "service": access.service,
        "form": form,
        "errors": errors or {},
        "services": [(str(service_id), name) for service_id, name in rows.all()],
        "deployment": deployment,
        "hint": target_hint(allowed, deployment.root_path, form.target_path),
        "cert_suffixes": " ".join(CERT_NAME_SUFFIXES),
        "action": f"/services/{access.service.id}/environments/new",
        "cancel_href": f"/services/{access.service.id}",
    }


@router.get("/services/{service_id:int}/environments/new", include_in_schema=False)
async def new_environment_page(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The empty add environment form with the current service selected."""
    deployment = await _deployment_of(db, access)
    form = EnvironmentForm(service=str(access.service.id))
    return render(request, PAGE, await _form_context(db, access, deployment, form))


@router.get("/services/{service_id:int}/environments/target-check", include_in_schema=False)
async def check_target(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
    target_path: str = "",
) -> Response:
    """The line under the target path field: where the path resolves, or why it cannot."""
    deployment = await _deployment_of(db, access)
    hint: TargetHint = target_hint(await load_allowed_paths(db), deployment.root_path, target_path)
    return render(request, HINT_FRAGMENT, {"hint": hint})


@router.post("/services/{service_id:int}/environments/new", include_in_schema=False)
async def create_environment_route(
    request: Request,
    access: Annotated[ServiceContext, Depends(service_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Create an environment with version 1. A refused value swaps the form again (422)."""
    deployment = await _deployment_of(db, access)
    length = request.headers.get("content-length", "")
    if not length.isdecimal():
        raise HTTPException(status.HTTP_411_LENGTH_REQUIRED, "content length required")
    if int(length) > MAX_REQUEST_BYTES:
        return render(
            request,
            FORM_FRAGMENT,
            await _form_context(
                db,
                access,
                deployment,
                EnvironmentForm(service=str(access.service.id)),
                {"content": FORM_TOO_LARGE},
            ),
            status_code=422,
        )
    parsed = await request.form(max_files=1, max_fields=30, max_part_size=MAX_REQUEST_BYTES)
    form = EnvironmentForm(
        service=_text(parsed, "service"),
        new_service=_text(parsed, "new_service"),
        name=_text(parsed, "name"),
        file_type=_text(parsed, "file_type"),
        target_path=_text(parsed, "target_path"),
        dist_path=_text(parsed, "dist_path"),
        file_mode=_text(parsed, "file_mode"),
        file_owner=_text(parsed, "file_owner"),
        file_group=_text(parsed, "file_group"),
        source=_text(parsed, "source") or "paste",
        content=_text(parsed, "content"),
        upload=await _upload_bytes(parsed, "upload"),
    )
    try:
        clean = await validate_environment(db, await load_allowed_paths(db), deployment, form)
        environment = await create_environment(
            db,
            get_crypto(request),
            deployment,
            clean,
            actor=access.auth.user,
            ip_address=client_address(request),
            notifier=get_notifier(request),
        )
    except EnvironmentFormError as error:
        return render(
            request,
            FORM_FRAGMENT,
            await _form_context(db, access, deployment, form, error.errors),
            status_code=422,
        )
    return hx_redirect(f"/services/{environment.service_id}?added=environment")
