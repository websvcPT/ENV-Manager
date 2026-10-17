"""SQLAlchemy models. Importing this package registers every table on the metadata."""

from app.models.access import (
    AccessGrant,
    ApiTokenRestriction,
    GrantLevel,
    RestrictionLevel,
)
from app.models.api_token import ApiToken
from app.models.app_setting import AppSetting
from app.models.audit import AuditActorType, AuditEntry, AuditResult
from app.models.base import Base
from app.models.deploy import (
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    PostDeployStatus,
)
from app.models.operations import AllowedPath, Backup, BackupStatus, BackupTrigger
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
from app.models.user import (
    LoginCounter,
    LoginCounterKind,
    PendingLogin,
    User,
    UserGroup,
    UserSession,
)

__all__ = [
    "AccessGrant",
    "AllowedPath",
    "ApiToken",
    "ApiTokenRestriction",
    "AppSetting",
    "AuditActorType",
    "AuditEntry",
    "AuditResult",
    "Backup",
    "BackupStatus",
    "BackupTrigger",
    "Base",
    "DeployResult",
    "DeployResultStatus",
    "DeployRun",
    "DeployStatus",
    "DeployTrigger",
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
    "PendingLogin",
    "PostDeployStatus",
    "Project",
    "RestrictionLevel",
    "Service",
    "ServiceHook",
    "User",
    "UserGroup",
    "UserSession",
]
