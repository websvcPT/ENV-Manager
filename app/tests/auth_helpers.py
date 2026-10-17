"""Helpers shared by the authentication tests."""

from app.core.passwords import hash_password
from app.models import User, UserGroup

PASSWORD = "correct horse battery"
PASSWORD_HASH = hash_password(PASSWORD)


def make_user(
    name: str = "alice",
    group: UserGroup = UserGroup.ADMIN,
    *,
    active: bool = True,
    email: str | None = None,
) -> User:
    """A user whose password is ``PASSWORD``."""
    return User(
        name=name,
        email=email or f"{name}@example.test",
        password_hash=PASSWORD_HASH,
        group=group,
        is_active=active,
    )
