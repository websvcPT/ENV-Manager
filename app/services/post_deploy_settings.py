"""The Settings switch for post-deploy commands.

The switch is one stored value. It can only be changed while Docker is usable (socket mounted,
daemon reachable, compose available), because a command can only run then anyway and the screen
must not promise more than the deploy can do. When the switch is off the deploy engine skips a
service's stored command and reports why, and the deploy dialog preview says the same.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.app_settings import PostDeploySettings, SettingChange, save_section
from app.services.audit import AuditActor
from app.services.docker_cli import DockerStatus

UNAVAILABLE_SUFFIX = "Post-deploy commands are off, so this setting cannot be changed."
UNAVAILABLE_FALLBACK = "Docker is not available."


class PostDeploySwitchError(Exception):
    """The switch cannot be changed now; the message is safe to show."""


async def save_post_deploy_switch(
    db: AsyncSession, enabled: bool, *, docker: DockerStatus, actor: AuditActor
) -> list[SettingChange]:
    """Store the switch with an audit entry, and commit. Refused while Docker is unusable."""
    if not docker.available:
        raise PostDeploySwitchError(f"{docker.reason or UNAVAILABLE_FALLBACK} {UNAVAILABLE_SUFFIX}")
    new = PostDeploySettings(enabled=enabled)
    changes = await save_section(db, "post_deploy", new, actor=actor)
    await db.commit()
    return changes
