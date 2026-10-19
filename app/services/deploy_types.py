"""Requests and results of the deploy engine."""

from dataclasses import dataclass, field
from enum import StrEnum

from app.models import DeployResultStatus, DeployStatus, DistOutcome, DriftState
from app.services.dist_check import DistCheck
from app.services.hooks import PostDeployHook, PostDeployResult


class DeployRequestError(Exception):
    """The request names something that cannot be deployed; the message is safe to show.

    An unknown service and a service the caller may not deploy read the same, and so do an
    unknown environment and one outside the caller's scope, so nothing hidden is named.
    """


class DeployRefusal(StrEnum):
    """Why a deploy did not start."""

    LOCKED = "locked"
    LOCK_UNAVAILABLE = "lock_unavailable"


@dataclass(frozen=True)
class DeployRequest:
    """What to deploy.

    ``environment_ids`` is None for every environment of the service the caller may deploy;
    ``versions`` maps an environment id to the version number to write (default: the newest).
    ``backup_drifted`` keeps a copy of a drifted target whatever the drift policy says.
    """

    service_id: int
    environment_ids: tuple[int, ...] | None = None
    versions: dict[int, int] = field(default_factory=dict)
    dry_run: bool = False
    force: bool = False
    backup_drifted: bool = False


@dataclass(frozen=True)
class EnvironmentOutcome:
    """The outcome for one environment: status, message and what the checks found.

    ``target_path`` and ``backup_path`` are relative to the deployment root. ``forced`` is True
    when the force flag overrode a block or halt of this environment. ``check`` is the dist check
    the decision was based on (key names and counts only), None when none ran.
    """

    environment_id: int
    name: str
    target_path: str
    version_id: int | None
    version_number: int | None
    status: DeployResultStatus
    message: str
    dist: DistOutcome | None = None
    drift: DriftState | None = None
    target_hash_before: str | None = None
    written_hash: str | None = None
    backup_path: str | None = None
    forced: bool = False
    check: DistCheck | None = None


@dataclass(frozen=True)
class DeployOutcome:
    """The result of a deploy request.

    ``run_id`` is None when the deploy was refused before it started (``refusal`` says why).
    ``hook`` is the stored post-deploy command the run was made with, so a caller that describes
    the post-deploy step does not read it again; it is not part of any answer to an API client.
    """

    run_id: int | None
    status: DeployStatus
    dry_run: bool
    force: bool
    environments: tuple[EnvironmentOutcome, ...]
    post_deploy: PostDeployResult
    refusal: DeployRefusal | None = None
    message: str | None = None
    hook: PostDeployHook | None = field(default=None, repr=False)
