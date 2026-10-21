"""Sidebar navigation definition and small presentation helpers for the page shell."""

from dataclasses import dataclass

from app.models import UserGroup


@dataclass(frozen=True)
class NavItem:
    """One sidebar entry and the groups that see it."""

    key: str
    label: str
    href: str
    icon: str
    groups: frozenset[UserGroup]


_ALL = frozenset(UserGroup)
_ADMINS = frozenset({UserGroup.ADMIN, UserGroup.SUPERADMIN})
_SUPERADMIN = frozenset({UserGroup.SUPERADMIN})

NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("projects", "Projects", "/projects", "layers", _ALL),
    NavItem("audit", "Audit log", "/audit", "shield", _ADMINS),
    NavItem("users", "Users", "/users", "users", _SUPERADMIN),
    NavItem("settings", "Settings", "/settings", "settings", _SUPERADMIN),
)


def nav_items_for(group: UserGroup) -> list[NavItem]:
    """The sidebar entries a user of the given group sees, in display order."""
    return [item for item in NAV_ITEMS if group in item.groups]


def initials(name: str) -> str:
    """Two uppercase letters for the account avatar."""
    letters = [character for character in name if character.isalnum()]
    return "".join(letters[:2]).upper() or "?"
