"""Why a single sign-on attempt was refused: a reason code and a fixed sentence for the person.

The sentences never carry anything the provider or the person sent. The reason code is what the
audit entry records and what a test or a screen switches on.
"""

from datetime import timedelta

NOT_CONFIGURED = "not_configured"
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_INVALID = "provider_invalid"
ISSUER_MISMATCH = "issuer_mismatch"
STATE_INVALID = "state_invalid"
BINDING_MISMATCH = "binding_mismatch"
PROVIDER_REFUSED = "provider_refused"
TOKEN_EXCHANGE_FAILED = "token_exchange_failed"  # noqa: S105
TOKEN_INVALID = "token_invalid"  # noqa: S105
EMAIL_MISSING = "email_missing"
NO_MATCHING_ROLE = "no_matching_role"
USER_DISABLED = "user_disabled"
CREATION_OFF = "creation_off"
EMAIL_UNVERIFIED = "email_unverified"
LINK_OFF = "link_off"
ALREADY_LINKED = "already_linked"
EMAIL_COLLISION = "email_collision"

MESSAGES: dict[str, str] = {
    NOT_CONFIGURED: "Single sign-on is not set up.",
    PROVIDER_UNAVAILABLE: "The identity provider could not be reached. Try again later.",
    PROVIDER_INVALID: "The identity provider sent an answer that could not be read.",
    ISSUER_MISMATCH: "The identity provider names a different issuer than the one set up.",
    STATE_INVALID: "This sign-in link is no longer valid. Start the sign in again.",
    BINDING_MISMATCH: "This sign in was not started in this browser. Start the sign in again.",
    PROVIDER_REFUSED: "The identity provider did not complete the sign in.",
    TOKEN_EXCHANGE_FAILED: "The identity provider did not accept the sign in. Try again.",
    TOKEN_INVALID: "The identity provider's answer could not be verified.",
    EMAIL_MISSING: "The identity provider did not give a usable email address.",
    NO_MATCHING_ROLE: "Your account at the identity provider has no role that may use ENV Manager.",
    USER_DISABLED: "This account is disabled.",
    CREATION_OFF: "No account exists for you here yet, and accounts are not created at sign in.",
    EMAIL_UNVERIFIED: (
        "An account with your email address exists, but the identity provider has not "
        "verified the address."
    ),
    LINK_OFF: (
        "An account with your email address exists, but it is not linked to the identity provider."
    ),
    ALREADY_LINKED: "The account with your email address is linked to another identity.",
    EMAIL_COLLISION: "Your email address at the identity provider belongs to another account.",
}


class SsoRefusal(Exception):
    """A sign in that cannot go on. ``reason`` is the code and ``message`` the fixed sentence."""

    def __init__(self, reason: str) -> None:
        """Hold the reason code; the sentence is looked up from it."""
        super().__init__(reason)
        self.reason = reason
        self.message = MESSAGES[reason]


class SsoLocked(Exception):
    """The client address is locked out by too many failed sign in attempts.

    ``retry_after`` is how long the lock lasts. Single sign-on refusals count toward the same
    address counter as wrong passwords, so a client cannot flood the sign in routes.
    """

    def __init__(self, retry_after: timedelta) -> None:
        """Hold the time left of the lock."""
        super().__init__("locked")
        self.retry_after = retry_after
