"""Tenant lifecycle (SPEC-006): sign-up per product, invitations, members, workspace status, installations.

The rules live in platform-empire migration 0009 (``platform.tenancy_*`` SECURITY DEFINER functions); this
module verifies who is calling, fills every caller identity and workspace argument from a verified token or a
verified Google id_token (never from the request body), keeps secrets out of the database (HMACs only) and maps
the functions' error codes to HTTP answers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Annotated, Any

import jwt
import psycopg
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from platform_identity.google import InvalidIdToken, Person
from platform_identity.keys import SigningKey
from platform_identity.rate_limit import RateLimitUnavailable
from platform_identity.service_clients import (
    ASSERTION_LEEWAY_SECONDS,
    ASSERTION_TYPE,
    InvalidClient,
    ReplayCache,
    ServiceClient,
    verify_assertion,
)

log = logging.getLogger("platform_identity.tenancy")

IDENTITY_AUDIENCE = "identity-api"
SIGNUP_MODES = ("closed", "invite_code", "open")
INVITATION_TTL_SECONDS = 7 * 86400
SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
PRODUCT = r"^[a-z][a-z0-9-]{1,31}$"
ROLE = r"^[a-z][a-z0-9_-]{0,62}$"
OWN_TOKEN_LEEWAY_SECONDS = 30
# Module level: with postponed annotations FastAPI resolves route annotations from module globals.
Auth = Annotated[str | None, Header()]

# The only platform functions this module calls (migration 0009).
FUNCTIONS = frozenset({
    "tenancy_signup", "tenancy_my_workspaces", "tenancy_invite", "tenancy_list_invitations",
    "tenancy_revoke_invitation", "tenancy_accept", "tenancy_list_members", "tenancy_set_member",
    "tenancy_remove_member", "tenancy_update_workspace", "tenancy_installations", "tenancy_install",
})

# Function error code → (HTTP status, OAuth-style error). Anything else is a 503.
ERRORS: dict[str, tuple[int, str]] = {
    "slug_taken": (409, "slug_taken"),
    "workspace_limit": (409, "workspace_limit"),
    "last_owner": (409, "last_owner"),
    "workspace_closed": (409, "workspace_closed"),
    "forbidden": (403, "forbidden"),
    "insufficient_privilege": (403, "insufficient_privilege"),
    "not_found": (404, "not_found"),
    "invalid_grant": (400, "invalid_grant"),
    "invalid_role": (400, "invalid_role"),
    "invalid_roles": (400, "invalid_role"),
    "invalid_status": (400, "invalid_request"),
    "invalid_request": (400, "invalid_request"),
    "installation_fixed": (400, "installation_fixed"),
    "invalid_client": (400, "invalid_client"),
}


@dataclass(frozen=True)
class TenancyConfig:
    invite_key: bytes = field(repr=False)
    invite_key_previous: bytes | None = field(default=None, repr=False)
    signup_products: frozenset[str] = frozenset()  # empty = sign-up off (kill switch)
    signup_mode: str = "closed"
    signup_code_hashes: frozenset[str] = frozenset()  # SHA-256 hex of each invite code (mode invite_code)
    signup_per_hour: int = 20
    max_owned_workspaces: int = 3
    role_templates: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)  # product → role → perms
    default_installs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # product → client ids

    def __post_init__(self) -> None:
        if len(self.invite_key) < 32:
            raise ValueError("the invite key must be at least 32 bytes")
        if self.signup_mode not in SIGNUP_MODES:
            raise ValueError("signup mode must be closed, invite_code or open")
        if not 1 <= self.signup_per_hour <= 10_000 or not 1 <= self.max_owned_workspaces <= 100:
            raise ValueError("signup limits out of range")
        for product in self.signup_products:
            roles = self.role_templates.get(product)
            if not roles or "owner" not in roles:
                raise ValueError(f"product {product} needs role templates with an owner role")


def _mac(key: bytes, kind: str, value: str) -> str:
    return kind + ":" + hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


class HourlyLimit:
    """Global sign-up budget per clock hour; per process (one identity-api replica)."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.time) -> None:
        self.limit, self._clock = limit, clock
        self._window, self._count = -1, 0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            window = int(self._clock() // 3600)
            if window != self._window:
                self._window, self._count = window, 0
            if self._count >= self.limit:
                return False
            self._count += 1
            return True


class SignupBody(BaseModel):
    id_token: str = Field(min_length=1, max_length=8192)
    product: str = Field(pattern=PRODUCT)
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=200)


class IdTokenBody(BaseModel):
    id_token: str = Field(min_length=1, max_length=8192)


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    role: str = Field(pattern=ROLE + "|^installation:[a-z][a-z0-9-]{2,62}$")


class AcceptBody(BaseModel):
    id_token: str = Field(min_length=1, max_length=8192)
    code: str = Field(min_length=1, max_length=200)


class MemberPatch(BaseModel):
    roles: list[str] | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, pattern="^(active|suspended)$")


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = Field(default=None, pattern="^(active|suspended|closed)$")


class InstallBody(BaseModel):
    client_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")


@dataclass(frozen=True)
class Caller:
    identity: uuid.UUID
    workspace: uuid.UUID
    scopes: frozenset[str] | None


def _error(status: int, error: str, description: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "error_description": description or error},
                         headers={"Cache-Control": "no-store"})


def email_hint(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:2]}…@{domain[:1]}…"


def installation_client(client: ServiceClient, scope_permissions: Mapping[str, str]) -> dict[str, Any]:
    """The installation template of a client: its registered scopes as canonical permissions."""
    perms = sorted({scope_permissions.get(s, s) for s in client.scopes})
    return {"subject": str(client.subject), "client_id": client.client_id, "display_name": client.client_id,
            "permissions": perms}


def register(app: FastAPI, *, config: TenancyConfig, connection: Callable[[], AbstractContextManager[Any]],
             google: Any, signing_key: SigningKey, issuer: str, clients: Mapping[str, ServiceClient],
             scope_permissions: Mapping[str, str], limiter: Any, address: Callable[[Request], str],
             replays: ReplayCache, assertion_audiences: list[str], clock: Callable[[], float] = time.time) -> None:
    public_key = _public_key(signing_key)
    signups = HourlyLimit(config.signup_per_hour, clock)
    keys = [config.invite_key] + ([config.invite_key_previous] if config.invite_key_previous else [])

    def admit(request: Request) -> None:
        try:
            allowed = limiter.allow(address(request))
        except RateLimitUnavailable:
            raise _error(503, "temporarily_unavailable") from None
        if not allowed:
            raise _error(429, "slow_down", "too many requests")

    def call(fn: str, *args: Any) -> Any:
        """Run one tenancy function as identity_api and commit; its error code becomes the HTTP answer."""
        if fn not in FUNCTIONS:
            raise ValueError(f"unknown tenancy function {fn}")
        query = sql.SQL("SELECT {}({}) AS result").format(
            sql.Identifier("platform", fn), sql.SQL(", ").join(sql.Placeholder() * len(args)))
        try:
            with connection() as conn:
                conn.row_factory = dict_row
                try:
                    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- composed with psycopg sql.Identifier from the FUNCTIONS allowlist; values are bound parameters
                    row = conn.execute(query, args).fetchone()
                    conn.commit()
                except psycopg.Error as exc:
                    conn.rollback()
                    code = getattr(exc.diag, "message_primary", None) or ""
                    if code in ERRORS:
                        status, error = ERRORS[code]
                        raise _error(status, error) from None
                    log.warning("tenancy function failed fn=%s sqlstate=%s", fn, exc.sqlstate)
                    raise _error(503, "temporarily_unavailable") from None
        except psycopg.Error:
            raise _error(503, "temporarily_unavailable") from None
        return row["result"] if row else None

    def person(id_token: str) -> Person:
        try:
            return google.person(id_token)
        except InvalidIdToken:
            raise _error(400, "invalid_grant", "the id_token is not valid") from None

    def caller(authorization: str | None, permission: str) -> Caller:
        """An access token this identity issued for identity-api, from a person (not a delegated chain)."""
        if not authorization or not authorization.startswith("Bearer "):
            raise _error(401, "invalid_token")
        try:
            claims = jwt.decode(authorization[7:], public_key, algorithms=["RS256"], issuer=issuer,
                                audience=IDENTITY_AUDIENCE, leeway=OWN_TOKEN_LEEWAY_SECONDS,
                                options={"require": ["exp", "iat", "sub", "ws", "jti"]})
            identity, workspace = uuid.UUID(claims["sub"]), uuid.UUID(claims["ws"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise _error(401, "invalid_token") from None
        if "act" in claims or "via" in claims or "client_id" in claims:
            raise _error(403, "forbidden", "delegated and service tokens cannot administer a workspace")
        scopes = frozenset(str(claims["scope"]).split()) if claims.get("scope") else None
        if scopes is not None and permission not in scopes:
            raise _error(403, "forbidden", "scope_insufficient")
        return Caller(identity, workspace, scopes)

    # --- sign-up and my workspaces ---------------------------------------------------------------------

    @app.post("/v1/signup", tags=["tenancy"])
    def signup(body: SignupBody, request: Request) -> JSONResponse:
        admit(request)
        if body.product not in config.signup_products or config.signup_mode == "closed":
            raise _error(403, "signup_closed")
        if config.signup_mode == "invite_code":
            digest = hashlib.sha256((body.code or "").encode()).hexdigest()
            if not any(hmac.compare_digest(digest, h) for h in config.signup_code_hashes):
                raise _error(403, "signup_closed")
        who = person(body.id_token)
        if who.email is None:
            raise _error(400, "invalid_grant", "a verified email is required")
        if not signups.allow():
            raise _error(429, "slow_down", "sign-up is busy, try again later")
        installs = [installation_client(clients[c], scope_permissions)
                    for c in config.default_installs.get(body.product, ()) if c in clients]
        roles = {name: list(perms) for name, perms in config.role_templates[body.product].items()}
        # The HMAC under every live key, current first, so a key rotation keeps sign-up idempotent.
        out = call("tenancy_signup", who.subject, [_mac(k, "subject", who.subject) for k in keys], who.name,
                   body.product, body.slug, body.name, config.max_owned_workspaces, Jsonb(roles), Jsonb(installs))
        log.info("signup product=%s created=%s", body.product, out["created"])
        return JSONResponse(out, status_code=201 if out["created"] else 200, headers={"Cache-Control": "no-store"})

    @app.post("/v1/me/workspaces", tags=["tenancy"])
    def my_workspaces(body: IdTokenBody, request: Request) -> JSONResponse:
        admit(request)
        who = person(body.id_token)
        return JSONResponse({"workspaces": call("tenancy_my_workspaces", who.subject)},
                            headers={"Cache-Control": "no-store"})

    # --- invitations ------------------------------------------------------------------------------------

    @app.post("/v1/invitations", tags=["tenancy"])
    def create_invitation(body: InviteBody, request: Request, authorization: Auth = None) -> JSONResponse:
        admit(request)
        who = caller(authorization, "identity.member:admin")
        email = body.email.strip().lower()
        code = secrets.token_urlsafe(32)
        out = call("tenancy_invite", who.identity, who.workspace, _mac(config.invite_key, "email", email),
                   email_hint(email), _mac(config.invite_key, "code", code), body.role, INVITATION_TTL_SECONDS)
        return JSONResponse({**out, "code": code}, status_code=201, headers={"Cache-Control": "no-store"})

    @app.get("/v1/invitations", tags=["tenancy"])
    def list_invitations(request: Request, authorization: Auth = None) -> JSONResponse:
        admit(request)
        who = caller(authorization, "identity.member:admin")
        return JSONResponse({"invitations": call("tenancy_list_invitations", who.identity, who.workspace)},
                            headers={"Cache-Control": "no-store"})

    @app.delete("/v1/invitations/{invitation_id}", tags=["tenancy"], status_code=204)
    def revoke_invitation(invitation_id: uuid.UUID, request: Request, authorization: Auth = None) -> Response:
        admit(request)
        who = caller(authorization, "identity.member:admin")
        call("tenancy_revoke_invitation", who.identity, who.workspace, invitation_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})  # 204 has no body

    @app.post("/v1/invitations/accept", tags=["tenancy"])
    def accept_invitation(body: AcceptBody, request: Request) -> JSONResponse:
        admit(request)
        who = person(body.id_token)
        # The same work for every outcome: HMACs under every live key, then one function call.
        email = who.email or ""
        codes = [_mac(k, "code", body.code) for k in keys]
        emails = [_mac(k, "email", email) for k in keys] if who.email else ["email:none"]
        return JSONResponse(call("tenancy_accept", who.subject, who.name, codes, emails),
                            headers={"Cache-Control": "no-store"})

    # --- members, workspace, installations -------------------------------------------------------------

    @app.get("/v1/members", tags=["tenancy"])
    def list_members(request: Request, authorization: Auth = None) -> JSONResponse:
        admit(request)
        who = caller(authorization, "identity.member:read")
        return JSONResponse({"members": call("tenancy_list_members", who.identity, who.workspace)},
                            headers={"Cache-Control": "no-store"})

    @app.patch("/v1/members/{identity_id}", tags=["tenancy"])
    def patch_member(identity_id: uuid.UUID, body: MemberPatch, request: Request,
                     authorization: Auth = None) -> JSONResponse:
        admit(request)
        who = caller(authorization, "identity.member:admin")
        if body.roles is None and body.status is None:
            raise _error(400, "invalid_request")
        if body.roles is not None and any(not re.fullmatch(ROLE + "|^installation:.*$", r) for r in body.roles):
            raise _error(400, "invalid_role")
        call("tenancy_set_member", who.identity, who.workspace, identity_id, body.roles, body.status)
        return JSONResponse({"identity_id": str(identity_id)}, headers={"Cache-Control": "no-store"})

    @app.delete("/v1/members/{identity_id}", tags=["tenancy"], status_code=204)
    def remove_member(identity_id: uuid.UUID, request: Request, authorization: Auth = None) -> Response:
        admit(request)
        who = caller(authorization, "identity.member:admin")
        call("tenancy_remove_member", who.identity, who.workspace, identity_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})  # 204 has no body

    @app.patch("/v1/workspace", tags=["tenancy"])
    def patch_workspace(body: WorkspacePatch, request: Request, authorization: Auth = None) -> JSONResponse:
        admit(request)
        who = caller(authorization, "identity.workspace:admin")
        if body.name is None and body.status is None:
            raise _error(400, "invalid_request")
        call("tenancy_update_workspace", who.identity, who.workspace, body.name, body.status)
        return JSONResponse({"workspace_id": str(who.workspace)}, headers={"Cache-Control": "no-store"})

    @app.post("/v1/installations", tags=["tenancy"])
    def install(body: InstallBody, request: Request, authorization: Auth = None) -> JSONResponse:
        admit(request)
        who = caller(authorization, "identity.member:admin")
        client = clients.get(body.client_id)
        if client is None or client.workspace is not None or not client.products:
            raise _error(400, "invalid_client")
        # The function checks the workspace's product against the client's products.
        out = call("tenancy_install", who.identity, who.workspace,
                   Jsonb({**installation_client(client, scope_permissions), "products": sorted(client.products)}))
        return JSONResponse(out, status_code=201 if out["created"] else 200, headers={"Cache-Control": "no-store"})

    @app.post("/v1/service/installations", tags=["tenancy"])
    def installations(
        request: Request,
        client_assertion_type: Annotated[str, Form(max_length=200)],
        client_assertion: Annotated[str, Form(max_length=8192)],
        after: Annotated[str | None, Form(max_length=64)] = None,
        limit: Annotated[int, Form(ge=1, le=500)] = 500,
    ) -> JSONResponse:
        """A "*" client lists the workspaces that installed it; it signs in with its own client assertion."""
        admit(request)
        if client_assertion_type != ASSERTION_TYPE:
            raise _error(400, "invalid_request")
        try:
            client, claims = verify_assertion(client_assertion, dict(clients), assertion_audiences)
        except InvalidClient:
            raise _error(401, "invalid_client") from None
        if client.workspace is not None:
            raise _error(400, "invalid_client", "only multi-workspace clients have installations")
        try:
            cursor = uuid.UUID(after) if after else None
        except ValueError:
            raise _error(400, "invalid_request", "after must be a workspace id") from None
        if not replays.use(client.client_id, claims["jti"], expires_at=claims["exp"] + ASSERTION_LEEWAY_SECONDS):
            raise _error(401, "invalid_client")
        page = call("tenancy_installations", client.subject, cursor, limit)
        return JSONResponse({"workspaces": page, "next": page[-1] if len(page) == limit else None},
                            headers={"Cache-Control": "no-store"})


def own_claims(token: str, signing_key: SigningKey, issuer: str) -> dict[str, Any]:
    """Claims of an access token this identity issued (any audience); raises jwt errors."""
    return jwt.decode(token, _public_key(signing_key), algorithms=["RS256"], issuer=issuer, leeway=OWN_TOKEN_LEEWAY_SECONDS,
                      options={"require": ["exp", "iat", "sub", "ws", "jti", "aud"], "verify_aud": False})


def _public_key(signing_key: SigningKey) -> rsa.RSAPublicKey:
    key = load_pem_private_key(signing_key.private_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):  # SigningKey already refuses anything else
        raise ValueError("the signing key must be an RSA private key")
    return key.public_key()
