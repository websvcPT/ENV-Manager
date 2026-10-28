"""The Settings tabs shared by every settings page: their order, names and addresses."""

from typing import Annotated, Any

from fastapi import Depends

from app.services.permissions import Capability
from app.web.dependencies import AuthContext, require_capability

SETTINGS_TABS: tuple[tuple[str, str], ...] = (
    ("paths", "Allowed paths"),
    ("smtp", "SMTP"),
    ("git", "Git"),
    ("backup", "Backup"),
    ("security", "Security"),
    ("sso", "Single sign-on"),
    ("post-deploy", "Post-deploy"),
    ("registries", "Registries"),
    ("notifications", "Notifications"),
    ("general", "General"),
)
TAB_LABELS = dict(SETTINGS_TABS)

ManageSettings = Annotated[AuthContext, Depends(require_capability(Capability.MANAGE_SETTINGS))]


def tab_context(slug: str) -> dict[str, Any]:
    """What the settings layout needs: the open tab, its label and the links of every tab."""
    return {
        "settings_tab": slug,
        "tab_label": TAB_LABELS[slug],
        "tab_items": [{"label": label, "href": f"/settings/{key}"} for key, label in SETTINGS_TABS],
    }
