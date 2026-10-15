"""platform-identity HTTP service (migration step 3: shadow).

- ``GET /.well-known/jwks.json`` and ``/.well-known/openid-configuration``:
  this service's own verification key. No cell trusts it yet, so tokens it
  issues cannot reach any data until the cutover in step 6.
- ``POST /v1/authz/decide``: the same body, rules (``platform.authorize``) and
  answers as the platform endpoint. In ``shadow`` mode the decision is made in
  a transaction that is rolled back, so nothing is recorded twice; the
  platform mirrors its own answered requests here and logs any difference.
- ``POST /v1/token``: RFC 8693 token exchange of a Google id_token for an
  access token bound to one audience and one workspace.
"""

from __future__ import annotations

import logging
import ipaddress
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, asynccontextmanager
from typing import Annotated, Any, Literal

import psycopg
from fastapi import FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from platform_identity.auth import Principal, TokenVerifier, Unauthenticated
from platform_identity.google import InvalidIdToken
from platform_identity.keys import SigningKey
from platform_identity.rate_limit import RateLimitUnavailable
from platform_identity.config import validate_shared_scope_permissions

log = logging.getLogger("platform_identity")

PERMISSION = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)$")
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
ACCESS_TOKEN_TTL_SECONDS = 600
TOKEN_REQUESTS_PER_MINUTE = 30
Auth = Annotated[str | None, Header()]
Channel = Annotated[str | None, Header(alias="x-empire-channel")]


class Decide(BaseModel):
    action: str = Field(pattern=PERMISSION.pattern)
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=200)
    approval_id: uuid.UUID | None = None


class Decision(BaseModel):
    decision: Literal["allow", "deny", "require_approval"]
    reason: str
    decision_id: uuid.UUID | None


class RateLimiter:
    """Fixed one-minute windows per key; in memory, so per instance."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._windows: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        window = int(self._clock() // 60)
        with self._lock:
            if len(self._windows) > 10_000:
                self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
                if len(self._windows) > 10_000:  # still full within one minute: start the window over
                    self._windows.clear()
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            if count >= self.limit:
                return False
            self._windows[key] = (start, count + 1)
            return True


def client_address(request: Request, *, trusted_proxies: tuple[str, ...] | None = None) -> str:
    """Explicit staged proxy trust; None preserves the legacy local profile."""
    forwarded = request.headers.get("x-forwarded-for", "")
    peer = request.client.host if request.client else "unknown"
    if trusted_proxies is None:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        return hops[-1][:64] if hops else peer
    networks = tuple(ipaddress.ip_network(network) for network in trusted_proxies)
    try:
        address = ipaddress.ip_address(peer)
        if not any(address in network for network in networks) or len(forwarded) > 2048:
            return peer
        hops = [hop.strip() for hop in forwarded.split(',')]
        if not forwarded or len(hops) > 32:
            return peer
        addresses = [ipaddress.ip_address(hop) for hop in hops]
        for address in reversed(addresses):
            if not any(address in network for network in networks):
                return str(address)
        return str(addresses[0])
    except ValueError:
        return peer


def _token_error(status: int, error: str, description: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "error_description": description},
                         headers={"Cache-Control": "no-store"})


def create_app(*, connect: Callable[[], psycopg.Connection] | None = None, platform_verifier: TokenVerifier, signing_key: SigningKey,
               issuer: str, google: Any, token_audiences: frozenset[str], decide_mode: str = "shadow",
               clock: Callable[[], float] = time.time, rate_limiter: Any = None,
               trusted_proxies: tuple[str, ...] | None = None, connection_pool: Any = None,
               shared_scope_permissions: Mapping[str,str] | None = None) -> FastAPI:
    if decide_mode not in ("shadow", "record"):
        raise ValueError("decide_mode must be shadow or record")
    if connect is None and connection_pool is None:
        raise ValueError('a database connection source is required')
    if trusted_proxies is not None:
        if len(trusted_proxies) > 32 or any(ipaddress.ip_network(p).prefixlen == 0 for p in trusted_proxies):
            raise ValueError('explicit non-wildcard trusted proxy networks required')
    scope_permissions = validate_shared_scope_permissions(shared_scope_permissions if shared_scope_permissions is not None else {})
    limiter = rate_limiter if rate_limiter is not None else RateLimiter(TOKEN_REQUESTS_PER_MINUTE)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if hasattr(limiter,'close'):
                limiter.close()
            if connection_pool is not None:
                connection_pool.close()

    app = FastAPI(title="platform-identity", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)

    @contextmanager
    def connection():
        if connection_pool is not None:
            with connection_pool.connection() as conn:
                yield conn
        else:
            conn = connect()
            try:
                yield conn
            finally:
                conn.close()
    jwk_set = {"keys": [signing_key.jwk]}

    def caller(authorization: str | None, channel: str | None) -> Principal:
        try:
            return platform_verifier.verify(authorization, channel)
        except Unauthenticated as exc:
            raise HTTPException(status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}) from exc

    @contextmanager
    def bound(workspace: uuid.UUID) -> Iterator[psycopg.Connection]:
        try:
            with connection() as conn:
                conn.row_factory = dict_row
                try:
                    conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
                    yield conn
                finally:
                    conn.rollback()
        except psycopg.Error:
            raise HTTPException(status_code=503, detail={"error": "unavailable"}) from None

    @app.get('/readyz', include_in_schema=False)
    def readyz():
        try:
            with connection() as conn:
                conn.execute('SELECT 1')
                conn.rollback()
            if hasattr(limiter,'ready') and limiter.ready() is not True:
                raise RateLimitUnavailable('admission unavailable')
            return JSONResponse({'status':'ready'})
        except Exception:
            return JSONResponse({'status':'unavailable'},status_code=503)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "platform-identity", "decide_mode": decide_mode}

    @app.get("/.well-known/jwks.json", include_in_schema=False)
    def jwks() -> JSONResponse:
        return JSONResponse(jwk_set, headers={"Cache-Control": "public, max-age=300"})

    @app.get("/.well-known/openid-configuration", include_in_schema=False)
    def openid_configuration() -> JSONResponse:
        return JSONResponse({
            "issuer": issuer,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "token_endpoint": f"{issuer}/v1/token",
            "grant_types_supported": [TOKEN_EXCHANGE],
            "token_endpoint_auth_methods_supported": ["none"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }, headers={"Cache-Control": "public, max-age=300"})

    @app.post("/v1/authz/decide", response_model=Decision, tags=["authz"])
    def decide(body: Decide, response: Response, authorization: Auth = None,
               x_empire_channel: Channel = None) -> Decision:
        principal = caller(authorization, x_empire_channel)
        if principal.scopes is not None and body.action not in principal.scopes:
            raise HTTPException(status_code=403, detail={"error": "deny", "reason": "scope_insufficient"})
        delegation = principal.delegation
        with bound(principal.workspace) as conn:
            row = conn.execute(
                "SELECT decision, reason, decision_id FROM platform.authorize("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (principal.actor, principal.workspace, body.action, body.resource_type, body.resource_id,
                 principal.workspace, delegation, principal.on_behalf_of if delegation else None, body.approval_id,
                 principal.channel, False),
            ).fetchone()
            if decide_mode == "record":
                conn.commit()
        response.headers["x-identity-decide-mode"] = decide_mode
        return Decision(decision=row["decision"], reason=row["reason"],
                        decision_id=row["decision_id"] if decide_mode == "record" else None)

    @app.post("/v1/token", tags=["token"])
    def token(
        request: Request,
        grant_type: Annotated[str, Form()],
        subject_token: Annotated[str, Form(max_length=8192)],
        subject_token_type: Annotated[str, Form()],
        audience: Annotated[str, Form(max_length=64)],
        workspace: Annotated[uuid.UUID, Form()],
        scope: Annotated[str | None, Form(max_length=2000)] = None,
    ) -> JSONResponse:
        try:
            allowed = limiter.allow(client_address(request,trusted_proxies=trusted_proxies))
        except RateLimitUnavailable:
            raise _token_error(503, 'temporarily_unavailable', 'token admission is unavailable') from None
        if not allowed:
            raise _token_error(429, "slow_down", "too many token requests")
        if grant_type != TOKEN_EXCHANGE:
            raise _token_error(400, "unsupported_grant_type", "only token exchange is supported")
        if subject_token_type != ID_TOKEN_TYPE:
            raise _token_error(400, "invalid_request", "subject_token_type must be an id_token")
        if audience not in token_audiences:
            raise _token_error(400, "invalid_target", "audience is not available")
        scopes = scope.split() if scope else []
        if len(scopes) > 32 or any(not PERMISSION.match(s) and s not in scope_permissions for s in scopes):
            raise _token_error(400, "invalid_scope", "scope must list permissions")
        try:
            external_subject = google.external_subject(subject_token)
        except InvalidIdToken as exc:
            raise _token_error(400, "invalid_grant", "the subject token is not valid") from exc
        with bound(workspace) as conn:
            row = conn.execute(
                "SELECT i.id FROM platform.identities i"
                " JOIN platform.memberships m ON m.identity_id = i.id AND m.workspace_id = %s"
                " WHERE i.external_subject = %s AND i.status = 'active' AND m.status = 'active'",
                (workspace, external_subject),
            ).fetchone()
            if row is not None:
                for shared_scope in sorted(set(scopes) & scope_permissions.keys()):
                    decision = conn.execute(
                        "SELECT decision, reason, decision_id FROM platform.authorize("
                        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (row['id'],workspace,scope_permissions[shared_scope],'shared-infra',shared_scope,
                         workspace,None,None,None,'api',False),
                    ).fetchone()
                    if not decision or decision['decision'] != 'allow':
                        raise _token_error(403,'invalid_scope','requested scope is not authorized')
        if row is None:
            # Same answer for an unknown person and a non-member, so membership does not leak.
            raise _token_error(400, "invalid_grant", "no active membership for this subject")
        now = int(clock())
        claims: dict[str, Any] = {
            "iss": issuer, "aud": audience, "sub": str(row["id"]), "ws": str(workspace),
            "iat": now, "exp": now + ACCESS_TOKEN_TTL_SECONDS, "jti": str(uuid.uuid4()),
        }
        if scopes:
            claims["scope"] = " ".join(sorted(set(scopes)))
        log.info("token issued audience=%s", audience)
        return JSONResponse({
            "access_token": signing_key.sign(claims),
            "issued_token_type": ACCESS_TOKEN_TYPE,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        }, headers={"Cache-Control": "no-store"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # The token endpoint answers in the OAuth shape and never echoes the submitted subject token.
        if request.url.path == "/v1/token":
            return JSONResponse({"error": "invalid_request", "error_description": "missing or malformed parameter"},
                                status_code=400, headers={"Cache-Control": "no-store"})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(HTTPException)
    def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        # Token endpoint errors use the OAuth shape; everything else keeps the platform's {"detail": ...}.
        detail = exc.detail
        if isinstance(detail, dict) and "error_description" in detail:
            return JSONResponse(detail, status_code=exc.status_code, headers=exc.headers)
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    return app


def main() -> None:
    import uvicorn

    from platform_identity.config import Config
    from platform_identity.google import GOOGLE_JWKS_URL, GoogleVerifier
    from platform_identity.keys import RemoteJwks, StaticKey

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    platform_keys = (RemoteJwks(config.platform_jwks_url) if config.platform_jwks_url
                     else StaticKey(config.platform_public_key_pem or ""))
    from platform_identity.rate_limit import PostgresRateLimiter, bounded_pool

    primary_pool = bounded_pool(config.database_url,max_size=config.db_pool_max,
                                timeout=config.db_timeout_seconds,statement_timeout_ms=config.db_statement_timeout_ms)
    limiter = (PostgresRateLimiter(config.rate_limit_database_url,per_key=config.rate_limit_per_key,
                                  global_limit=config.rate_limit_global,pool_max=config.rate_limit_pool_max,
                                  timeout=config.db_timeout_seconds,statement_timeout_ms=config.db_statement_timeout_ms)
               if config.rate_limit_mode == 'postgres' else None)
    app = create_app(
        connection_pool=primary_pool,
        rate_limiter=limiter,
        shared_scope_permissions=config.shared_scope_permissions,
        trusted_proxies=config.trusted_proxies if config.rate_limit_mode == 'postgres' else None,
        platform_verifier=TokenVerifier(platform_keys, config.platform_issuer,
                                        max_lifetime=config.platform_max_lifetime),
        signing_key=SigningKey(config.signing_key_pem),
        issuer=config.issuer,
        google=GoogleVerifier(RemoteJwks(GOOGLE_JWKS_URL), config.google_client_ids),
        token_audiences=config.token_audiences,
        decide_mode=config.decide_mode,
    )
    # Preserve the socket peer; staged mode alone applies explicit proxy trust.
    uvicorn.run(app, host=config.host, port=config.port, proxy_headers=False,
                limit_concurrency=config.max_concurrency,timeout_keep_alive=5)
