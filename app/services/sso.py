"""Single sign-on with an OpenID Connect provider: starting a sign in and finishing it.

``start`` stores the attempt and returns the address to send the browser to, with the token to
keep in a cookie of that browser. ``complete`` uses the attempt up, trades the code for an ID
token, checks it, finds or creates the account and starts the session through the same step a
password sign in ends with. Two-factor of ENV Manager is not asked on this path: the provider is
the one that checks the person.

Accounts follow the provider and the settings:

* an identity (issuer and subject) already linked to an account signs that account in;
* otherwise an existing account with the same email is linked, only when the provider says the
  email is verified and linking is on;
* otherwise an account is created, when creating is on, with no password and no grants;
* the group comes from the first mapping row whose role the person has, at every sign in. The
  superadmin group is never set or removed here, but a superadmin still needs a matching row;
* a disabled account, a missing role and an email that belongs to another account are refused.

Every refusal is a reason code and a fixed sentence (``SsoRefusal``) and is audited as a failed
sign in without tokens, codes or claims other than the email address.
"""

from dataclasses import dataclass
from datetime import datetime

from authlib.oauth2.client import OAuth2Client
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.models import AuditResult
from app.models.types import utc_now
from app.services.app_settings import (
    SSO_CLIENT_SECRET_KEY,
    SsoSettings,
    has_secret,
    load_stored_settings,
    read_secret,
)
from app.services.audit import AuditActor, record_actor_audit
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult, finish_login
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    UserChange,
    UserChanged,
    announce_event,
)
from app.services.sso_accounts import SSO_ACTOR, Attempt, sign_in_account
from app.services.sso_claims import SsoClaims, read_claims
from app.services.sso_logins import UsedSsoLogin, consume_sso_login, create_sso_login
from app.services.sso_provider import ProviderMetadata, SsoProvider
from app.services.sso_refusal import NOT_CONFIGURED, PROVIDER_REFUSED, SsoRefusal

SCOPE = "openid profile email"
CALLBACK_PATH = "/login/sso/callback"


@dataclass(frozen=True)
class SsoStart:
    """Where to send the browser, and the token to keep in a cookie of that browser."""

    url: str
    binding_token: str


def redirect_uri(base_url: str) -> str:
    """The address the provider sends the browser back to."""
    return f"{base_url.rstrip('/')}{CALLBACK_PATH}"


async def _require_ready(db: AsyncSession, settings: SsoSettings) -> None:
    """Refuse unless sign in is switched on and set up, secret included."""
    if not settings.configured or not await has_secret(db, SSO_CLIENT_SECRET_KEY):
        raise SsoRefusal(NOT_CONFIGURED)


async def _audit_refusal(
    db: AsyncSession, refusal: SsoRefusal, attempt: Attempt, ip_address: str | None
) -> None:
    """Audit and commit a refused sign in: the reason and the email, nothing the provider sent."""
    details: dict[str, object] = {"method": "sso", "reason": refusal.reason}
    if attempt.email:
        details["email"] = attempt.email
    await record_actor_audit(
        db,
        AuditActor.for_sign_in(attempt.user, ip_address),
        action="login.failure",
        result=AuditResult.FAILURE,
        details=details,
    )
    await db.commit()


async def start(
    db: AsyncSession,
    crypto: CryptoService,
    provider: SsoProvider,
    *,
    base_url: str,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> SsoStart:
    """Store a new attempt and return the provider address to send the browser to.

    The address asks for the code flow with PKCE (S256), a nonce and a state, all of this
    attempt. Raises ``SsoRefusal`` when sign in is not set up or the provider cannot be read.
    """
    try:
        settings = (await load_stored_settings(db)).sso
        await _require_ready(db, settings)
        metadata = await provider.metadata(settings.issuer)
    except SsoRefusal as refusal:
        await _audit_refusal(db, refusal, Attempt(), ip_address)
        raise
    login = await create_sso_login(db, crypto, now=now)
    await db.commit()
    client = OAuth2Client(
        None,
        client_id=settings.client_id,
        scope=SCOPE,
        redirect_uri=redirect_uri(base_url),
        code_challenge_method="S256",
    )
    url, _ = client.create_authorization_url(
        metadata.authorization_endpoint,
        state=login.state,
        code_verifier=login.verifier,
        nonce=login.nonce,
    )
    return SsoStart(url, login.binding_token)


async def complete(
    db: AsyncSession,
    crypto: CryptoService,
    provider: SsoProvider,
    policy: AuthPolicy,
    *,
    state: str,
    binding_token: str,
    code: str | None,
    provider_error: str | None = None,
    base_url: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
    now: datetime | None = None,
) -> LoginResult:
    """Finish a sign in and start the session, or raise ``SsoRefusal`` after auditing it.

    ``state`` and ``code`` come from the address the provider sent the browser back to,
    ``provider_error`` is its ``error`` parameter when it refused, and ``binding_token`` is the
    cookie of the browser that started the attempt.
    """
    moment = now or utc_now()
    attempt = Attempt()
    try:
        used = await consume_sso_login(
            db, crypto, state=state, binding_token=binding_token, now=moment
        )
        if provider_error or not code:
            raise SsoRefusal(PROVIDER_REFUSED)
        settings = (await load_stored_settings(db)).sso
        await _require_ready(db, settings)
        metadata = await provider.metadata(settings.issuer)
        claims = await _claims_of(
            db, crypto, provider, settings, metadata, code, used, base_url, moment
        )
        attempt.email = claims.email
        user, created = await sign_in_account(
            db, settings, metadata.issuer, claims, attempt, moment
        )
    except SsoRefusal as refusal:
        await _audit_refusal(db, refusal, attempt, ip_address)
        raise
    result = await finish_login(
        db,
        crypto,
        policy,
        user,
        ip_address,
        user_agent,
        user.email.lower(),
        moment,
        notifier,
        details={"method": "sso"},
    )
    if created:
        await announce_event(
            notifier, UserChanged(UserChange.CREATED, user.name, SSO_ACTOR, user.group.value)
        )
    return result


async def _claims_of(
    db: AsyncSession,
    crypto: CryptoService,
    provider: SsoProvider,
    settings: SsoSettings,
    metadata: ProviderMetadata,
    code: str,
    used: UsedSsoLogin,
    base_url: str,
    now: datetime,
) -> SsoClaims:
    """Trade the code for the ID token and read the checked claims."""
    try:
        secret = await read_secret(db, crypto, SSO_CLIENT_SECRET_KEY)
    except CryptoError:
        raise SsoRefusal(NOT_CONFIGURED) from None
    if not secret:
        raise SsoRefusal(NOT_CONFIGURED)
    id_token = await provider.exchange_code(
        metadata,
        client_id=settings.client_id,
        client_secret=secret,
        code=code,
        redirect_uri=redirect_uri(base_url),
        code_verifier=used.verifier,
    )
    checked = await provider.verify_id_token(
        metadata, id_token, client_id=settings.client_id, nonce=used.nonce, now=now
    )
    return read_claims(checked, settings)
