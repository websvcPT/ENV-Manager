"""Why an API call was refused, in terms of the API and not of HTTP.

The API layer turns a refusal into a status code and the one JSON error shape. A refusal is an
expected outcome (bad token, missing permission, a busy lock), so error reporting ignores it.
"""

from enum import StrEnum

from app.core.errors import ExpectedOutcome
from app.schemas.api import FieldIssue


class RefusalKind(StrEnum):
    """The ways an API call can be refused."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    DEPLOY_LOCKED = "deploy_locked"
    LOCK_UNAVAILABLE = "lock_unavailable"
    RATE_LIMITED = "rate_limited"


class ApiRefusal(ExpectedOutcome):
    """A refused call: the kind, a message that is safe to send and what the client may fix.

    ``audited`` is True when the entry that records the call was written before the refusal (the
    deploy engine audits the runs it refuses), so the caller must not write a second one.
    ``retry_after`` is the number of seconds a rate limited client has to wait.
    """

    def __init__(
        self,
        kind: RefusalKind,
        message: str,
        *,
        fields: tuple[FieldIssue, ...] = (),
        retry_after: int | None = None,
        audited: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.fields = fields
        self.retry_after = retry_after
        self.audited = audited
