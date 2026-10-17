"""Projects, deployments, services, environments, versions and service hooks."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, enum_values, utc_now


class DistMissingPolicy(StrEnum):
    """What a deploy does when dist keys are missing from the environment."""

    BLOCK = "block"
    WARN = "warn"


class DriftPolicy(StrEnum):
    """What a deploy does when the target changed outside the app."""

    HALT = "halt"
    OVERWRITE = "overwrite"
    BACKUP_OVERWRITE = "backup_overwrite"


class MissingTargetPolicy(StrEnum):
    """What a deploy does when the target file does not exist yet."""

    CREATE = "create"
    HALT = "halt"


class HookType(StrEnum):
    """When a service hook runs. Only post-deploy hooks run today."""

    POST_DEPLOY = "post_deploy"
    PRE_DEPLOY = "pre_deploy"


def _policy_column[PolicyT: StrEnum](policy: type[PolicyT], length: int) -> Enum:
    """Enum column storing plain policy values."""
    return Enum(policy, native_enum=False, length=length, values_callable=enum_values)


class Project(Base):
    """A group of deployments with its own access."""

    __tablename__ = "projects"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class Deployment(Base):
    """One Compose deployment on a host path, with the default deploy policies."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_deployments_project_name"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    root_path: Mapped[str] = mapped_column(String(4096))
    dist_missing_policy: Mapped[DistMissingPolicy] = mapped_column(
        _policy_column(DistMissingPolicy, 10), default=DistMissingPolicy.BLOCK
    )
    drift_policy: Mapped[DriftPolicy] = mapped_column(
        _policy_column(DriftPolicy, 20), default=DriftPolicy.HALT
    )
    missing_target_policy: Mapped[MissingTargetPolicy] = mapped_column(
        _policy_column(MissingTargetPolicy, 10), default=MissingTargetPolicy.CREATE
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class Service(Base):
    """Groups the environments that feed one workload. Deploys run per service."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("deployment_id", "name", name="uq_services_deployment_name"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class ServiceHook(Base):
    """Commands run around a deploy of a service, at most one hook per type."""

    __tablename__ = "service_hooks"
    __table_args__ = (UniqueConstraint("service_id", "type", name="uq_service_hooks_service_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[HookType] = mapped_column(_policy_column(HookType, 20))
    commands: Mapped[str] = mapped_column(Text, default="")
    working_directory: Mapped[str | None] = mapped_column(String(4096), default=None)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    run_only_if_changed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class Environment(Base):
    """One text file under version control with a target path inside its deployment root."""

    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("service_id", "name", name="uq_environments_service_name"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    target_path: Mapped[str] = mapped_column(String(4096))
    dist_path: Mapped[str | None] = mapped_column(String(4096), default=None)
    dist_missing_policy: Mapped[DistMissingPolicy | None] = mapped_column(
        _policy_column(DistMissingPolicy, 10), default=None
    )
    drift_policy: Mapped[DriftPolicy | None] = mapped_column(
        _policy_column(DriftPolicy, 20), default=None
    )
    missing_target_policy: Mapped[MissingTargetPolicy | None] = mapped_column(
        _policy_column(MissingTargetPolicy, 10), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class EnvironmentVersion(Base):
    """Immutable saved content of an environment. Content is stored as ciphertext."""

    __tablename__ = "environment_versions"
    __table_args__ = (
        UniqueConstraint("environment_id", "number", name="uq_environment_versions_env_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    content_encrypted: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(String(500), default=None)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
