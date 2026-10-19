"""Startup configuration that guards security limits."""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError

from app.config import Settings, get_settings
from app.core.csrf import CSRF_COOKIE_NAME
from app.main import create_app
from app.services.app_settings import SecuritySettings
from app.services.auth_policy import AuthPolicy, policy_from_settings


def test_the_address_failure_limit_defaults_to_the_policy_default() -> None:
    """Unset, the sign-in limit per address is the code default."""
    settings = Settings(environment="production", csrf_secret=SecretStr("x" * 40))
    assert settings.login_ip_max_failures is None
    policy = policy_from_settings(
        SecuritySettings(), ip_max_failures_override=settings.login_ip_max_failures
    )
    assert policy == AuthPolicy()


@pytest.mark.parametrize("environment", ["development", "testing"])
def test_a_higher_address_limit_is_accepted_in_development_and_testing(environment: str) -> None:
    """Browser specs share one client address, so these environments may raise the limit."""
    settings = Settings(environment=environment, login_ip_max_failures=500)
    policy = policy_from_settings(
        SecuritySettings(), ip_max_failures_override=settings.login_ip_max_failures
    )
    assert policy.ip_max_failures == 500
    assert policy.user_max_failures == AuthPolicy().user_max_failures
    assert policy.api_ip_max_failures == AuthPolicy().api_ip_max_failures


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_the_address_limit_override_is_refused_anywhere_else(environment: str) -> None:
    """A value left over from a development setup must not weaken the lockout in production."""
    with pytest.raises(ValidationError, match="ENVMANAGER_LOGIN_IP_MAX_FAILURES is only accepted"):
        Settings(
            environment=environment,
            csrf_secret=SecretStr("x" * 40),
            login_ip_max_failures=500,
        )


@pytest.mark.parametrize("value", [0, -1, 100_001])
def test_the_address_limit_must_be_a_sensible_number(value: int) -> None:
    """Zero, negative and absurd limits are refused at startup."""
    with pytest.raises(ValidationError, match="between 1 and 100000"):
        Settings(environment="development", login_ip_max_failures=value)


def test_the_address_limit_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ENVMANAGER_LOGIN_IP_MAX_FAILURES`` is the variable."""
    monkeypatch.setenv("ENVMANAGER_LOGIN_IP_MAX_FAILURES", "250")
    assert Settings(environment="development").login_ip_max_failures == 250


async def sign_in_failures(application: FastAPI, count: int) -> int:
    """Fail to sign in ``count`` times as different names from one address; the last status."""
    transport = httpx.ASGITransport(app=application, client=("198.51.100.7", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        await http.get("/healthz")
        status = 0
        for index in range(count):
            response = await http.post(
                "/login",
                data={
                    "identifier": f"nobody{index}",
                    "password": "wrong",
                    "csrf_token": http.cookies[CSRF_COOKIE_NAME],
                },
            )
            status = response.status_code
    return status


async def test_one_address_is_locked_at_the_default_limit(web_app: FastAPI) -> None:
    """Without the override twenty failed sign ins from one address lock it."""
    assert web_app.state.login_ip_max_failures is None
    assert await sign_in_failures(web_app, 19) == 401
    assert await sign_in_failures(web_app, 1) == 429


@pytest.fixture
def raised_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise the address limit for the application the next fixture builds."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    monkeypatch.setenv("ENVMANAGER_LOGIN_IP_MAX_FAILURES", "60")


async def test_one_address_keeps_signing_in_under_a_raised_limit(
    raised_limit: None, web_app: FastAPI
) -> None:
    """The startup override reaches the live policy: the address is not locked at twenty."""
    assert web_app.state.login_ip_max_failures == 60
    assert await sign_in_failures(web_app, 25) == 401


async def get_status(path: str) -> int:
    """The status of a GET to a freshly built application, outside any lifespan."""
    get_settings.cache_clear()
    try:
        application = create_app()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return (await http.get(path)).status_code
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_the_api_docs_are_not_served_by_default(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``ENVMANAGER_SERVE_DOCS`` the docs pages and the schema answer 404."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    assert Settings().serve_docs is False
    assert await get_status(path) == 404


async def test_the_api_schema_is_served_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ENVMANAGER_SERVE_DOCS=true`` turns the schema route on."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    monkeypatch.setenv("ENVMANAGER_SERVE_DOCS", "true")
    assert await get_status("/openapi.json") == 200
