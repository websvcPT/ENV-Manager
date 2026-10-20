"""The API's request body, the result it answers and the error shape every answer shares.

The body model is strict: unknown keys and wrongly typed values are refused, and ``force`` gets
its own message because the API never overrides a block or a halt. The result models list every
field the API may send, so a value, file content, command text or command output cannot leak
through an attribute added to a record later.
"""

import json
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from app.models import (
    DeployResultStatus,
    DeployStatus,
    DistOutcome,
    DriftState,
    FileType,
    PostDeployStatus,
)

MAX_ID = 2**63 - 1
MAX_BODY_BYTES = 64 * 1024
MAX_ENVIRONMENTS = 200
MAX_REFERENCE_LENGTH = 100
MAX_ISSUES = 20
BODY_FIELD = "body"
FORCE_FIELD = "force"
FORCE_MESSAGE = (
    "force is not accepted by the API. A blocked or halted deploy must be resolved and forced "
    "by a person in the web interface."
)
_VALUE_ERROR_PREFIX = "Value error, "


class FieldIssue(BaseModel):
    """One problem with a field of the request."""

    field: str
    message: str


class ErrorBody(BaseModel):
    """What every error answer of the API carries.

    ``fields`` lists the problems of a refused body and is empty otherwise; ``request_id`` is set
    for an unexpected server error only.
    """

    code: str
    message: str
    fields: list[FieldIssue] = []
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The one JSON shape of every error answer under ``/api``."""

    error: ErrorBody


class InvalidBody(Exception):
    """The request body cannot be used; ``issues`` say what is wrong with it."""

    def __init__(self, issues: list[FieldIssue]) -> None:
        super().__init__("invalid request body")
        self.issues = issues


def _reference_problem(value: object, what: str) -> str | None:
    """Why a value is not an environment name or id, or None when it is one."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        return f"{what} must be an environment name or id."
    if isinstance(value, int):
        return None if 1 <= value <= MAX_ID else f"{what} is not a valid id."
    if not 1 <= len(value) <= MAX_REFERENCE_LENGTH:
        return f"{what} must have 1 to {MAX_REFERENCE_LENGTH} characters."
    return None


class DeployBody(BaseModel):
    """What a deploy request may carry. Every field is optional and nothing else is accepted.

    An environment is named by its file name or, as a number, by its id. ``versions`` maps an
    environment (its name, or its id written as text) to the version number to write.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    environments: list[StrictInt | StrictStr] | None = None
    versions: dict[StrictStr, StrictInt] | None = None
    dry_run: StrictBool | None = None

    @field_validator("environments", mode="before")
    @classmethod
    def _check_environments(cls, value: object) -> object:
        """Refuse anything but a short list of names and ids."""
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("Must be a list of environment names or ids.")
        if len(value) > MAX_ENVIRONMENTS:
            raise ValueError(f"Name at most {MAX_ENVIRONMENTS} environments.")
        for item in value:
            problem = _reference_problem(item, "Each item")
            if problem:
                raise ValueError(problem)
        return value

    @field_validator("versions", mode="before")
    @classmethod
    def _check_versions(cls, value: object) -> object:
        """Refuse anything but an object of environment to version number."""
        if value is None:
            return value
        if not isinstance(value, dict):
            raise ValueError("Must be an object that maps an environment to a version number.")
        if len(value) > MAX_ENVIRONMENTS:
            raise ValueError(f"Name at most {MAX_ENVIRONMENTS} environments.")
        for key, number in value.items():
            problem = _reference_problem(key, "Each key")
            if problem:
                raise ValueError(problem)
            if isinstance(number, bool) or not isinstance(number, int):
                raise ValueError("Each version must be a whole number.")
            if not 1 <= number <= MAX_ID:
                raise ValueError("A version number must be 1 or more.")
        return value

    @field_validator("dry_run", mode="before")
    @classmethod
    def _check_dry_run(cls, value: object) -> object:
        """Refuse anything but true, false or null."""
        if value is not None and not isinstance(value, bool):
            raise ValueError("Must be true or false.")
        return value


def _issues(error: ValidationError) -> list[FieldIssue]:
    """The problems of a refused body in the API's words, without echoing any input."""
    found: list[FieldIssue] = []
    for item in error.errors(include_input=False, include_url=False)[:MAX_ISSUES]:
        field = str(item["loc"][0]) if item["loc"] else BODY_FIELD
        if item["type"] == "extra_forbidden":
            message = FORCE_MESSAGE if field == FORCE_FIELD else "This field is not accepted."
        else:
            message = item["msg"].removeprefix(_VALUE_ERROR_PREFIX)
        found.append(FieldIssue(field=field, message=message))
    return found


def parse_deploy_body(raw: bytes | None) -> DeployBody:
    """The validated deploy body. An empty body is the same as ``{}``.

    ``raw`` is None when the body was larger than the limit. Raises ``InvalidBody``.
    """
    if raw is None:
        raise InvalidBody([FieldIssue(field=BODY_FIELD, message="The body is too large.")])
    document: Any = {}
    if raw.strip():
        try:
            document = json.loads(raw)
        except ValueError, RecursionError:
            raise InvalidBody(
                [FieldIssue(field=BODY_FIELD, message="The body must be valid JSON.")]
            ) from None
    if not isinstance(document, dict):
        raise InvalidBody([FieldIssue(field=BODY_FIELD, message="The body must be a JSON object.")])
    try:
        return DeployBody.model_validate(document)
    except ValidationError as error:
        raise InvalidBody(_issues(error)) from None


class ServiceInfo(BaseModel):
    """The service a run belongs to."""

    id: int
    name: str


class EnvironmentResult(BaseModel):
    """What one run did with one environment.

    ``file_type`` is the type of the file (``env`` or ``cert``). ``message`` is a fixed sentence
    about the outcome; it never holds a value, a path or any file content.
    """

    id: int
    name: str
    file_type: FileType
    version: int | None
    status: DeployResultStatus
    dist: DistOutcome | None
    drift: DriftState | None
    message: str | None


class PostDeployInfo(BaseModel):
    """The post-deploy step of a run: its status, exit code and fixed reason, never its output."""

    status: PostDeployStatus
    exit_code: int | None
    reason: str | None


class RunResult(BaseModel):
    """A deploy run as the API answers it, for a deploy and for the last result alike.

    ``environments`` holds the files inside the token's scope only. A ``post_deploy`` status of
    ``running`` means the command has not ended yet and ``finished_at`` is still empty.
    """

    run_id: int
    service: ServiceInfo
    dry_run: bool
    status: DeployStatus
    started_at: datetime
    finished_at: datetime | None
    environments: list[EnvironmentResult]
    post_deploy: PostDeployInfo
