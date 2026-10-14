"""Runtime configuration from environment variables; secrets never have defaults.

=============================  =================================================
``IDENTITY_DATABASE_URL``      platform database as role ``identity_api`` (secret)
``IDENTITY_SIGNING_KEY_PEM``   this service's RSA private key (secret)
``IDENTITY_ISSUER``            this service's issuer, an HTTPS URL (decision I4)
``IDENTITY_PLATFORM_ISSUER``   issuer of the platform tokens decide accepts
``IDENTITY_PLATFORM_JWKS_URL`` where those tokens' keys are published
``IDENTITY_PLATFORM_PUBLIC_KEY_PEM``  pinned fallback when no JWKS URL is set
``IDENTITY_PLATFORM_MAX_LIFETIME``    delegated token lifetime limit (seconds)
``IDENTITY_GOOGLE_CLIENT_IDS`` comma-separated Google OAuth client IDs
``IDENTITY_TOKEN_AUDIENCES``   audiences ``/v1/token`` may issue for
``IDENTITY_DECIDE_MODE``       ``shadow`` (default: decide, record nothing) or
                               ``record`` (record like the platform does)
=============================  =================================================
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from platform_identity.auth import HARD_MAX_LIFETIME_SECONDS, MAX_LIFETIME_SECONDS
from platform_identity.keys import require_https

DECIDE_MODES = ("shadow", "record")
AUDIENCE_PATTERN = "^[a-z][a-z0-9-]{1,62}$"


class ConfigError(SystemExit):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str = field(repr=False)
    signing_key_pem: str = field(repr=False)
    issuer: str
    platform_issuer: str
    platform_jwks_url: str | None
    platform_public_key_pem: str | None = field(repr=False)
    platform_max_lifetime: int
    google_client_ids: frozenset[str]
    token_audiences: frozenset[str]
    decide_mode: str
    host: str
    port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is required")
            return value

        issuer = required("IDENTITY_ISSUER").rstrip("/")
        try:
            require_https(issuer)
        except ValueError as exc:
            raise ConfigError("IDENTITY_ISSUER must be an HTTPS URL") from exc
        platform_issuer = required("IDENTITY_PLATFORM_ISSUER")
        if platform_issuer == issuer:
            raise ConfigError("IDENTITY_ISSUER must differ from the platform issuer while cells trust the platform")
        jwks_url = env.get("IDENTITY_PLATFORM_JWKS_URL", "").strip() or None
        pem = env.get("IDENTITY_PLATFORM_PUBLIC_KEY_PEM", "").strip() or None
        if not jwks_url and not pem:
            raise ConfigError("set IDENTITY_PLATFORM_JWKS_URL or IDENTITY_PLATFORM_PUBLIC_KEY_PEM")
        try:
            max_lifetime = int(env.get("IDENTITY_PLATFORM_MAX_LIFETIME", str(MAX_LIFETIME_SECONDS)))
        except ValueError as exc:
            raise ConfigError("IDENTITY_PLATFORM_MAX_LIFETIME must be an integer") from exc
        if not 0 < max_lifetime <= HARD_MAX_LIFETIME_SECONDS:
            raise ConfigError("IDENTITY_PLATFORM_MAX_LIFETIME is out of range")
        mode = env.get("IDENTITY_DECIDE_MODE", "shadow").strip()
        if mode not in DECIDE_MODES:
            raise ConfigError(f"IDENTITY_DECIDE_MODE must be one of {', '.join(DECIDE_MODES)}")
        audiences = frozenset(a.strip() for a in env.get("IDENTITY_TOKEN_AUDIENCES", "").split(",") if a.strip())
        if any(not re.match(AUDIENCE_PATTERN, a) for a in audiences):
            raise ConfigError("IDENTITY_TOKEN_AUDIENCES holds an invalid audience")
        return cls(
            database_url=required("IDENTITY_DATABASE_URL"),
            signing_key_pem=required("IDENTITY_SIGNING_KEY_PEM").replace("\\n", "\n"),
            issuer=issuer,
            platform_issuer=platform_issuer,
            platform_jwks_url=jwks_url,
            platform_public_key_pem=pem.replace("\\n", "\n") if pem else None,
            platform_max_lifetime=max_lifetime,
            google_client_ids=frozenset(c.strip() for c in env.get("IDENTITY_GOOGLE_CLIENT_IDS", "").split(",")
                                        if c.strip()),
            token_audiences=audiences,
            decide_mode=mode,
            host=env.get("HOST", "0.0.0.0"),
            port=int(env.get("PORT", "8080")),
        )
