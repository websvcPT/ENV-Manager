"""The Docker Compose import screens of a deployment that has no services yet. Superadmin only.

Step one lists the Compose files found and takes a typed path, step two shows what the real
Compose reads from them (services and files, each to tick), and step three is a confirmation
dialog whose import sends the user back to the deployment page. The routes are POSTs behind CSRF
because every step runs the Compose CLI; each one reads the files again and carries the choices
in the form, so no scan is kept between requests and no posted path is used without being checked
(the services check every path, key, owner and type against their own scan).

The services check no roles: every route here declares the manage structure capability.
"""

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.templating import render
from app.models import Deployment
from app.services.compose_config import ComposeScanError
from app.services.compose_import import (
    HAS_SERVICES_MESSAGE,
    ComposeImportError,
    import_compose,
)
from app.services.compose_scan import (
    MAX_COMPOSE_FILES,
    ComposeFileOffer,
    ComposeScan,
    find_compose_files,
    scan_compose,
)
from app.services.docker_cli import DockerCli
from app.services.notify_events import FullNotifier
from app.services.permissions import Capability
from app.web.compose_import_form import (
    parse_posted,
    preview_rows,
    review_import,
    selection_of,
    usable_services,
)
from app.web.dependencies import get_crypto, get_db, get_docker_cli, get_notifier
from app.web.deployments import DeploymentContext, deployment_access
from app.web.htmx import hx_redirect

router = APIRouter()

BASE = "/deployments/{deployment_id:int}/import-compose"
PAGE = "pages/compose_import.html"
FILES_FRAGMENT = "fragments/compose_import_files.html"
PREVIEW_FRAGMENT = "fragments/compose_import_preview.html"
CONFIRM_FRAGMENT = "fragments/compose_import_confirm.html"
MAX_TYPED_PATH = 500
NOTHING_TO_IMPORT = "Nothing to import"


@dataclass(frozen=True)
class ImportTarget:
    """The deployment of an import request as the page shows it and as it is stored, and whether
    it already has services (an import is refused then)."""

    access: DeploymentContext
    deployment: Deployment
    blocked: bool


async def import_target(
    access: Annotated[DeploymentContext, Depends(deployment_access(Capability.MANAGE_STRUCTURE))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImportTarget:
    """The deployment for the superadmin, with whether it already has services."""
    deployment = await db.get(Deployment, access.deployment.id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return ImportTarget(access, deployment, bool(access.deployment.services))


def _address(target: ImportTarget, tail: str = "") -> str:
    """The address of one of the import routes of the deployment."""
    return f"/deployments/{target.deployment.id}/import-compose{tail}"


def _default_chosen(offers: tuple[ComposeFileOffer, ...]) -> list[str]:
    """The Compose files ticked at first: the first base file with the overrides of its folder."""
    base = next((offer for offer in offers if not offer.override), None)
    if base is None:
        return []
    return [offer.path for offer in offers if offer.folder == base.folder]


async def _step_one(
    request: Request,
    target: ImportTarget,
    db: AsyncSession,
    *,
    chosen: list[str] | None = None,
    typed: str = "",
    error: str | None = None,
    status_code: int = 200,
    fragment: bool = False,
) -> Response:
    """Step one, as the page or as the fragment that replaces the later steps.

    ``chosen`` are the Compose files to tick again; those that are not among the files found
    now are not ticked, and the first of them goes into the path field unless one was typed.
    """
    offers: tuple[ComposeFileOffer, ...] = ()
    message = HAS_SERVICES_MESSAGE if target.blocked else error
    if not target.blocked:
        try:
            offers = await find_compose_files(db, target.deployment)
        except ComposeScanError as refused:
            message = str(refused)
    paths = {offer.path for offer in offers}
    if chosen is None:
        chosen = _default_chosen(offers)
    else:
        outside = [path for path in chosen if path not in paths]
        typed = typed or (outside[0] if outside else "")
        chosen = [path for path in chosen if path in paths]
    context = {
        "deployment": target.access.deployment,
        "offers": offers,
        "chosen": set(chosen),
        "typed": typed,
        "error": message,
        "blocked": target.blocked,
        "action": _address(target, "/preview"),
    }
    code = status.HTTP_409_CONFLICT if target.blocked else status_code
    return render(request, FILES_FRAGMENT if fragment else PAGE, context, status_code=code)


@router.get(BASE, include_in_schema=False)
async def import_page(
    request: Request,
    target: Annotated[ImportTarget, Depends(import_target)],
    db: Annotated[AsyncSession, Depends(get_db)],
    compose_file: Annotated[list[str] | None, Query()] = None,
) -> Response:
    """Step one. A ``compose_file`` list (the Back link of the preview) ticks those files again."""
    chosen = None if compose_file is None else compose_file[:MAX_COMPOSE_FILES]
    return await _step_one(request, target, db, chosen=chosen)


@router.post(f"{BASE}/preview", include_in_schema=False)
async def import_preview(
    request: Request,
    target: Annotated[ImportTarget, Depends(import_target)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    compose_file: Annotated[list[str] | None, Form()] = None,
    extra_path: Annotated[str, Form()] = "",
) -> Response:
    """Step two: scan the chosen and the typed Compose files and show what they hold.

    The ticked files are matched against the files found now, in the order of the folder, and the
    typed path (checked by the scan) comes last. A refusal draws step one again with the reason
    (422); the preview is a fragment that replaces step one.
    """
    posted = [path.strip() for path in compose_file or []][:MAX_COMPOSE_FILES]
    typed = extra_path.strip()[:MAX_TYPED_PATH]
    if target.blocked:
        return await _step_one(request, target, db, fragment=True)
    ordered = list(posted)
    error: str | None = None
    try:
        offers = await find_compose_files(db, target.deployment)
        ordered = [offer.path for offer in offers if offer.path in posted]
        if typed and typed not in ordered:
            ordered.append(typed)
        scan = await scan_compose(db, cli, target.deployment, ordered)
    except ComposeScanError as refused:
        error = str(refused)
    if error is not None:
        return await _step_one(
            request,
            target,
            db,
            chosen=ordered,
            typed=typed,
            error=error,
            status_code=422,
            fragment=True,
        )
    return render(request, PREVIEW_FRAGMENT, _preview_context(target, scan))


def _preview_context(target: ImportTarget, scan: ComposeScan) -> dict[str, object]:
    """What the preview fragment shows: the scan, a row per file with its first choices, the
    counts, and the addresses that lead back to step one and on to the confirmation."""
    rows = preview_rows(scan)
    back = urlencode([("compose_file", path) for path in scan.compose_files])
    return {
        "deployment": target.access.deployment,
        "scan": scan,
        "rows": rows,
        "usable_services": len(usable_services(scan)),
        "importable_files": sum(1 for row in rows if row.candidate.disabled_reason is None),
        "back_href": f"{_address(target)}?{back}",
        "confirm_action": _address(target, "/confirm"),
    }


def _problem(
    request: Request,
    title: str,
    message: str,
    items: tuple[tuple[str, str], ...] = (),
    status_code: int = 422,
) -> Response:
    """The confirmation dialog when the import cannot go on: the reason and the item reasons."""
    return render(
        request,
        CONFIRM_FRAGMENT,
        {"problem_title": title, "problem": message, "items": items},
        status_code=status_code,
    )


@router.post(f"{BASE}/confirm", include_in_schema=False)
async def import_confirm(
    request: Request,
    target: Annotated[ImportTarget, Depends(import_target)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
) -> Response:
    """Step three: scan again, judge the posted choices and show what the import would create.

    Nothing is changed. The dialog carries the judged choices to the import as hidden fields.
    """
    if target.blocked:
        return _problem(request, NOTHING_TO_IMPORT, HAS_SERVICES_MESSAGE, status_code=409)
    posted = parse_posted(await request.form())
    try:
        scan = await scan_compose(db, cli, target.deployment, posted.compose_files)
    except ComposeScanError as refused:
        return _problem(request, "The Compose files cannot be read", str(refused))
    review = review_import(scan, posted)
    if review.problems:
        return _problem(
            request,
            "Review the selection",
            "Some of the choices cannot be imported. Close this and change them.",
            review.problems,
        )
    with_content = sum(1 for candidate, _ in review.picks if candidate.exists)
    from_dist = sum(1 for _, choice in review.picks if choice.from_dist)
    chosen = set(review.selection.services)
    return render(
        request,
        CONFIRM_FRAGMENT,
        {
            "action": _address(target),
            "selection": review.selection,
            "picks": review.picks,
            "dropped": review.dropped,
            "with_content": with_content,
            "from_dist": from_dist,
            "empty": len(review.picks) - with_content - from_dist,
            "linked": sum(1 for _, choice in review.picks if choice.dist_path),
            "left_services": sum(1 for service in scan.services if service.name not in chosen),
        },
    )


@router.post(BASE, include_in_schema=False)
async def import_services(
    request: Request,
    target: Annotated[ImportTarget, Depends(import_target)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cli: Annotated[DockerCli, Depends(get_docker_cli)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    notifier: Annotated[FullNotifier, Depends(get_notifier)],
) -> Response:
    """Import the confirmed services and files. Success leaves for the deployment page.

    The import scans again and refuses what no longer fits, changing nothing; the dialog then
    shows the reason and the files it names (422).
    """
    if target.blocked:
        return _problem(request, NOTHING_TO_IMPORT, HAS_SERVICES_MESSAGE, status_code=409)
    selection = selection_of(parse_posted(await request.form()))
    try:
        result = await import_compose(
            db,
            crypto,
            cli,
            target.deployment,
            selection,
            actor=target.access.auth.user,
            ip_address=client_address(request),
            notifier=notifier,
        )
    except ComposeImportError as refused:
        items = tuple(refused.errors.items())
        return _problem(request, "Nothing was imported", str(refused), items)
    counts = ",".join(
        str(number)
        for number in (
            result.services,
            result.environments,
            result.with_content,
            result.empty,
            result.skipped,
        )
    )
    return hx_redirect(f"/deployments/{target.deployment.id}?imported={counts}")
