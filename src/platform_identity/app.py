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
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal, cast

import psycopg
from fastapi import FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, Field

from platform_identity.auth import Principal, TokenVerifier, Unauthenticated
from platform_identity.google import InvalidIdToken
from platform_identity.keys import SigningKey

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


def client_address(request: Request) -> str:
    """The address the edge proxy saw: the last ``X-Forwarded-For`` hop, which the client cannot choose."""
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        return hops[-1][:64]
    return request.client.host if request.client else "unknown"


def _token_error(status: int, error: str, description: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "error_description": description},
                         headers={"Cache-Control": "no-store"})


def create_app(*, connect: Callable[[], psycopg.Connection], platform_verifier: TokenVerifier, signing_key: SigningKey,
               issuer: str, google: Any, token_audiences: frozenset[str], decide_mode: str = "shadow",
               clock: Callable[[], float] = time.time) -> FastAPI:
    if decide_mode not in ("shadow", "record"):
        raise ValueError("decide_mode must be shadow or record")
    app = FastAPI(title="platform-identity", version="0.1.0", docs_url=None, redoc_url=None)
    limiter = RateLimiter(TOKEN_REQUESTS_PER_MINUTE)
    jwk_set = {"keys": [signing_key.jwk]}

    def caller(authorization: str | None, channel: str | None) -> Principal:
        try:
            return platform_verifier.verify(authorization, channel)
        except Unauthenticated as exc:
            raise HTTPException(status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}) from exc

    @contextmanager
    def bound(workspace: uuid.UUID) -> Iterator[psycopg.Connection[DictRow]]:
        try:
            conn = cast("psycopg.Connection[DictRow]", connect())
        except psycopg.OperationalError as exc:
            raise HTTPException(status_code=503, detail={"error": "unavailable"}) from exc
        conn.row_factory = dict_row
        try:
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
            yield conn
        finally:
            conn.rollback()
            conn.close()

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
        if row is None:
            # platform.authorize always returns one row; fail closed instead of crashing if it does not.
            raise HTTPException(status_code=503, detail={"error": "unavailable"})
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
        if not limiter.allow(client_address(request)):
            raise _token_error(429, "slow_down", "too many token requests")
        if grant_type != TOKEN_EXCHANGE:
            raise _token_error(400, "unsupported_grant_type", "only token exchange is supported")
        if subject_token_type != ID_TOKEN_TYPE:
            raise _token_error(400, "invalid_request", "subject_token_type must be an id_token")
        if audience not in token_audiences:
            raise _token_error(400, "invalid_target", "audience is not available")
        scopes = scope.split() if scope else []
        if len(scopes) > 32 or any(not PERMISSION.match(s) for s in scopes):
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
    app = create_app(
        connect=lambda: psycopg.connect(config.database_url, connect_timeout=5),
        platform_verifier=TokenVerifier(platform_keys, config.platform_issuer,
                                        max_lifetime=config.platform_max_lifetime),
        signing_key=SigningKey(config.signing_key_pem),
        issuer=config.issuer,
        google=GoogleVerifier(RemoteJwks(GOOGLE_JWKS_URL), config.google_client_ids),
        token_audiences=config.token_audiences,
        decide_mode=config.decide_mode,
    )
    # No proxy_headers: the rate limiter reads the last X-Forwarded-For hop itself (client_address).
    uvicorn.run(app, host=config.host, port=config.port, proxy_headers=False)
