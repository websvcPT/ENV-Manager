"""What a sign in reads from a checked ID token: the identity, the name and the roles, and the
group the roles map to."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.models import UserGroup
from app.services.app_settings import SsoSettings
from app.services.sso_refusal import EMAIL_MISSING, TOKEN_INVALID, SsoRefusal
from app.services.users import MAX_NAME_LENGTH, InvalidUserError, clean_name, validate_identity

MAX_SUBJECT_LENGTH = 255
FALLBACK_NAME = "SSO user"


@dataclass(frozen=True)
class SsoClaims:
    """What a sign in needs from the ID token."""

    subject: str
    email: str
    email_verified: bool
    name: str | None
    roles: tuple[str, ...]


def claim_at_path(claims: Mapping[str, object], path: str) -> object | None:
    """The value at a dotted path of the claims, None when there is none.

    A key that holds a dot itself (a client id such as ``my.app``) is matched too: at each level
    the shortest run of path parts that names a key is tried first.
    """
    return _walk(claims, path.split("."))


def _walk(node: object, parts: list[str]) -> object | None:
    """The value below ``node`` at the path parts, trying each way to group parts into keys."""
    if not parts:
        return node
    if not isinstance(node, Mapping):
        return None
    for size in range(1, len(parts) + 1):
        key = ".".join(parts[:size])
        if key in node and (found := _walk(node[key], parts[size:])) is not None:
            return found
    return None


def roles_from(claims: Mapping[str, object], path: str) -> tuple[str, ...]:
    """The roles at the path: a list of texts, else none."""
    value = claim_at_path(claims, path)
    if isinstance(value, list) and all(isinstance(role, str) for role in value):
        return tuple(value)
    return ()


def group_for(roles: tuple[str, ...], mappings: tuple[tuple[str, str], ...]) -> UserGroup | None:
    """The group of the first mapping row whose role the person has, None when no row matches."""
    for role, group in mappings:
        if role in roles:
            return UserGroup(group)
    return None


def _name_from(text: str) -> str | None:
    """A display name from the text, softening characters a name cannot hold, None if empty."""
    try:
        return clean_name(text)
    except InvalidUserError:
        pass
    softened = "".join(char if char.isalnum() or char in " .'-" else " " for char in text)
    try:
        return clean_name(softened[:MAX_NAME_LENGTH])
    except InvalidUserError:
        return None


def _display_name(claims: Mapping[str, object], email: str) -> str | None:
    """The display name: the name claim, else the preferred user name, else the part of the
    email before the at sign. None when none of them makes a name."""
    for candidate in (claims.get("name"), claims.get("preferred_username"), email.split("@")[0]):
        if isinstance(candidate, str) and (name := _name_from(candidate)):
            return name
    return None


def read_claims(claims: Mapping[str, object], settings: SsoSettings) -> SsoClaims:
    """The subject, email, name and roles of a checked ID token, else a refusal."""
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
        raise SsoRefusal(TOKEN_INVALID)
    raw_email = claims.get("email")
    try:
        _, email = validate_identity(FALLBACK_NAME, raw_email if isinstance(raw_email, str) else "")
    except InvalidUserError:
        raise SsoRefusal(EMAIL_MISSING) from None
    return SsoClaims(
        subject=subject,
        email=email,
        email_verified=claims.get("email_verified") is True,
        name=_display_name(claims, email),
        roles=roles_from(claims, settings.effective_roles_claim),
    )
