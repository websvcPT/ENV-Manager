"""The development mail catcher as the SMTP settings of a development database.

The development compose file runs smtp4dev next to the application. Seeding a development
database points the SMTP settings at it, but only while SMTP is not configured, so settings a
developer has chosen are never replaced.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.app_settings import SmtpSettings, load_stored_settings, save_section
from app.services.audit import AuditActor

DEV_SMTP = SmtpSettings(
    host="smtp4dev", port=25, tls_mode="none", username="", from_address="env-manager@example.com"
)


async def ensure_dev_smtp(db: AsyncSession) -> bool:
    """Store the development mail catcher as the SMTP settings when none are configured.

    Returns whether it stored them. The caller commits.
    """
    if (await load_stored_settings(db)).smtp.configured:
        return False
    await save_section(db, "smtp", DEV_SMTP, actor=AuditActor.cli())
    return True
