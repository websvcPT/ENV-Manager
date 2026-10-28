"""Talking to an OpenID Connect provider: its metadata, signing keys, code exchange and ID token.

Requests go through an httpx transport that can be replaced, with short timeouts, no redirects
and a bound on the size of an answer. The discovery document and the signing keys are kept in
memory for a while; the keys are fetched again once when a token names a key that is not in them
(a provider rotated its keys). The ID token is checked with ``joserfc`` and only asymmetric
signature algorithms are accepted, so a token signed with ``none`` or an HMAC is refused.

Anything that goes wrong becomes an ``SsoRefusal`` with a fixed sentence: the text of an
exception, the provider's answer and the tokens are never kept, because they may carry secrets.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from authlib.oauth2.auth import ClientAuth
from authlib.oauth2.rfc6749.parameters import prepare_token_request
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet

from app.models.types import utc_now
from app.services.sso_refusal import (
    ISSUER_MISMATCH,
    PROVIDER_INVALID,
    PROVIDER_UNAVAILABLE,
    TOKEN_EXCHANGE_FAILED,
    TOKEN_INVALID,
    SsoRefusal,
)

DISCOVERY_PATH = "/.well-known/openid-configuration"
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_ANSWER_BYTES = 1_000_000
CACHE_TTL = timedelta(minutes=10)
LEEWAY_SECONDS = 60
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
BASIC_AUTH = "client_secret_basic"
POST_AUTH = "client_secret_post"


@dataclass(frozen=True)
class ProviderMetadata:
    """What the discovery document says that a sign in needs."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    auth_method: str


def _endpoint(document: dict[str, object], name: str) -> str:
    """An absolute http or https address from the document, else a refusal."""
    value = document.get(name)
    if not isinstance(value, str):
        raise SsoRefusal(PROVIDER_INVALID)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise SsoRefusal(PROVIDER_INVALID) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SsoRefusal(PROVIDER_INVALID)
    return value


def _auth_method(document: dict[str, object]) -> str:
    """How to send the client secret: in the body only when that is the one method offered."""
    offered = document.get("token_endpoint_auth_methods_supported")
    if isinstance(offered, list) and BASIC_AUTH not in offered and POST_AUTH in offered:
        return POST_AUTH
    return BASIC_AUTH


def _metadata_from(document: dict[str, object], issuer: str) -> ProviderMetadata:
    """The metadata of a discovery document, checking that it is about the configured issuer."""
    if document.get("issuer") != issuer:
        raise SsoRefusal(ISSUER_MISMATCH)
    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=_endpoint(document, "authorization_endpoint"),
        token_endpoint=_endpoint(document, "token_endpoint"),
        jwks_uri=_endpoint(document, "jwks_uri"),
        auth_method=_auth_method(document),
    )


def _key_set_from(document: dict[str, object]) -> KeySet:
    """The asymmetric keys of a JWKS document, else a refusal. Shared secrets are left out."""
    try:
        keys = KeySet.import_key_set(document)  # type: ignore[arg-type]
    except JoseError, KeyError, TypeError, ValueError, AttributeError:
        raise SsoRefusal(PROVIDER_INVALID) from None
    usable = [key for key in keys if key.key_type != "oct"]
    if not usable:
        raise SsoRefusal(PROVIDER_INVALID)
    return KeySet(usable)


def check_claims(
    claims: dict[str, object], *, issuer: str, client_id: str, nonce: str, now: datetime
) -> None:
    """Refuse an ID token that is not for this client, from this issuer, current and bound to
    this sign in.

    The issuer and the audience must match exactly, the expiry and the issue time are checked
    with a minute of leeway, the nonce must be the one of this attempt, and a token with several
    audiences must name this client as its authorized party (a present ``azp`` must always be it).
    """
    registry = jwt.JWTClaimsRegistry(
        now=int(now.timestamp()),
        leeway=LEEWAY_SECONDS,
        iss={"essential": True, "value": issuer},
        aud={"essential": True, "value": client_id},
        sub={"essential": True},
        exp={"essential": True},
        iat={"essential": True},
        nonce={"essential": True, "value": nonce},
    )
    try:
        registry.validate(claims)
    except JoseError:
        raise SsoRefusal(TOKEN_INVALID) from None
    audience = claims["aud"]
    authorized_party = claims.get("azp")
    several = isinstance(audience, list) and len(audience) > 1
    if (several and authorized_party is None) or (
        authorized_party is not None and authorized_party != client_id
    ):
        raise SsoRefusal(TOKEN_INVALID)


class SsoProvider:
    """The connection to the identity provider, with its caches.

    One instance lives with the application (``app.state``), so the caches are shared by every
    request and a test builds its own. ``transport`` replaces the network, ``clock`` is the time
    the caches expire by.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        ttl: timedelta = CACHE_TTL,
    ) -> None:
        """Use the given transport (the real network when None) and cache lifetime."""
        self._transport = transport
        self._clock = clock
        self._ttl = ttl
        self._metadata: dict[str, tuple[datetime, ProviderMetadata]] = {}
        self._keys: dict[str, tuple[datetime, KeySet]] = {}

    async def _send(
        self, method: str, url: str, *, headers: dict[str, str], content: str | None = None
    ) -> tuple[int, bytes]:
        """One request: the status and the answer, never more than ``MAX_ANSWER_BYTES``."""
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport, timeout=REQUEST_TIMEOUT, follow_redirects=False
                ) as client,
                client.stream(method, url, headers=headers, content=content) as response,
            ):
                answer = bytearray()
                async for chunk in response.aiter_bytes():
                    answer += chunk
                    if len(answer) > MAX_ANSWER_BYTES:
                        raise SsoRefusal(PROVIDER_INVALID)
                return response.status_code, bytes(answer)
        except httpx.HTTPError, httpx.InvalidURL, ValueError:
            raise SsoRefusal(PROVIDER_UNAVAILABLE) from None

    async def _document(self, url: str) -> dict[str, object]:
        """A JSON object fetched with GET, else a refusal."""
        status, answer = await self._send("GET", url, headers={"Accept": "application/json"})
        if status != 200:
            raise SsoRefusal(PROVIDER_UNAVAILABLE)
        try:
            document = json.loads(answer)
        except ValueError:
            raise SsoRefusal(PROVIDER_INVALID) from None
        if not isinstance(document, dict):
            raise SsoRefusal(PROVIDER_INVALID)
        return document

    async def metadata(self, issuer: str, *, refresh: bool = False) -> ProviderMetadata:
        """The metadata of the issuer, from the cache while it is fresh unless asked to fetch it
        again.

        The document's own issuer must equal the configured one exactly, so a provider cannot be
        taken for another by serving its address.
        """
        moment = self._clock()
        cached = self._metadata.get(issuer)
        if not refresh and cached is not None and moment - cached[0] < self._ttl:
            return cached[1]
        document = await self._document(f"{issuer.rstrip('/')}{DISCOVERY_PATH}")
        found = _metadata_from(document, issuer)
        self._metadata[issuer] = (moment, found)
        return found

    async def signing_keys(self, metadata: ProviderMetadata, *, refresh: bool = False) -> KeySet:
        """The signing keys of the provider, from the cache while it is fresh unless asked to
        fetch them again."""
        moment = self._clock()
        cached = self._keys.get(metadata.jwks_uri)
        if not refresh and cached is not None and moment - cached[0] < self._ttl:
            return cached[1]
        keys = _key_set_from(await self._document(metadata.jwks_uri))
        self._keys[metadata.jwks_uri] = (moment, keys)
        return keys

    async def exchange_code(
        self,
        metadata: ProviderMetadata,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> str:
        """Trade the authorization code, with the PKCE verifier and the client secret, for the
        ID token the provider issued. Anything else the provider answers is not read."""
        body = prepare_token_request(
            "authorization_code",
            redirect_uri=redirect_uri,
            code=code,
            code_verifier=code_verifier,
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        _, headers, body = ClientAuth(client_id, client_secret, metadata.auth_method).prepare(
            "POST", metadata.token_endpoint, headers, body
        )
        status, answer = await self._send(
            "POST", metadata.token_endpoint, headers=headers, content=body
        )
        try:
            document = json.loads(answer)
        except ValueError:
            document = None
        id_token = document.get("id_token") if isinstance(document, dict) else None
        if status != 200 or not isinstance(id_token, str) or not id_token:
            raise SsoRefusal(TOKEN_EXCHANGE_FAILED)
        return id_token

    async def verify_id_token(
        self,
        metadata: ProviderMetadata,
        id_token: str,
        *,
        client_id: str,
        nonce: str,
        now: datetime,
    ) -> dict[str, object]:
        """The claims of an ID token whose signature, issuer, audience, times and nonce check out.

        A key id that is not in the cached keys fetches them once more before the token is
        refused.
        """
        claims = await self._decoded(metadata, id_token)
        check_claims(claims, issuer=metadata.issuer, client_id=client_id, nonce=nonce, now=now)
        return claims

    async def _decoded(self, metadata: ProviderMetadata, id_token: str) -> dict[str, object]:
        """The claims of a token with a good signature from one of the provider's keys."""
        keys = await self.signing_keys(metadata)
        try:
            try:
                token = jwt.decode(id_token, keys, algorithms=list(ALGORITHMS))
            except InvalidKeyIdError:
                keys = await self.signing_keys(metadata, refresh=True)
                token = jwt.decode(id_token, keys, algorithms=list(ALGORITHMS))
        except JoseError, ValueError, KeyError, TypeError:
            raise SsoRefusal(TOKEN_INVALID) from None
        return dict(token.claims)
