"""Access grants and API token restrictions that point into the structure tree."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, enum_values, utc_now


class GrantLevel(StrEnum):
    """The tree level a grant is attached to."""

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    ENVIRONMENT = "environment"


class RestrictionLevel(StrEnum):
    """The tree level an API token restriction is attached to."""

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    SERVICE = "service"
    ENVIRONMENT = "environment"


class AccessGrant(Base):
    """Gives a user access to one project, deployment or environment and what is under it."""

    __tablename__ = "access_grants"
    __table_args__ = (
        CheckConstraint(
            "(level = 'project' AND project_id IS NOT NULL"
            " AND deployment_id IS NULL AND environment_id IS NULL)"
            " OR (level = 'deployment' AND deployment_id IS NOT NULL"
            " AND project_id IS NULL AND environment_id IS NULL)"
            " OR (level = 'environment' AND environment_id IS NOT NULL"
            " AND project_id IS NULL AND deployment_id IS NULL)",
            name="target_matches_level",
        ),
        UniqueConstraint("user_id", "project_id", name="uq_access_grants_user_project"),
        UniqueConstraint("user_id", "deployment_id", name="uq_access_grants_user_deployment"),
        UniqueConstraint("user_id", "environment_id", name="uq_access_grants_user_environment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    level: Mapped[GrantLevel] = mapped_column(
        Enum(GrantLevel, native_enum=False, length=20, values_callable=enum_values)
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), default=None, index=True
    )
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), default=None, index=True
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class ApiTokenRestriction(Base):
    """Narrows an API token to one project, deployment, service or set of environments.

    A token without rows is limited only by its owner's access. Several environment rows
    narrow it to that set. Targets are protected from deletion while a restriction points at
    them, because removing the row would widen the token.
    """

    __tablename__ = "api_token_restrictions"
    __table_args__ = (
        CheckConstraint(
            "(level = 'project' AND project_id IS NOT NULL AND deployment_id IS NULL"
            " AND service_id IS NULL AND environment_id IS NULL)"
            " OR (level = 'deployment' AND deployment_id IS NOT NULL AND project_id IS NULL"
            " AND service_id IS NULL AND environment_id IS NULL)"
            " OR (level = 'service' AND service_id IS NOT NULL AND project_id IS NULL"
            " AND deployment_id IS NULL AND environment_id IS NULL)"
            " OR (level = 'environment' AND environment_id IS NOT NULL AND project_id IS NULL"
            " AND deployment_id IS NULL AND service_id IS NULL)",
            name="target_matches_level",
        ),
        UniqueConstraint("token_id", "project_id", name="uq_api_token_restrictions_token_project"),
        UniqueConstraint(
            "token_id", "deployment_id", name="uq_api_token_restrictions_token_deployment"
        ),
        UniqueConstraint("token_id", "service_id", name="uq_api_token_restrictions_token_service"),
        UniqueConstraint(
            "token_id", "environment_id", name="uq_api_token_restrictions_token_environment"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[RestrictionLevel] = mapped_column(
        Enum(RestrictionLevel, native_enum=False, length=20, values_callable=enum_values)
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), default=None, index=True
    )
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="RESTRICT"), default=None, index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), default=None, index=True
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
