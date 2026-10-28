"""A fake identity provider and a sign in harness for the single sign-on tests.

The provider is an in-process httpx transport that serves the discovery document, the signing
keys and the token endpoint, signs its tokens with a key pair made by ``cryptography`` and checks
what the application sends (the client secret and the PKCE verifier), so the real code of the
application runs against it and nothing touches the network.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from joserfc import jwk, jwt
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import User
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult
from app.services.notify_events import NO_EVENT_NOTIFIER, EventNotifier
from app.services.sso import SsoStart, complete, start
from app.services.sso_provider import SsoProvider
from app.services.sso_settings import SsoForm, save_sso, validate_sso

ISSUER = "https://idp.example.test/realms/main"
CLIENT_ID = "env-manager"
CLIENT_SECRET = "client-secret-for-tests-0123456789"
BASE_URL = "http://app.example.test"
REDIRECT_URI = f"{BASE_URL}/login/sso/callback"
MAPPINGS = (("env-admin", "admin"), ("env-viewer", "viewer"))
POLICY = AuthPolicy()
CODE = "authorization-code-of-the-test"
SUBJECT = "subject-0001"
EMAIL = "pat@example.test"


def _encoded(data: bytes) -> str:
    """Base64 without padding, as the parts of a token are written."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@cache
def rsa_key(kid: str) -> jwk.RSAKey:
    """A private RSA signing key with the key id, made once per id."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwk.RSAKey.import_key(pem, {"kid": kid, "use": "sig"})


@cache
def ec_key(kid: str) -> jwk.ECKey:
    """A private P-256 signing key with the key id, made once per id."""
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwk.ECKey.import_key(pem, {"kid": kid, "use": "sig"})


def unsigned_token(claims: dict[str, object]) -> str:
    """A token that says ``alg: none`` and has no signature."""
    header = _encoded(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    return f"{header}.{_encoded(json.dumps(claims).encode())}."


def hmac_token(claims: dict[str, object], secret: bytes, kid: str = "key-1") -> str:
    """A token signed with HS256, as an attacker who knows only the public key would make it."""
    header = _encoded(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    signing_input = f"{header}.{_encoded(json.dumps(claims).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_encoded(signature)}"


@dataclass
class FakeIdp:
    """The provider. Tests change its fields to make it misbehave.

    ``tokens`` is what the token endpoint hands out for the next exchange.
    """

    issuer: str = ISSUER
    keys: list[jwk.Key] = field(default_factory=lambda: [rsa_key("key-1")])
    discovery_issuer: str | None = None
    down: bool = False
    token_status: int = 200
    token_body: dict[str, object] | None = None
    auth_methods: list[str] | None = None
    seen: list[tuple[str, str]] = field(default_factory=list)
    token_requests: list[httpx.Request] = field(default_factory=list)
    id_token: str = ""
    challenge: str = ""
    answers: dict[str, Callable[[], httpx.Response]] = field(default_factory=dict)

    @property
    def jwks_uri(self) -> str:
        """Where the signing keys are served."""
        return f"{self.issuer}/protocol/openid-connect/certs"

    @property
    def token_endpoint(self) -> str:
        """Where a code is traded for tokens."""
        return f"{self.issuer}/protocol/openid-connect/token"

    def count(self, url: str) -> int:
        """How many times a URL was requested."""
        return sum(1 for _, seen in self.seen if seen == url)

    def discovery(self) -> dict[str, object]:
        """The discovery document."""
        document: dict[str, object] = {
            "issuer": self.discovery_issuer or self.issuer,
            "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
            "token_endpoint": self.token_endpoint,
            "jwks_uri": self.jwks_uri,
        }
        if self.auth_methods is not None:
            document["token_endpoint_auth_methods_supported"] = self.auth_methods
        return document

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one request of the application."""
        url = str(request.url)
        self.seen.append((request.method, url))
        if url in self.answers:
            return self.answers[url]()
        if self.down:
            raise httpx.ConnectError("the provider is down")
        if url == f"{self.issuer}/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if url == self.jwks_uri:
            keys = [key.as_dict(private=False) for key in self.keys]
            return httpx.Response(200, json={"keys": keys})
        if url == self.token_endpoint and request.method == "POST":
            return self._token(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        """The token endpoint: checks the client secret and the PKCE verifier like a real one."""
        self.token_requests.append(request)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        basic = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        secret_ok = request.headers.get("Authorization") == basic or (
            form.get("client_id") == CLIENT_ID and form.get("client_secret") == CLIENT_SECRET
        )
        verifier = form.get("code_verifier", "")
        challenge = _encoded(hashlib.sha256(verifier.encode()).digest())
        if (
            not secret_ok
            or form.get("grant_type") != "authorization_code"
            or form.get("code") != CODE
            or form.get("redirect_uri") != REDIRECT_URI
            or challenge != self.challenge
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        body = self.token_body if self.token_body is not None else {"id_token": self.id_token}
        return httpx.Response(self.token_status, json={"token_type": "Bearer", **body})

    def transport(self) -> httpx.MockTransport:
        """The network the application is given."""
        return httpx.MockTransport(self.handler)

    def sign(
        self,
        claims: dict[str, object],
        *,
        key: jwk.Key | None = None,
        alg: str = "RS256",
        kid: str | None = "key-1",
    ) -> str:
        """A token for the claims signed with the key."""
        header: dict[str, str] = {"alg": alg}
        if kid is not None:
            header["kid"] = kid
        return jwt.encode(header, dict(claims), key or self.keys[0])


def good_claims(nonce: str, /, **changes: object) -> dict[str, object]:
    """The claims of a token a provider would issue for a person with the role env-admin."""
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": SUBJECT,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
        "email": EMAIL,
        "email_verified": True,
        "name": "Pat Example",
        "preferred_username": "pat",
        "resource_access": {CLIENT_ID: {"roles": ["env-admin"]}},
    }
    claims.update(changes)
    return {key: value for key, value in claims.items() if value is not None}


@dataclass
class World:
    """A migrated database, the provider and the application's side of the sign in."""

    engine: AsyncEngine
    crypto: CryptoService
    idp: FakeIdp
    provider: SsoProvider

    async def configure(self, **changes: object) -> None:
        """Switch sign in on with the test client, its secret and the role rows, then apply the
        changes to the settings form (``remove_secret``, ``create_users``, ``mappings``...)."""
        form = SsoForm(
            enabled=True,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            roles_claim="",
            account_url="",
            create_users=True,
            link_by_email=True,
            mappings=MAPPINGS,
            client_secret=CLIENT_SECRET,
        )
        values = {**form.__dict__, **changes}
        async with create_session_factory(self.engine)() as db:
            await save_sso(db, self.crypto, validate_sso(SsoForm(**values)), actor=AuditActor.cli())

    async def add_user(self, user: User) -> User:
        """Store an account and return it."""
        async with create_session_factory(self.engine)() as db:
            db.add(user)
            await db.commit()
            return user

    async def begin(self, *, now: datetime | None = None) -> Begun:
        """Start an attempt the way the sign in link does."""
        async with create_session_factory(self.engine)() as db:
            started = await start(db, self.crypto, self.provider, base_url=BASE_URL, now=now)
        query = parse_qs(urlsplit(started.url).query)
        self.idp.challenge = query["code_challenge"][0]
        return Begun(started, query)

    async def finish(
        self,
        begun: Begun,
        *,
        claims: dict[str, object] | None = None,
        changes: dict[str, object] | None = None,
        token: Callable[[dict[str, object]], str] | None = None,
        state: str | None = None,
        binding: str | None = None,
        code: str | None = CODE,
        provider_error: str | None = None,
        now: datetime | None = None,
        notifier: EventNotifier | None = None,
    ) -> LoginResult:
        """Come back from the provider with a token for the claims (the good ones by default)."""
        nonce = begun.query["nonce"][0]
        issued = claims if claims is not None else good_claims(nonce, **(changes or {}))
        self.idp.id_token = token(issued) if token else self.idp.sign(issued)
        async with create_session_factory(self.engine)() as db:
            return await complete(
                db,
                self.crypto,
                self.provider,
                POLICY,
                state=begun.query["state"][0] if state is None else state,
                binding_token=begun.started.binding_token if binding is None else binding,
                code=code,
                provider_error=provider_error,
                base_url=BASE_URL,
                ip_address="203.0.113.9",
                now=now,
                notifier=notifier or NO_EVENT_NOTIFIER,
            )

    async def sign_in(self, **kwargs: Any) -> LoginResult:
        """Start an attempt and finish it at once."""
        return await self.finish(await self.begin(), **kwargs)


@dataclass
class Begun:
    """A started attempt: what the browser was given and the parameters of the redirect."""

    started: SsoStart
    query: dict[str, list[str]]


def build_world(engine: AsyncEngine, crypto: CryptoService) -> World:
    """The application's side of a sign in against a fresh fake provider."""
    idp = FakeIdp()
    return World(engine, crypto, idp, SsoProvider(idp.transport()))
