"""The one JSON error shape of the API: ``{"error": {"code", "message", "fields", "request_id"}}``.

Every answer under ``/api`` that is not a success goes through here: refusals of the services,
unknown paths and methods, a body or path that cannot be read, and unexpected errors. No answer
echoes what the client sent, and none can be cached.
"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.api import ErrorBody, ErrorResponse, FieldIssue
from app.services.api_errors import ApiRefusal, RefusalKind

NO_STORE = {"Cache-Control": "no-store"}
GENERIC_CODE = "error"
STATUS_BY_KIND = {
    RefusalKind.UNAUTHORIZED: 401,
    RefusalKind.FORBIDDEN: 403,
    RefusalKind.NOT_FOUND: 404,
    RefusalKind.INVALID: 422,
    RefusalKind.DEPLOY_LOCKED: 409,
    RefusalKind.LOCK_UNAVAILABLE: 503,
    RefusalKind.RATE_LIMITED: 429,
}
CODE_BY_STATUS = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "deploy_locked",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "lock_unavailable",
}
CODE_BY_KIND = {kind: CODE_BY_STATUS[status] for kind, status in STATUS_BY_KIND.items()}
PHRASES = {
    404: "Not found.",
    405: "Method not allowed.",
    500: "Internal server error.",
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    fields: tuple[FieldIssue, ...] = (),
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """An error answer in the shared shape, with headers that keep it out of every cache."""
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, fields=list(fields), request_id=request_id)
    )
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status_code,
        headers={**NO_STORE, **(headers or {})},
    )


async def refusal_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a refused call: 401 carries the challenge, 429 the wait."""
    if not isinstance(exc, ApiRefusal):
        raise exc
    headers: dict[str, str] = {}
    if exc.kind is RefusalKind.UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return error_response(
        STATUS_BY_KIND[exc.kind],
        CODE_BY_KIND[exc.kind],
        exc.message,
        fields=exc.fields,
        headers=headers,
    )


def http_error_response(status_code: int, headers: dict[str, str] | None = None) -> JSONResponse:
    """The answer for an HTTP error raised by the framework, such as an unknown path or method.

    The framework's own wording is replaced; the headers it set (``Allow`` on a wrong method) are
    kept.
    """
    return error_response(
        status_code,
        CODE_BY_STATUS.get(status_code, GENERIC_CODE),
        PHRASES.get(status_code, "The request could not be completed."),
        headers=headers,
    )


def internal_error_response(request_id: str) -> JSONResponse:
    """The answer for an unexpected error: no text from the error, only a request id."""
    return error_response(
        500,
        CODE_BY_STATUS[500],
        PHRASES[500],
        request_id=request_id,
    )


def validation_error_response(exc: RequestValidationError) -> JSONResponse:
    """A request the framework could not read, with the places that failed and no input echoed."""
    fields = tuple(
        FieldIssue(field=".".join(str(part) for part in item["loc"]), message=item["msg"])
        for item in exc.errors()[:20]
    )
    return error_response(422, CODE_BY_STATUS[422], "The request is not valid.", fields=fields)
