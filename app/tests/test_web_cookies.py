"""The one place the web routes set a cookie that carries a credential."""

from collections.abc import Iterator
from typing import Literal

import pytest
from starlette.responses import Response

from app.config import get_settings
from app.web.cookies import set_private_cookie

SameSite = Literal["lax", "strict"]


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    """The settings are cached; read them again under each test's environment and after it."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def cookie_header(samesite: SameSite = "lax", path: str = "/") -> str:
    """The Set-Cookie header of a cookie set through the helper."""
    response = Response()
    set_private_cookie(response, "token", "value", max_age=60, path=path, samesite=samesite)
    return response.headers["set-cookie"]


@pytest.mark.parametrize(("samesite", "path"), [("lax", "/"), ("strict", "/login")])
def test_a_private_cookie_carries_its_scope_and_is_never_readable_by_scripts(
    monkeypatch: pytest.MonkeyPatch, samesite: SameSite, path: str
) -> None:
    monkeypatch.setenv("ENVMANAGER_BASE_URL", "http://localhost:8080")
    header = cookie_header(samesite, path)
    assert header.startswith("token=value;")
    assert "HttpOnly" in header
    assert f"SameSite={samesite}" in header
    assert f"Path={path}" in header
    assert "Max-Age=60" in header


def test_a_private_cookie_is_secure_when_the_base_url_is_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVMANAGER_BASE_URL", "https://env.example.test")
    assert "Secure" in cookie_header()


def test_a_private_cookie_is_not_secure_when_the_base_url_is_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVMANAGER_BASE_URL", "http://localhost:8080")
    assert "Secure" not in cookie_header()
