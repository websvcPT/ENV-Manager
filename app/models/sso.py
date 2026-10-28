"""Single sign-on: the identity a user is linked to and the sign-ins that are in progress."""

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, utc_now


class UserIdentity(Base):
    """The identity at a provider that a user signs in with.

    A provider identity is its issuer and its subject, which never change for a person, so a
    change of email address at the provider does not break the link. A user has at most one
    identity and an identity belongs to one user.
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    issuer: Mapped[str] = mapped_column(String(2048))
    subject: Mapped[str] = mapped_column(String(255))
    linked_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class SsoLogin(Base):
    """A single sign-on attempt between the redirect to the provider and its callback.

    Single use and short lived. Only hashes of the state and of the token that ties the attempt
    to the browser that started it are stored. The PKCE code verifier is sealed with the master
    key, bound to the state hash of its own row.
    """

    __tablename__ = "sso_logins"

    id: Mapped[int] = mapped_column(primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
