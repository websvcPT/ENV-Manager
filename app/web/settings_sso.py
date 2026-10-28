"""The Single sign-on tab: the identity provider settings, the group mapping and the test.

Superadmin only: every route holds the settings capability, so any other group gets 403. The
client secret is never put back into the page. The mapping rows are part of the one form and are
edited on the server: adding or removing a row answers the rows only, so a secret typed in the
meantime stays in the browser. Turning sign in on from off asks for a confirmation first, in a
dialog that is only offered while the settings are complete; a save that would turn it on without
that confirmation is refused. The test fetches the provider's discovery document and signing keys
and saves nothing.
"""

from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.templating import render
from app.services.app_settings import (
    SSO_CLIENT_SECRET_KEY,
    SSO_GROUPS,
    SsoSettings,
    StoredSettings,
    has_secret,
    has_secret_in,
)
from app.services.sso import redirect_uri
from app.services.sso_provider import SsoProvider
from app.services.sso_settings import (
    MAX_MAPPINGS,
    SsoForm,
    SsoFormError,
    check_discovery,
    review_sso,
    save_sso,
)
from app.web.dependencies import (
    audit_actor,
    get_crypto,
    get_db,
    get_sso_provider,
    get_stored_settings,
)
from app.web.htmx import hx_events
from app.web.settings_tabs import ManageSettings, tab_context

router = APIRouter()

FORM_FRAGMENT = "fragments/settings_sso.html"
MAPPINGS_FRAGMENT = "fragments/settings_sso_mappings.html"
ENABLE_FRAGMENT = "fragments/settings_sso_enable.html"
TEST_FRAGMENT = "fragments/settings_sso_test.html"
GROUP_OPTIONS = [(group, group.capitalize()) for group in SSO_GROUPS]
NEW_ROW = ("", "viewer")

Database = Annotated[AsyncSession, Depends(get_db)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]
Provider = Annotated[SsoProvider, Depends(get_sso_provider)]


def read_form(
    enabled: Annotated[str, Form()] = "",
    issuer: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    roles_claim: Annotated[str, Form()] = "",
    account_url: Annotated[str, Form()] = "",
    create_users: Annotated[str, Form()] = "",
    link_by_email: Annotated[str, Form()] = "",
    mapping_role: Annotated[list[str] | None, Form()] = None,
    mapping_group: Annotated[list[str] | None, Form()] = None,
    client_secret: Annotated[str, Form()] = "",
    remove_stored: Annotated[str, Form(alias="remove_client_secret")] = "",
) -> SsoForm:
    """The submitted form: a checkbox is on when it was sent, the rows are the repeated role and
    group fields taken in the order they were sent."""
    rows = tuple(zip(mapping_role or [], mapping_group or [], strict=False))
    return SsoForm(
        enabled == "on",
        issuer,
        client_id,
        roles_claim,
        account_url,
        create_users == "on",
        link_by_email == "on",
        rows,
        client_secret,
        remove_stored == "on",
    )


Submitted = Annotated[SsoForm, Depends(read_form)]


def _defaults(form: SsoForm) -> dict[str, str]:
    """What an empty roles claim and account address stand for with the typed issuer and client."""
    derived = SsoSettings(issuer=form.issuer.strip(), client_id=form.client_id.strip())
    return {
        "roles_claim": (
            derived.effective_roles_claim if derived.client_id else "resource_access.<client>.roles"
        ),
        "account_url": derived.effective_account_url if derived.issuer else "",
    }


def _form_context(
    form: SsoForm,
    *,
    secret_stored: bool,
    sso_on: bool,
    errors: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the form needs: values, refusals, the choices, the toast and what is stored.

    ``secret_stored`` and ``sso_on`` are read by the caller: from the request's settings when
    nothing was written, from the database after a save.
    """
    return {
        "form": form,
        "errors": errors or {},
        "saved": saved,
        "secret_stored": secret_stored,
        "sso_on": sso_on,
        "defaults": _defaults(form),
        "redirect_uri": redirect_uri(get_settings().base_url),
        **_mapping_context(form, (errors or {}).get("mappings")),
    }


def _mapping_context(form: SsoForm, error: str | None) -> dict[str, Any]:
    """What the mapping rows need: the rows, the group choices, the refusal and the row limit."""
    return {
        "rows": list(form.mappings),
        "group_options": GROUP_OPTIONS,
        "error": error,
        "can_add": len(form.mappings) < MAX_MAPPINGS,
    }


def _shown(form: SsoForm) -> SsoForm:
    """The form to answer with: everything as typed except the secret, which is never echoed."""
    return replace(form, client_secret="")


@router.get("/settings/sso", include_in_schema=False)
async def sso_page(request: Request, context: ManageSettings, stored: Stored) -> Response:
    """The Single sign-on tab."""
    page = {
        **tab_context("sso"),
        **_form_context(
            SsoForm.from_settings(stored.sso),
            secret_stored=has_secret_in(stored, SSO_CLIENT_SECRET_KEY),
            sso_on=stored.sso_on,
        ),
    }
    return render(request, "pages/settings_sso.html", page)


@router.post("/settings/sso", include_in_schema=False)
async def save_sso_route(
    request: Request,
    db: Database,
    crypto: Crypto,
    context: ManageSettings,
    stored: Stored,
    form: Submitted,
    confirm_enable: Annotated[str, Form()] = "",
) -> Response:
    """Save the settings. A refused value answers the form again with the reasons (422)."""
    secret_stored = has_secret_in(stored, SSO_CLIENT_SECRET_KEY)
    try:
        clean = review_sso(
            form,
            secret_stored=secret_stored,
            was_on=stored.sso_on,
            confirmed=confirm_enable == "1",
        )
    except SsoFormError as error:
        page = _form_context(
            _shown(form),
            secret_stored=secret_stored,
            sso_on=stored.sso_on,
            errors=error.errors,
        )
        response = render(request, FORM_FRAGMENT, page, status_code=422)
        response.headers["HX-Trigger"] = hx_events("close-dialogs")
        return response
    changed = await save_sso(db, crypto, clean, actor=audit_actor(request, context))
    if changed:
        saved = {
            "tone": "success",
            "title": "Single sign-on settings saved",
            "message": "They apply to the next sign in.",
        }
    else:
        saved = {
            "tone": "info",
            "title": "Nothing to save",
            "message": "The settings are unchanged.",
        }
    stored_now = await has_secret(db, SSO_CLIENT_SECRET_KEY)
    page = _form_context(
        SsoForm.from_settings(clean.settings),
        secret_stored=stored_now,
        sso_on=clean.settings.configured and stored_now,
        saved=saved,
    )
    response = render(request, FORM_FRAGMENT, page)
    response.headers["HX-Trigger"] = hx_events("close-dialogs")
    return response


@router.post("/settings/sso/mappings", include_in_schema=False)
async def edit_mappings(
    request: Request,
    context: ManageSettings,
    form: Submitted,
    row_action: Annotated[str, Form()] = "",
) -> Response:
    """Add a row (``add``), remove one (``remove:<position>``) or move one up or down
    (``up:<position>``, ``down:<position>``) and answer the rows. A move past either end of the
    list changes nothing."""
    rows = list(form.mappings)
    verb, _, position = row_action.partition(":")
    index = int(position) if position.isdigit() else -1
    if verb == "add" and len(rows) < MAX_MAPPINGS:
        rows.append(NEW_ROW)
    elif verb == "remove" and 0 <= index < len(rows):
        del rows[index]
    elif verb in ("up", "down") and 0 <= index < len(rows):
        other = index - 1 if verb == "up" else index + 1
        if 0 <= other < len(rows):
            rows[index], rows[other] = rows[other], rows[index]
    shown = replace(form, mappings=tuple(rows))
    return render(request, MAPPINGS_FRAGMENT, _mapping_context(shown, None))


@router.post("/settings/sso/enable", include_in_schema=False)
async def review_enable(
    request: Request, context: ManageSettings, stored: Stored, form: Submitted
) -> Response:
    """The confirmation of turning sign in on, or the form again with what is missing (422)."""
    secret_stored = has_secret_in(stored, SSO_CLIENT_SECRET_KEY)
    try:
        review_sso(
            replace(form, enabled=True),
            secret_stored=secret_stored,
            was_on=stored.sso_on,
            confirmed=True,
        )
    except SsoFormError as error:
        page = _form_context(
            _shown(replace(form, enabled=True)),
            secret_stored=secret_stored,
            sso_on=stored.sso_on,
            errors=error.errors,
        )
        response = render(request, FORM_FRAGMENT, page, status_code=422)
        response.headers["HX-Retarget"] = "#sso"
        response.headers["HX-Reswap"] = "outerHTML"
        response.headers["HX-Trigger"] = hx_events("close-dialogs")
        return response
    return render(request, ENABLE_FRAGMENT, {})


@router.post("/settings/sso/test", include_in_schema=False)
async def test_discovery(
    request: Request,
    context: ManageSettings,
    stored: Stored,
    provider: Provider,
    issuer: Annotated[str, Form()] = "",
) -> Response:
    """Fetch the discovery document and the signing keys of the typed issuer, or the stored one
    when none is typed, and answer how it went. Nothing is saved."""
    check = await check_discovery(provider, issuer.strip() or stored.sso.issuer)
    return render(request, TEST_FRAGMENT, {"check": check})
