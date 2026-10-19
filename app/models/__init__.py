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
    DistOutcome,
    DriftState,
    PostDeployStatus,
)
from app.models.git_sync import SYNC_STATE_ID, GitSyncState
from app.models.notifications import NotificationStatus, UserSeenAddress
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
    "SYNC_STATE_ID",
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
    "DistOutcome",
    "DriftPolicy",
    "DriftState",
    "Environment",
    "EnvironmentVersion",
    "GitSyncState",
    "GrantLevel",
    "HookType",
    "LoginCounter",
    "LoginCounterKind",
    "MissingTargetPolicy",
    "NotificationStatus",
    "PendingLogin",
    "PostDeployStatus",
    "Project",
    "RestrictionLevel",
    "Service",
    "ServiceHook",
    "User",
    "UserGroup",
    "UserSeenAddress",
    "UserSession",
]
