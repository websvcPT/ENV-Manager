"""Deploy runs and the per-environment results they produce."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, enum_values, utc_now


class DeployTrigger(StrEnum):
    """How a deploy was started."""

    UI = "ui"
    API = "api"
    CLI = "cli"


class DeployStatus(StrEnum):
    """Overall outcome of a deploy run."""

    SUCCESS = "success"
    HALTED = "halted"
    BLOCKED = "blocked"
    FAILED = "failed"
    POST_DEPLOY_FAILED = "post_deploy_failed"


class PostDeployStatus(StrEnum):
    """What happened to the post-deploy command of a run.

    ``RUNNING`` is recorded while the command executes and is replaced by the final outcome.
    """

    RUNNING = "running"
    RAN = "ran"
    SKIPPED = "skipped"
    FAILED = "failed"
    NONE = "none"


class DeployResultStatus(StrEnum):
    """Outcome for one environment of a run."""

    WRITTEN = "written"
    UNCHANGED = "unchanged"
    WOULD_WRITE = "would_write"
    BLOCKED = "blocked"
    HALTED = "halted"
    FAILED = "failed"


class DistOutcome(StrEnum):
    """How a dist check ended: the policy decision for missing keys, or why there is none."""

    BLOCKED = "blocked"
    WARNING = "warning"
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"


class DriftState(StrEnum):
    """How the target file stood against the last content this app wrote to it.

    ``in_sync`` covers an identical file and, for a target this app never wrote, an empty one.
    """

    IN_SYNC = "in_sync"
    DRIFT = "drift"
    MISSING = "missing"


class DeployRun(Base):
    """One deploy of a service. Names are copied so history survives structure deletion."""

    __tablename__ = "deploy_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL"), default=None, index=True
    )
    service_name: Mapped[str] = mapped_column(String(100))
    deployment_name: Mapped[str] = mapped_column(String(100))
    triggered_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    triggered_by_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL"), default=None
    )
    trigger: Mapped[DeployTrigger] = mapped_column(
        Enum(DeployTrigger, native_enum=False, length=10, values_callable=enum_values)
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[DeployStatus] = mapped_column(
        Enum(DeployStatus, native_enum=False, length=20, values_callable=enum_values)
    )
    post_deploy_status: Mapped[PostDeployStatus] = mapped_column(
        Enum(PostDeployStatus, native_enum=False, length=10, values_callable=enum_values),
        default=PostDeployStatus.NONE,
    )
    post_deploy_exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    post_deploy_output: Mapped[str | None] = mapped_column(Text, default=None)
    post_deploy_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    audit_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_entries.id", ondelete="SET NULL"), default=None
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class DeployResult(Base):
    """The outcome for one environment within a run, with the hashes needed for drift checks.

    ``target_path`` and ``backup_path`` are relative to the deployment root, so a result never
    names the root. ``written_hash`` is the keyed hash of the content this app wrote to the target
    or found there identical to the version; it is the baseline of the next drift check.
    ``dist_outcome`` and ``drift_state`` are what the checks of this run found, None when a check
    did not run.
    """

    __tablename__ = "deploy_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("deploy_runs.id", ondelete="CASCADE"), index=True
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"), default=None, index=True
    )
    environment_name: Mapped[str] = mapped_column(String(100))
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("environment_versions.id", ondelete="SET NULL"), default=None
    )
    target_path: Mapped[str] = mapped_column(String(4096))
    status: Mapped[DeployResultStatus] = mapped_column(
        Enum(DeployResultStatus, native_enum=False, length=15, values_callable=enum_values)
    )
    message: Mapped[str | None] = mapped_column(String(500), default=None)
    target_hash_before: Mapped[str | None] = mapped_column(String(64), default=None)
    written_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    backup_path: Mapped[str | None] = mapped_column(String(4096), default=None)
    dist_outcome: Mapped[DistOutcome | None] = mapped_column(
        Enum(DistOutcome, native_enum=False, length=10, values_callable=enum_values),
        default=None,
    )
    drift_state: Mapped[DriftState | None] = mapped_column(
        Enum(DriftState, native_enum=False, length=10, values_callable=enum_values),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
