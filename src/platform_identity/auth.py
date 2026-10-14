"""Verify platform-issued bearer tokens exactly as platform-empire does today.

The decision endpoint must accept and refuse the same tokens as the endpoint
it replaces (``tasks_api.auth.TokenVerifier`` with audience ``platform-api``),
otherwise the shadow comparison would measure token handling instead of
authorization. Claims:

- ``sub``: identity performing the request; ``ws``: its workspace
- ``del`` / ``obo``: delegation and the identity it acts for (both or neither)
- ``act.sub``: initiating identity when it differs from ``sub``
- ``scope``: narrows the permissions on exchanged tokens, never widens them
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import jwt

from platform_identity.keys import KeyUnavailable, decode_with_any

PLATFORM_AUDIENCE = "platform-api"
MAX_LIFETIME_SECONDS = 3600  # tokens without a delegation have no revocation record
HARD_MAX_LIFETIME_SECONDS = 30 * 86400
CHANNELS = frozenset({"api", "mcp", "workflow", "cli", "event"})
REQUIRED_CLAIMS = ["exp", "iat", "sub", "jti"]


class Unauthenticated(Exception):
    pass


class KeySource(Protocol):
    def keys(self, kid: str | None) -> list[object]: ...


@dataclass(frozen=True)
class Principal:
    actor: uuid.UUID
    workspace: uuid.UUID
    initiated_by: uuid.UUID
    on_behalf_of: uuid.UUID
    delegation: uuid.UUID | None
    channel: str
    scopes: frozenset[str] | None = None


@dataclass(frozen=True)
class TokenVerifier:
    keys: KeySource
    issuer: str
    audience: str = PLATFORM_AUDIENCE
    max_lifetime: int = MAX_LIFETIME_SECONDS  # delegated tokens only

    def __post_init__(self) -> None:
        if not 0 < self.max_lifetime <= HARD_MAX_LIFETIME_SECONDS:
            raise ValueError(f"max_lifetime must be between 1 and {HARD_MAX_LIFETIME_SECONDS} seconds")

    def verify(self, authorization: str | None, channel: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = decode_with_any(token, self.keys, algorithms=["RS256"], audience=self.audience,
                                     issuer=self.issuer, options={"require": REQUIRED_CLAIMS})
            lifetime = claims["exp"] - claims["iat"]
            limit = self.max_lifetime if claims.get("del") else min(self.max_lifetime, MAX_LIFETIME_SECONDS)
            if lifetime > limit:
                raise Unauthenticated("token lifetime too long")
            actor = uuid.UUID(claims["sub"])
            workspace = uuid.UUID(claims["ws"])
            delegation = uuid.UUID(claims["del"]) if claims.get("del") else None
            on_behalf_of = uuid.UUID(claims["obo"]) if claims.get("obo") else None
            initiated_by = uuid.UUID(claims["act"]["sub"]) if claims.get("act") else actor
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, KeyUnavailable, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        if (delegation is None) != (on_behalf_of is None):
            raise Unauthenticated("delegation and obo must be given together")
        channel = channel or "api"
        if channel not in CHANNELS:
            raise Unauthenticated("unknown channel")
        scopes = frozenset(str(claims["scope"]).split()) if claims.get("scope") else None
        return Principal(actor=actor, workspace=workspace, initiated_by=initiated_by,
                         on_behalf_of=on_behalf_of or actor, delegation=delegation, channel=channel, scopes=scopes)
