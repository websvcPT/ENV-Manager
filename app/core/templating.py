"""Jinja2 environment and page rendering shared by every HTML route."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from app import VERSION
from app.core.navigation import initials, nav_items_for

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
HTMX_VERSION = "4.0.0"


def base_context(request: Request) -> dict[str, Any]:
    """Values every template can rely on: CSRF token, signed-in user and navigation."""
    auth = getattr(request.state, "auth", None)
    user = auth.user if auth is not None else None
    return {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "app_version": VERSION,
        "htmx_version": HTMX_VERSION,
        "current_user": user,
        "nav_items": nav_items_for(user.group) if user is not None else [],
        "user_initials": initials(user.name) if user is not None else "",
    }


environment = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    undefined=StrictUndefined,
)
templates = Jinja2Templates(env=environment, context_processors=[base_context])


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a page or fragment with the headers every authenticated response needs.

    Responses are never stored by caches and vary on `HX-Request`, because the same URL can
    answer a full page or a fragment.
    """
    response: HTMLResponse = templates.TemplateResponse(
        request, name, context or {}, status_code=status_code
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "HX-Request"
    return response
