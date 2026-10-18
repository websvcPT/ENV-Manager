"""The settings-changed notification seam.

Changes to the allowed paths are audited and also announced to the superadmins as a system
notification. Whatever delivers notifications implements ``SettingsNotifier``; the services that
change settings call it after the change is committed and ignore a notifier that fails. The event
carries the area, what happened and who did it, never a secret.
"""

from dataclasses import dataclass
from typing import Protocol

from app.core.log import get_logger

log = get_logger("settings_events")


@dataclass(frozen=True)
class SettingsChanged:
    """A settings change that people should hear about, such as an allowed path added.

    ``area`` names the settings area (``allowed_paths``), ``action`` what happened (``added`` or
    ``removed``), ``subject`` the item it happened to and ``actor`` is the label of who did it.
    """

    area: str
    action: str
    subject: str
    actor: str


class SettingsNotifier(Protocol):
    """Receives settings-changed events."""

    async def settings_changed(self, event: SettingsChanged) -> None:
        """Handle the event; the caller waits for nothing but this call."""
        ...


class NoSettingsNotifier:
    """The notifier used until notifications exist: it does nothing."""

    async def settings_changed(self, event: SettingsChanged) -> None:
        """Ignore the event."""


async def announce(notifier: SettingsNotifier, event: SettingsChanged) -> None:
    """Deliver an event; a notifier that fails is logged and never fails the change."""
    try:
        await notifier.settings_changed(event)
    except Exception as error:
        log.error("settings notification failed: %s", type(error).__name__)
