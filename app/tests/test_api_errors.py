"""One JSON error shape for every answer under /api, and no echo of what the client sent."""

import httpx
import pytest
from fastapi.exceptions import RequestValidationError

from app.api.errors import validation_error_response
from app.core.security_headers import SECURITY_HEADERS
from app.models import DeployStatus, PostDeployStatus
from app.services.deploy import DeployEngine
from app.services.deploy_lock import hold_deploy_lock
from app.services.deploy_types import DeployOutcome, DeployRefusal
from app.services.hooks import PostDeployResult
from app.services.tokens import OPERATION_READ_LAST_DEPLOYMENT
from app.tests.api_helpers import ApiWorld, bearer, deploy, last, make_token

ERROR_KEYS = {"code", "message", "fields", "request_id"}


def assert_shape(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    """The answer has the shared error shape, the right status and code and cannot be cached."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == ERROR_KEYS
    error: dict[str, object] = body["error"]
    assert error["code"] == code and isinstance(error["message"], str) and error["message"]
    assert isinstance(error["fields"], list)
    return error


async def test_every_error_answer_has_the_same_shape(
    api_world: ApiWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401, 403, 404, 405, 409, 422, 500 and 503 share the keys, the headers and the codes."""
    world = api_world
    secret = await make_token(world, "admin")
    read_only = await make_token(
        world, "admin", operations=(OPERATION_READ_LAST_DEPLOYMENT,), name="ro"
    )
    mysql = world.services["prod/mysql"]

    assert_shape(await last(world, "nope", mysql), 401, "unauthorized")
    assert_shape(await deploy(world, read_only, mysql), 403, "forbidden")
    assert_shape(await last(world, secret, 987654), 404, "not_found")
    assert_shape(await world.http.get("/api/v1/nothing"), 404, "not_found")
    assert_shape(await world.http.get("/api/v1/nothing", headers=bearer(secret)), 404, "not_found")
    allowed = await world.http.get(f"/api/v1/services/{mysql}/deploy", headers=bearer(secret))
    assert_shape(allowed, 405, "method_not_allowed")
    assert allowed.headers["allow"] == "POST"
    wrong = await world.http.post(
        f"/api/v1/services/{mysql}/deployments/last", headers=bearer(secret)
    )
    assert_shape(wrong, 405, "method_not_allowed")
    with hold_deploy_lock(world.app.state.lock_dir, str(world.roots["prod"])):
        assert_shape(await deploy(world, secret, mysql), 409, "deploy_locked")
    invalid = assert_shape(
        await deploy(world, secret, mysql, {"force": True}), 422, "validation_error"
    )
    assert invalid["fields"] and set(invalid["fields"][0]) == {"field", "message"}  # type: ignore[index]

    async def unavailable(
        self: DeployEngine, *arguments: object, **keywords: object
    ) -> DeployOutcome:
        return DeployOutcome(
            None,
            DeployStatus.FAILED,
            False,
            False,
            (),
            PostDeployResult(PostDeployStatus.NONE),
            refusal=DeployRefusal.LOCK_UNAVAILABLE,
        )

    monkeypatch.setattr(DeployEngine, "run", unavailable)
    assert_shape(await deploy(world, secret, mysql), 503, "lock_unavailable")


async def test_an_unexpected_error_answers_with_a_request_id_and_nothing_else(
    api_world: ApiWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server error shows no text of the error, only the request id."""
    world = api_world
    secret = await make_token(world, "admin")

    async def explode(*arguments: object, **keywords: object) -> None:
        raise RuntimeError("secret detail the client must not see")

    monkeypatch.setattr("app.api.v1.deploy_service", explode)
    response = await deploy(world, secret, world.services["prod/mysql"])
    error = assert_shape(response, 500, "internal_error")
    assert isinstance(error["request_id"], str) and len(error["request_id"]) == 8
    assert "secret detail" not in response.text and "RuntimeError" not in response.text


async def test_a_rate_limited_answer_has_the_same_shape(api_world: ApiWorld) -> None:
    """429 shares the shape and adds only the Retry-After header."""
    world = api_world
    for _ in range(10):
        response = await last(world, "nope", 1)
    error = assert_shape(response, 429, "rate_limited")
    assert response.headers["retry-after"] == "900" and error["request_id"] is None


def test_a_request_the_framework_cannot_read_is_answered_without_its_input() -> None:
    """A path or query problem reports where it failed and never echoes the input."""
    error = RequestValidationError(
        [
            {
                "type": "int_parsing",
                "loc": ("path", "id"),
                "msg": "Input should be an integer",
                "input": "SECRET",
            }
        ]
    )
    response = validation_error_response(error)
    assert response.status_code == 422 and b"SECRET" not in response.body
    assert b'"field":"path.id"' in response.body
    assert response.headers["cache-control"] == "no-store"
