"""The Git tab: the repository settings, saved with their credentials, and the connection test.

Superadmin only: every route holds the settings capability, so any other group gets 403. The deploy
key and the token are never put back into the page. A save answers the form with its toast and
sends the banner area again, because saving clears the push failing banner. The connection test
reads from the saved repository and sends nothing; for an ssh address it shows the host key
fingerprint, and a separate post pins exactly that key. Both answer 200 whatever the result, so
htmx swaps them.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.display_time import display_of
from app.core.templating import render
from app.services.app_settings import (
    GIT_DEPLOY_KEY_KEY,
    GIT_TOKEN_KEY,
    GitSettings,
    StoredSettings,
    has_secret,
    has_secret_in,
)
from app.services.git_connection import accept_host_key, check_connection
from app.services.git_remote import without_password
from app.services.git_services import GitServices
from app.services.git_settings import (
    GitForm,
    GitFormError,
    format_options,
    save_git,
    validate_git,
)
from app.services.git_view import load_git_status
from app.services.shell_banners import load_shell_banners
from app.web.dependencies import audit_actor, get_crypto, get_db, get_stored_settings
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_git.html"
SAVED_FRAGMENT = "fragments/git_form_saved.html"
TEST_FRAGMENT = "fragments/git_test_result.html"

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]


def _services(request: Request) -> GitServices:
    """The application's git services."""
    services: GitServices = request.app.state.git_services
    return services


async def _form_context(
    request: Request,
    db: AsyncSession,
    git: GitSettings,
    form: GitForm,
    credentials: tuple[bool, bool],
    *,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the form needs: values, refusals, the choices, the toast, the credentials stored and
    the state of the sync.

    ``credentials`` says whether a deploy key and whether a token are stored, as the caller read
    it: from the request's settings when nothing was written, from the database after a save.
    """
    deploy_key_stored, token_stored = credentials
    return {
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "format_options": format_options(),
        "deploy_key_stored": deploy_key_stored,
        "token_stored": token_stored,
        "status": await load_git_status(db, git, display_of(request)),
    }


def _credentials_in(stored: StoredSettings) -> tuple[bool, bool]:
    """Whether a deploy key and a token were stored when the request's settings were read."""
    return has_secret_in(stored, GIT_DEPLOY_KEY_KEY), has_secret_in(stored, GIT_TOKEN_KEY)


@router.get("/settings/git", include_in_schema=False)
async def git_page(
    request: Request, db: Database, _context: ManageSettings, stored: Stored
) -> Response:
    """The Git tab."""
    form = GitForm.from_settings(stored.git)
    context = await _form_context(request, db, stored.git, form, _credentials_in(stored))
    return render(request, "pages/settings_git.html", {**tab_context("git"), **context})


@router.post("/settings/git", include_in_schema=False)
async def save_git_route(
    request: Request,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    stored: Stored,
    url: Annotated[str, Form()] = "",
    branch: Annotated[str, Form()] = "",
    author_name: Annotated[str, Form()] = "",
    export_format: Annotated[str, Form()] = "",
    push_enabled: Annotated[str, Form()] = "",
    deploy_key: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
    remove_deploy_key: Annotated[str, Form()] = "",
    remove_token: Annotated[str, Form()] = "",
) -> Response:
    """Save the git settings. A refused value answers the form again with the reasons (422)."""
    form = GitForm(
        url,
        branch,
        author_name,
        export_format,
        push_enabled == "on",
        deploy_key,
        token,
        remove_deploy_key == "on",
        remove_token == "on",  # noqa: S105
    )
    try:
        clean = validate_git(form)
    except GitFormError as error:
        shown = GitForm(
            without_password(url),
            branch,
            author_name,
            export_format,
            push_enabled == "on",
            remove_deploy_key=remove_deploy_key == "on",
            remove_token=remove_token == "on",  # noqa: S105
        )
        page = await _form_context(
            request, db, stored.git, shown, _credentials_in(stored), errors=error.errors
        )
        return render(request, FORM_FRAGMENT, page, status_code=422)
    changed = await save_git(
        db,
        crypto,
        clean,
        actor=audit_actor(request, context),
        locations=_services(request).locations,
    )
    if changed:
        saved = {
            "tone": "success",
            "title": "Git settings saved",
            "message": "The next scheduled push uses them.",
        }
    else:
        saved = {
            "tone": "info",
            "title": "Nothing to save",
            "message": "The settings are unchanged.",
        }
    request.state.banners = await load_shell_banners(db, display_of(request))
    credentials = (
        await has_secret(db, GIT_DEPLOY_KEY_KEY),
        await has_secret(db, GIT_TOKEN_KEY),
    )
    page = await _form_context(
        request,
        db,
        clean.settings,
        GitForm.from_settings(clean.settings),
        credentials,
        saved=saved,
    )
    return render(request, SAVED_FRAGMENT, page)


@router.post("/settings/git/test", include_in_schema=False)
async def test_route(
    request: Request, db: Database, context: ManageSettings, stored: Stored
) -> Response:
    """Test the saved repository without sending anything and answer how it went."""
    check = await check_connection(db, _services(request), actor=audit_actor(request, context))
    status = await load_git_status(db, stored.git, display_of(request))
    return render(request, TEST_FRAGMENT, {"check": check, "status": status})


@router.post("/settings/git/pin", include_in_schema=False)
async def pin_route(
    request: Request,
    db: Database,
    context: ManageSettings,
    stored: Stored,
    key_type: Annotated[str, Form()] = "",
    fingerprint: Annotated[str, Form()] = "",
) -> Response:
    """Pin the host key the superadmin was shown, then test the connection again."""
    check = await accept_host_key(
        db,
        _services(request),
        actor=audit_actor(request, context),
        key_type=key_type.strip(),
        fingerprint=fingerprint.strip(),
    )
    status = await load_git_status(db, stored.git, display_of(request))
    return render(request, TEST_FRAGMENT, {"check": check, "status": status})
