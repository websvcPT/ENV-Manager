"""The answers that drive htmx: redirects and client events have one shape."""

import json
import re
from pathlib import Path

from app.web.htmx import ANSWER_HEADERS, events_response, hx_events, hx_redirect

WEB = Path(__file__).resolve().parents[1] / "web"
REDIRECT_HEADER = re.compile(r"""["']HX-Redirect["']""", re.IGNORECASE)
PRIVATE_COPY = re.compile(r"^\s*def _(redirect|events)\(", re.MULTILINE)


def test_events_are_one_json_object_in_the_order_given() -> None:
    assert json.loads(hx_events("paths-changed", "close-dialogs")) == {
        "paths-changed": True,
        "close-dialogs": True,
    }
    assert list(json.loads(hx_events("b", "a"))) == ["b", "a"]
    assert json.loads(hx_events("deploy-finished")) == {"deploy-finished": True}


def test_an_events_response_is_an_empty_204_that_is_never_cached() -> None:
    response = events_response("user-changed", "close-dialogs")
    assert response.status_code == 204 and response.body == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"
    assert json.loads(response.headers["hx-trigger"]) == {
        "user-changed": True,
        "close-dialogs": True,
    }


def test_a_redirect_is_an_empty_answer_that_is_never_cached() -> None:
    response = hx_redirect("/projects")
    assert response.status_code == 200 and response.body == b""
    assert response.headers["hx-redirect"] == "/projects"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "HX-Request"
    assert "hx-trigger" not in response.headers


def test_a_redirect_keeps_the_status_and_headers_of_the_caller_and_stays_open() -> None:
    response = hx_redirect("/login", status_code=401, headers={"WWW-Authenticate": "Session"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Session"
    response.set_cookie("session", "", max_age=0, path="/")
    assert "session=" in response.headers["set-cookie"]
    assert response.headers["hx-redirect"] == "/login"


def test_a_caller_header_cannot_weaken_the_cache_headers() -> None:
    response = hx_redirect("/", headers={"Cache-Control": "public", "Vary": "Cookie"})
    assert response.headers["cache-control"] == ANSWER_HEADERS["Cache-Control"]
    assert response.headers["vary"] == ANSWER_HEADERS["Vary"]


def test_only_the_helpers_write_an_hx_redirect_answer() -> None:
    """No route builds the header by hand or keeps a private copy of a helper."""
    offenders = []
    for path in sorted(WEB.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.name != "htmx.py" and REDIRECT_HEADER.search(source):
            offenders.append(f"{path.name}: writes the HX-Redirect header")
        if PRIVATE_COPY.search(source):
            offenders.append(f"{path.name}: keeps a private _redirect or _events")
    assert offenders == []
