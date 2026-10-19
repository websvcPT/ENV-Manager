"""The notifier the command line uses: it delivers before the command ends.

The command line runs in its own process, so it cannot leave work behind for a background task:
its loop ends with the command. The notifier here sends inline, once, under one time limit, and
never changes what a command does or what it exits with. A finished deploy is also posted to the
webhook, once. It needs the master key to read the SMTP password and the webhook secret; a process
without a usable key sends nothing, as it would when SMTP is not set up.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoError, CryptoService, build_crypto_service
from app.core.db import create_session_factory
from app.core.log import get_logger
from app.services.notifier import Notifications
from app.services.notify_events import NO_EVENT_NOTIFIER, FullNotifier

log = get_logger("cli_notify")


def cli_notifier(db: AsyncSession, crypto: CryptoService | None = None) -> FullNotifier:
    """An inline notifier on the database of the session the command runs on.

    The key is the configured one unless ``crypto`` is given: a command that has just moved the
    data to another key passes the new one, because the SMTP password and webhook secret are
    sealed with it now.
    """
    engine = db.bind
    if not isinstance(engine, AsyncEngine):
        return NO_EVENT_NOTIFIER
    settings = get_settings()
    if crypto is None:
        try:
            crypto = build_crypto_service(settings.key, settings.key_file)
        except CryptoError as error:
            log.warning(
                "notifications are not sent from the command line: %s", type(error).__name__
            )
            return NO_EVENT_NOTIFIER
    return Notifications(
        create_session_factory(engine), crypto, base_url=settings.base_url, inline=True
    )
