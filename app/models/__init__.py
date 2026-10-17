"""SQLAlchemy models. Importing this package registers every table on the metadata."""

from app.models.access import (
    AccessGrant,
    ApiTokenRestriction,
    GrantLevel,
    RestrictionLevel,
)
from app.models.api_token import ApiToken
from app.models.app_setting import AppSetting
from app.models.base import Base
from app.models.structure import (
    Deployment,
    DistMissingPolicy,
    DriftPolicy,
    Environment,
    EnvironmentVersion,
    HookType,
    MissingTargetPolicy,
    Project,
    Service,
    ServiceHook,
)
from app.models.user import LoginCounter, LoginCounterKind, User, UserGroup, UserSession

__all__ = [
    "AccessGrant",
    "ApiToken",
    "ApiTokenRestriction",
    "AppSetting",
    "Base",
    "Deployment",
    "DistMissingPolicy",
    "DriftPolicy",
    "Environment",
    "EnvironmentVersion",
    "GrantLevel",
    "HookType",
    "LoginCounter",
    "LoginCounterKind",
    "MissingTargetPolicy",
    "Project",
    "RestrictionLevel",
    "Service",
    "ServiceHook",
    "User",
    "UserGroup",
    "UserSession",
]
