"""Page shell, error pages and static assets, on the seeded application."""

import html
import json
import re
import struct
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.core.csrf import CSRF_COOKIE_NAME
from app.core.security_headers import CONTENT_SECURITY_POLICY
from app.models import UserGroup
from app.tests.conftest import sign_in
from app.tests.web_helpers import Crumb, breadcrumb_of
from app.web.dependencies import require_auth, require_group

NAV_PATTERN = re.compile(r'<nav aria-label="Primary".*?</nav>', re.DOTALL)
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
STATIC = Path(__file__).resolve().parent.parent / "static"
BRAND = "/static/brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sidebar_links(page: str) -> list[str]:
    """The hrefs in the primary navigation, in order."""
    match = NAV_PATTERN.search(page)
    assert match is not None
    return re.findall(r'<a href="([^"]+)"', match.group(0))


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("root", ["/projects", "/audit", "/users", "/settings"]),
        ("admin", ["/projects", "/audit"]),
        ("viewer", ["/projects"]),
    ],
)
async def test_sidebar_items_follow_the_group(
    web_client: httpx.AsyncClient, user: str, expected: list[str]
) -> None:
    """Each group sees only its own navigation entries."""
    await sign_in(web_client, user)
    response = await web_client.get("/projects")
    assert response.status_code == 200
    assert sidebar_links(response.text) == expected


async def test_projects_page_renders_the_shell(web_client: httpx.AsyncClient) -> None:
    """The page has sidebar, top bar, page header, empty state, mobile menu and role badge."""
    await sign_in(web_client, "root")
    response = await web_client.get("/projects")
    page = response.text
    for marker in (
        "data-nav",
        'data-fragment="page-header"',
        'data-fragment="projects-table"',
        'data-fragment="banner-area"',
        'id="mobile-menu"',
        'data-status="superadmin"',
        'data-js="theme-toggle"',
        'data-js="nav-toggle"',
        'aria-current="page"',
        "Account menu, root",
    ):
        assert marker in page
    assert response.headers["cache-control"] == "no-store"
    assert "HX-Request" in response.headers["vary"]
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_home_redirects_a_signed_in_user_to_projects(
    web_client: httpx.AsyncClient,
) -> None:
    """Login lands on `/`, which forwards to the projects page."""
    await sign_in(web_client, "root")
    response = await web_client.get("/")
    assert (response.status_code, response.headers["location"]) == (303, "/projects")


async def test_body_carries_the_csrf_header_for_htmx(web_client: httpx.AsyncClient) -> None:
    """htmx requests inherit the token header, and it equals the token cookie."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get("/projects")).text
    match = re.search(r"<body[^>]*hx-headers:inherited='([^']*)'", page)
    assert match is not None
    assert json.loads(html.unescape(match.group(1))) == {
        "X-CSRFToken": web_client.cookies[CSRF_COOKIE_NAME]
    }


async def test_head_configures_htmx_and_loads_only_local_assets(
    web_client: httpx.AsyncClient,
) -> None:
    """The htmx config meta precedes the vendored script, and every asset is served."""
    await sign_in(web_client, "root")
    page = (await web_client.get("/projects")).text
    config = re.search(r'<meta name="htmx-config" content=\'([^\']*)\'', page)
    assert config is not None
    assert json.loads(config.group(1))["mode"] == "same-origin"
    assert page.index('name="htmx-config"') < page.index("/static/htmx/htmx-")
    sources = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert "/static/app.js" in sources and "/static/style.css" in sources
    for source in sources:
        assert (await web_client.get(source)).status_code == 200


def region(page: str, pattern: str) -> str:
    """The first part of a page that matches a pattern, which must exist."""
    match = re.search(pattern, page, re.DOTALL)
    assert match is not None
    return match.group(0)


async def test_shell_marks_follow_the_surface_they_sit_on(web_client: httpx.AsyncClient) -> None:
    """Navy sidebar and menu take the transparent mark, the top bar the solid tile."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get("/projects")).text
    sidebar = region(page, r'<nav aria-label="Primary".*?</nav>')
    menu = region(page, r'<dialog id="mobile-menu".*?</dialog>')
    top_bar = region(page, r"<header.*?</header>")
    for navy in (sidebar, menu):
        assert f'src="{BRAND}/env-mark-transparent.svg" alt=""' in navy
        assert "env-mark-solid" not in navy
    assert f'src="{BRAND}/env-mark-solid.svg" alt=""' in top_bar
    assert "env-mark-transparent" not in top_bar
    for part in (sidebar, menu, top_bar):
        assert "ENV Manager" in part


async def test_the_collapsed_sidebar_keeps_the_brand_name_for_assistive_technology(
    web_client: httpx.AsyncClient,
) -> None:
    """Collapsing hides the heading visually only (it stays in the accessibility tree), so the
    mark next to it still has a name."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get("/projects")).text
    sidebar = region(page, r'<nav aria-label="Primary".*?</nav>')
    heading = region(sidebar, r"<span data-brand-name[^>]*>ENV Manager</span>")
    assert "group-data-[collapsed=true]:sr-only" in heading
    assert "group-data-[collapsed=true]:hidden" not in heading


async def test_lockup_appears_only_on_pages_without_the_shell(
    web_client: httpx.AsyncClient,
) -> None:
    """Sign in and the anonymous error page carry the lockup, the shell not."""
    lockup = f'src="{BRAND}/env-vertical-solid.svg" alt=""'
    for path in ("/login", "/does-not-exist"):
        assert lockup in (await web_client.get(path)).text, path
    login = region((await web_client.get("/login")).text, r'<div id="login-card".*?</h1>')
    assert login.index(lockup) < login.index("ENV Manager")
    await sign_in(web_client, "viewer")
    for path in ("/projects", "/does-not-exist"):
        assert lockup not in (await web_client.get(path)).text, path


async def test_the_shell_holds_the_shared_row_menu_once_and_bare_pages_not_at_all(
    web_client: httpx.AsyncClient,
) -> None:
    """Row actions open one dialog of the shell, filled by the page script."""
    assert 'id="row-menu"' not in (await web_client.get("/login")).text
    await sign_in(web_client, "viewer")
    for path in ("/projects", "/does-not-exist"):
        assert (await web_client.get(path)).text.count('id="row-menu"') == 1, path


async def test_every_page_head_links_the_favicons(web_client: httpx.AsyncClient) -> None:
    """Bare pages, the shell and error pages declare the SVG icon, the PNG and the touch icon."""
    pages = [(await web_client.get("/login")).text]
    await sign_in(web_client, "viewer")
    pages += [(await web_client.get(path)).text for path in ("/projects", "/does-not-exist")]
    for page in pages:
        head = region(page, r"<head>.*?</head>")
        assert f'<link rel="icon" href="{BRAND}/env-mark-solid.svg" type="image/svg+xml">' in head
        assert (
            f'<link rel="icon" href="{BRAND}/favicon-32.png" type="image/png" sizes="32x32">'
            in head
        )
        assert (
            f'<link rel="apple-touch-icon" href="{BRAND}/apple-touch-icon.png" sizes="180x180">'
            in head
        )


@pytest.mark.parametrize(
    ("name", "media_type", "png_size"),
    [
        ("env-mark-solid.svg", "image/svg+xml", None),
        ("env-mark-transparent.svg", "image/svg+xml", None),
        ("env-vertical-solid.svg", "image/svg+xml", None),
        ("favicon-32.png", "image/png", (32, 32)),
        ("apple-touch-icon.png", "image/png", (180, 180)),
    ],
)
async def test_brand_files_are_served_with_the_media_type_nosniff_needs(
    web_client: httpx.AsyncClient,
    name: str,
    media_type: str,
    png_size: tuple[int, int] | None,
) -> None:
    """A wrong media type would break the image silently under nosniff."""
    response = await web_client.get(f"{BRAND}/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == media_type
    assert response.headers["x-content-type-options"] == "nosniff"
    if png_size is not None:
        assert response.content[:8] == PNG_SIGNATURE
        assert struct.unpack(">II", response.content[16:24]) == png_size


def test_apple_touch_icon_is_opaque() -> None:
    """iOS paints transparent pixels black, so the icon has no alpha channel."""
    data = (STATIC / "brand" / "apple-touch-icon.png").read_bytes()
    color_type = data[25]
    assert color_type in (0, 2)


@pytest.mark.parametrize("path", sorted((STATIC / "brand").glob("*.svg")), ids=lambda p: p.name)
def test_served_svgs_carry_a_drawing_and_nothing_else(path: Path) -> None:
    """One svg root sized by a viewBox, no content manifest, script, style or event handler."""
    text = path.read_text(encoding="utf-8")
    assert re.match(r'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 \d+ \d+"[^>]*>', text)
    assert text.count("<svg") == 1 and text.rstrip().endswith("</svg>")
    assert not re.search(r"metadata|c2pa|<script|\sstyle=|\son\w+=|href=", text, re.IGNORECASE)
    assert chr(0x2014) not in text
    assert len(text) < 4000


def test_every_brand_file_is_used_by_a_template() -> None:
    """Only the brand files the application uses are kept."""
    templates = "".join(path.read_text(encoding="utf-8") for path in TEMPLATES.rglob("*.html"))
    for path in (STATIC / "brand").iterdir():
        assert f"{BRAND}/{path.name}" in templates, path.name


async def test_rendered_page_has_no_inline_script_or_style(web_client: httpx.AsyncClient) -> None:
    """The strict CSP forbids inline script, inline style and event handler attributes."""
    await sign_in(web_client, "root")
    page = (await web_client.get("/projects")).text
    assert not re.search(r"<script(?![^>]*\ssrc=)", page)
    assert not re.search(r"\sstyle=|\sonclick=|javascript:", page)


async def test_unknown_path_renders_the_not_found_page_inside_the_shell(
    web_client: httpx.AsyncClient,
) -> None:
    """A signed-in browser gets the 404 page with navigation."""
    await sign_in(web_client, "admin")
    response = await web_client.get("/does-not-exist")
    assert response.status_code == 404
    assert 'data-fragment="system-state"' in response.text
    assert "Not found" in response.text
    assert sidebar_links(response.text) == ["/projects", "/audit"]
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_anonymous_not_found_page_has_no_navigation(web_client: httpx.AsyncClient) -> None:
    """Without a session the 404 page renders without the shell."""
    response = await web_client.get("/does-not-exist")
    assert response.status_code == 404
    assert "Not found" in response.text
    assert "data-nav" not in response.text


async def test_not_found_stays_json_for_api_and_non_browser_clients(
    web_client: httpx.AsyncClient,
) -> None:
    """The API prefix answers in the API error shape; clients that do not ask for HTML get JSON."""
    api = await web_client.get("/api/v1/nothing")
    assert api.status_code == 404
    assert api.json() == {
        "error": {"code": "not_found", "message": "Not found.", "fields": [], "request_id": None}
    }
    plain = await web_client.get("/does-not-exist", headers={"Accept": "*/*"})
    assert plain.json() == {"detail": "Not Found"}


async def test_failed_htmx_request_is_not_swapped(web_client: httpx.AsyncClient) -> None:
    """An htmx request to a missing page gets an empty answer that htmx must not swap."""
    response = await web_client.get("/does-not-exist", headers={"HX-Request": "true"})
    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["hx-reswap"] == "none"


async def test_forbidden_page_for_a_user_without_the_group(
    web_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    """A group check failure renders the 403 page inside the shell."""

    @web_app.get("/superadmin-only", dependencies=[Depends(require_group(UserGroup.SUPERADMIN))])
    async def superadmin_only() -> dict[str, str]:
        return {}

    await sign_in(web_client, "viewer")
    response = await web_client.get("/superadmin-only")
    assert response.status_code == 403
    assert "No access" in response.text
    assert sidebar_links(response.text) == ["/projects"]


async def test_unhandled_error_page_hides_details_and_keeps_security_headers(
    web_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    """The 500 page shows a request id, never the exception text, and carries the CSP."""

    @web_app.get("/boom", dependencies=[Depends(require_auth)])
    async def boom() -> dict[str, str]:
        raise RuntimeError("secret-value-in-message")

    await sign_in(web_client, "root")
    page = await web_client.get("/boom")
    assert page.status_code == 500
    assert "Something went wrong" in page.text
    assert re.search(r"Request id [0-9a-f]{8}", page.text)
    assert "secret-value-in-message" not in page.text
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert page.headers["cache-control"] == "no-store"
    api = await web_client.get("/boom", headers={"Accept": "application/json"})
    assert api.status_code == 500
    assert set(api.json()) == {"detail", "request_id"}
    assert "secret-value-in-message" not in api.text


def is_emoji(character: str) -> bool:
    """Whether a character is in the emoji and pictograph blocks."""
    code = ord(character)
    return code >= 0x1F000 or 0x2600 <= code <= 0x27BF


def template_files() -> list[Path]:
    """Every template and the page script."""
    return [*sorted(TEMPLATES.rglob("*.html")), STATIC / "app.js"]


@pytest.mark.parametrize("path", template_files(), ids=lambda path: path.name)
def test_sources_follow_the_content_rules(path: Path) -> None:
    """No em-dash, emoji, inline style, inline script, inline handler or script URL."""
    text = path.read_text(encoding="utf-8")
    assert chr(0x2014) not in text
    assert not any(is_emoji(character) for character in text)
    if path.suffix == ".html":
        assert not re.search(r"\sstyle=|\sonclick=|javascript:", text)
        assert not re.search(r"<script(?![^>]*\ssrc=)", text)


async def test_account_menu_has_account_theme_and_logout(web_client: httpx.AsyncClient) -> None:
    """The top bar button opens a menu with the account link and a CSRF protected log out."""
    await sign_in(web_client, "admin")
    page = (await web_client.get("/projects")).text
    assert 'data-js="dialog-open" data-target="account-menu"' in page
    menu = re.search(r'<dialog id="account-menu".*?</dialog>', page, re.DOTALL)
    assert menu is not None
    body = menu.group(0)
    assert 'href="/account"' in body
    assert 'data-js="theme-toggle"' in body
    assert "admin@example.test" in body
    form = re.search(r'<form method="post" action="/logout".*?</form>', body, re.DOTALL)
    assert form is not None and 'name="csrf_token"' in form.group(0)
    assert "Log out" in form.group(0)


@pytest.mark.parametrize(("user", "listed"), [("root", True), ("admin", True), ("viewer", False)])
async def test_api_tokens_are_in_the_account_menu_for_the_groups_that_hold_tokens(
    web_client: httpx.AsyncClient, user: str, listed: bool
) -> None:
    """The account menu lists API tokens right under Account, the sidebar never does."""
    await sign_in(web_client, user)
    page = (await web_client.get("/projects")).text
    menu = re.search(r'<dialog id="account-menu".*?</dialog>', page, re.DOTALL)
    assert menu is not None
    body = menu.group(0)
    assert ('data-account-menu-item="tokens"' in body) is listed
    if listed:
        assert body.index('href="/account"') < body.index('href="/tokens"')
        assert body.index('href="/tokens"') < body.index('data-js="theme-toggle"')
    assert "/tokens" not in sidebar_links(page)


async def test_logout_form_ends_the_session(web_client: httpx.AsyncClient) -> None:
    """The menu form with its CSRF field signs the user out; a forged field is refused."""
    await sign_in(web_client, "viewer")
    page = (await web_client.get("/projects")).text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert token is not None
    refused = await web_client.post("/logout", data={"csrf_token": "forged"})
    assert refused.status_code == 403
    done = await web_client.post("/logout", data={"csrf_token": token.group(1)})
    assert (done.status_code, done.headers["location"]) == (303, "/login")
    assert (await web_client.get("/projects")).status_code == 303


async def test_a_page_with_one_item_has_no_compact_breadcrumb(
    web_client: httpx.AsyncClient,
) -> None:
    """With no parent there is no compact trail; with a parent it is parent and current."""
    await sign_in(web_client, "root")
    projects = (await web_client.get("/projects")).text
    assert breadcrumb_of(projects) == [Crumb("Projects", None, None)]
    assert 'data-breadcrumb="compact"' not in projects

    section = (await web_client.get("/settings/smtp")).text
    full = breadcrumb_of(section)
    assert [crumb.type for crumb in full] == [None, None]
    assert [(crumb.label, crumb.href) for crumb in breadcrumb_of(section, compact=True)] == [
        ("Settings", "/settings"),
        (full[-1].label, None),
    ]
