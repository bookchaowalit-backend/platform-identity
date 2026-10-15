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
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from platform_identity.auth import HARD_MAX_LIFETIME_SECONDS, MAX_LIFETIME_SECONDS
from platform_identity.keys import require_https

DECIDE_MODES = ("shadow", "record")
AUDIENCE_PATTERN = "^[a-z][a-z0-9-]{1,62}$"


_CANONICAL = re.compile(r'[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)')
_SHARED = re.compile(r'[a-z][a-z0-9_.-]{0,63}:[a-z][a-z0-9_.-]{0,63}')


def validate_shared_scope_permissions(mapping: Mapping[str,str]) -> dict[str,str]:
    if not isinstance(mapping,Mapping) or len(mapping) > 64:
        raise ValueError('shared scope map must be an object with at most 64 entries')
    for scope,permission in mapping.items():
        if (not isinstance(scope,str) or len(scope)>128 or not _SHARED.fullmatch(scope) or _CANONICAL.fullmatch(scope)
                or not isinstance(permission,str) or len(permission)>200 or not _CANONICAL.fullmatch(permission)):
            raise ValueError('shared scopes must map to canonical RBAC permissions')
    return dict(mapping)


def _unique_pairs(pairs):
    values = {}
    for key,value in pairs:
        if key in values:
            raise ValueError('duplicate scope mapping')
        values[key] = value
    return values


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

    rate_limit_mode: str = 'local'
    rate_limit_database_url: str | None = field(default=None, repr=False)
    rate_limit_per_key: int = 30
    rate_limit_global: int = 300
    rate_limit_pool_max: int = 4
    trusted_proxies: tuple[str, ...] = ()
    db_pool_max: int = 8
    db_timeout_seconds: float = 5
    db_statement_timeout_ms: int = 5000
    max_concurrency: int = 64
    shared_scope_permissions: Mapping[str,str] = field(default_factory=lambda:MappingProxyType({}))

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
        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                number = int(env.get(name,str(default)))
            except ValueError:
                raise ConfigError(f'{name} must be an integer') from None
            if not minimum <= number <= maximum:
                raise ConfigError(f'{name} is out of range')
            return number

        rate_mode = env.get('IDENTITY_RATE_LIMIT_MODE','local').strip()
        if rate_mode not in ('local','postgres'):
            raise ConfigError('IDENTITY_RATE_LIMIT_MODE must be local or postgres')
        limiter_dsn = env.get('IDENTITY_RATE_LIMIT_DATABASE_URL','').strip() or None
        if rate_mode == 'postgres' and limiter_dsn is None:
            raise ConfigError('postgres mode requires IDENTITY_RATE_LIMIT_DATABASE_URL')
        proxies = tuple(p.strip() for p in env.get('IDENTITY_TRUSTED_PROXIES','').split(',') if p.strip())
        try:
            if len(proxies) > 32:
                raise ValueError()
            for proxy in proxies:
                network = ipaddress.ip_network(proxy)
                if network.prefixlen == 0:
                    raise ValueError()
        except ValueError:
            raise ConfigError('IDENTITY_TRUSTED_PROXIES must contain at most 32 explicit non-wildcard CIDRs') from None
        try:
            db_timeout = float(env.get('IDENTITY_DB_TIMEOUT_SECONDS','5'))
            if not math.isfinite(db_timeout) or not .1 <= db_timeout <= 30:
                raise ValueError()
        except ValueError:
            raise ConfigError('IDENTITY_DB_TIMEOUT_SECONDS must be .1..30 seconds') from None
        try:
            raw_mapping = env.get('IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON','{}')
            if len(raw_mapping) > 16384:
                raise ValueError()
            scope_mapping = validate_shared_scope_permissions(json.loads(raw_mapping,object_pairs_hook=_unique_pairs))
        except (TypeError,ValueError):
            raise ConfigError('IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON must map concrete shared scopes to canonical RBAC actions') from None
        return cls(
            shared_scope_permissions=MappingProxyType(scope_mapping),
            rate_limit_mode=rate_mode,
            rate_limit_database_url=limiter_dsn,
            rate_limit_per_key=integer('IDENTITY_RATE_LIMIT_PER_KEY',30,1,1000000),
            rate_limit_global=integer('IDENTITY_RATE_LIMIT_GLOBAL',300,1,1000000),
            rate_limit_pool_max=integer('IDENTITY_RATE_LIMIT_POOL_MAX',4,1,32),
            trusted_proxies=proxies,
            db_pool_max=integer('IDENTITY_DB_POOL_MAX',8,1,32),
            db_timeout_seconds=db_timeout,
            db_statement_timeout_ms=integer('IDENTITY_DB_STATEMENT_TIMEOUT_MS',5000,100,30000),
            max_concurrency=integer('IDENTITY_MAX_CONCURRENCY',64,1,1024),
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
