"""Request dependencies: database session, authenticated user and group checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.display_time import DisplayTime, display_of
from app.core.sessions import SESSION_COOKIE_NAME
from app.models import User, UserGroup, UserSession
from app.services.app_settings import StoredSettings, load_stored_settings
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy, policy_from_settings
from app.services.backup_events import BackupNotifier
from app.services.deploy import DeployEngine
from app.services.docker_cli import DockerCli
from app.services.hook_runner import DockerHookRunner
from app.services.mailer import MailTransport
from app.services.notify_events import FullNotifier
from app.services.permissions import AccessScope, Capability, ItemRef, load_user_scope
from app.services.sessions import resolve_session
from app.services.settings_events import SettingsNotifier
from app.services.shell_banners import load_shell_banners
from app.services.webhook import WebhookTransport


@dataclass(frozen=True)
class AuthContext:
    """The signed-in user and the session that proved it."""

    user: User
    session: UserSession


def audit_actor(request: Request, context: AuthContext) -> AuditActor:
    """The signed-in user as the actor of an audit entry, with the address of the request."""
    return AuditActor.for_user(context.user, client_address(request))


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request."""
    async with request.app.state.session_factory() as db:
        yield db


def get_crypto(request: Request) -> CryptoService:
    """The application crypto service."""
    crypto: CryptoService = request.app.state.crypto
    return crypto


def get_docker_cli(request: Request) -> DockerCli:
    """The application-wide docker CLI on the configured Docker socket."""
    cli: DockerCli = request.app.state.docker_cli
    return cli


def get_deploy_engine(request: Request) -> DeployEngine:
    """A deploy engine on the app's key, lock folder, current docker CLI and notifier.

    Built per request so it uses the docker CLI and the notifier the app holds at that moment.
    """
    state = request.app.state
    return DeployEngine(
        state.crypto,
        state.lock_dir,
        hook_runner=DockerHookRunner(state.docker_cli),
        notifier=state.notifier,
    )


def get_notifier(request: Request) -> FullNotifier:
    """The application's notifier, which every service announces its events to."""
    notifier: FullNotifier = request.app.state.notifier
    return notifier


def get_backup_notifier(request: Request) -> BackupNotifier:
    """The application's backup notifier."""
    notifier: BackupNotifier = request.app.state.backup_notifier
    return notifier


def get_settings_notifier(request: Request) -> SettingsNotifier:
    """The application's settings notifier."""
    notifier: SettingsNotifier = request.app.state.settings_notifier
    return notifier


def get_mail_transport(request: Request) -> MailTransport:
    """The application's mail transport."""
    transport: MailTransport = request.app.state.mail_transport
    return transport


def get_webhook_transport(request: Request) -> WebhookTransport:
    """The application's webhook transport."""
    transport: WebhookTransport = request.app.state.webhook_transport
    return transport


async def get_stored_settings(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> StoredSettings:
    """The typed settings as stored, read once per request.

    Dependencies are cached for the length of a request, so every reader of the settings in one
    request shares this answer. The page templates read the display settings from it.
    """
    stored = await load_stored_settings(db)
    request.state.display = DisplayTime(stored.general.time_zone, stored.general.date_format)
    return stored


def auth_policy_of(request: Request, stored: StoredSettings) -> AuthPolicy:
    """The authentication limits in force: the stored security settings and the startup override."""
    return policy_from_settings(
        stored.security, ip_max_failures_override=request.app.state.login_ip_max_failures
    )


async def get_auth_policy(
    request: Request, stored: Annotated[StoredSettings, Depends(get_stored_settings)]
) -> AuthPolicy:
    """The authentication thresholds in force for this request, from the stored settings."""
    return auth_policy_of(request, stored)


async def require_auth(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
    stored: Annotated[StoredSettings, Depends(get_stored_settings)],
) -> AuthContext:
    """Resolve the session cookie to a signed-in user, or answer 401.

    Every route declares this dependency (directly or through ``require_group``) unless it is on
    the explicit public list; a test fails when a route has neither.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    resolved = await resolve_session(db, token, policy) if token else None
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    context = AuthContext(user=resolved[1], session=resolved[0])
    request.state.auth = context
    if context.user.group is UserGroup.SUPERADMIN and _is_page_request(request):
        request.state.banners = await load_shell_banners(db, display_of(request), stored.smtp)
    return context


def _is_page_request(request: Request) -> bool:
    """Whether the request may render a whole page (the only place banners are shown)."""
    return request.method == "GET" and "HX-Request" not in request.headers


async def resolve_optional_auth(request: Request) -> AuthContext | None:
    """The signed-in user when the request carries a live session, else None.

    Used by error pages for routes that never ran authentication (an unknown path). Any
    failure to look the session up counts as anonymous.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        async with request.app.state.session_factory() as db:
            policy = auth_policy_of(request, await load_stored_settings(db))
            resolved = await resolve_session(db, token, policy)
    except Exception:
        return None
    return AuthContext(user=resolved[1], session=resolved[0]) if resolved else None


def require_group(*groups: UserGroup) -> Callable[..., Awaitable[AuthContext]]:
    """A dependency that admits only users in one of the given groups, else answers 403."""

    async def check(context: Annotated[AuthContext, Depends(require_auth)]) -> AuthContext:
        if context.user.group not in groups:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return context

    return check


async def get_scope(
    context: Annotated[AuthContext, Depends(require_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccessScope:
    """What the signed-in user may reach, loaded fresh so grant changes apply at once."""
    return await load_user_scope(db, context.user)


def require_capability(capability: Capability) -> Callable[..., Awaitable[AuthContext]]:
    """A dependency that admits only callers whose group holds a capability without a target.

    Used for superadmin-only structure changes, user and settings management and similar
    actions. Answers 403 otherwise, after authentication has already proved who is asking.
    """

    async def check(
        context: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
    ) -> AuthContext:
        if not scope.can(capability):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return context

    return check


class ItemAccess[ContextT](Protocol):
    """A dependency factory made by ``item_access``: the capability the route needs, if any."""

    def __call__(self, capability: Capability | None = None) -> Callable[..., Awaitable[ContextT]]:
        """The dependency that loads the item and checks the capability."""


def item_access[ItemT, ContextT](
    id_param: str,
    loader: Callable[[AsyncSession, AccessScope, int], Awaitable[ItemT | None]],
    item_ref: Callable[[ItemT], ItemRef],
    not_found: str,
    context: Callable[[AuthContext, AccessScope, ItemT], ContextT],
) -> ItemAccess[ContextT]:
    """The dependency factory of one kind of structure item (project, deployment, service, file).

    The dependency reads the item's id from the path parameter ``id_param`` (every route uses the
    ``int`` converter) and loads the item for the caller with ``loader``, which answers None for
    an item that does not exist or lies outside the caller's scope: that is a 404 with the
    ``not_found`` text, so the existence of a hidden item is not revealed. Only then is the
    capability checked, on the item ``item_ref`` names, and a group that lacks it gets a 403.
    ``context`` builds what the route receives from the caller, their scope and the item.
    """

    def access(capability: Capability | None = None) -> Callable[..., Awaitable[ContextT]]:
        async def load(
            request: Request,
            auth: Annotated[AuthContext, Depends(require_auth)],
            scope: Annotated[AccessScope, Depends(get_scope)],
            db: Annotated[AsyncSession, Depends(get_db)],
        ) -> ContextT:
            item = await loader(db, scope, int(request.path_params[id_param]))
            if item is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
            if capability is not None and not scope.can(capability, item_ref(item)):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
            return context(auth, scope, item)

        return load

    return access
