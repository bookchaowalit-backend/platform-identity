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
``IDENTITY_AUDIENCE_BUNDLES_JSON``  ``{"<audience>": [members]}``: a token exchanged
                               for the key also carries every member (docs/PLATFORM-SERVICES-CUTOVER.md)
``IDENTITY_BUNDLE_TOKEN_TTL_SECONDS``  lifetime of a bundle token, 60..600 (default 300; other tokens 600)
``IDENTITY_SERVICE_CLIENTS_JSON``  registered service clients (public keys only;
                               docs/SERVICE-CLIENTS.md)
``IDENTITY_DECIDE_MODE``       ``shadow`` (default: decide, record nothing) or
                               ``record`` (record like the platform does)
``IDENTITY_TENANCY_MODE``      ``off`` (default) or ``on``: SPEC-006 sign-up, members,
                               delegation and "*" clients; needs platform-empire 0009
``IDENTITY_INVITE_KEY``        HMAC key for invitation codes, emails, subjects (secret,
                               ≥ 32 chars); ``_PREVIOUS`` is also tried during rotation
``IDENTITY_SIGNUP_PRODUCTS``   products open for sign-up (empty = sign-up off)
``IDENTITY_SIGNUP_MODE``       ``closed`` (default), ``invite_code`` or ``open``
``IDENTITY_SIGNUP_CODES_SHA256``  SHA-256 hex of each sign-up code (``invite_code``)
``IDENTITY_SIGNUP_PER_HOUR``   global sign-up budget per hour (default 20)
``IDENTITY_MAX_OWNED_WORKSPACES``  workspaces one person may own (default 3)
``IDENTITY_ROLE_TEMPLATES_JSON``   ``{product: {role: [permissions]}}`` with an owner role
``IDENTITY_DEFAULT_INSTALLS``  ``{product: ["*" client ids]}`` installed at sign-up
=============================  =================================================
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from platform_identity.auth import (
    HARD_MAX_LIFETIME_SECONDS,
    MAX_LIFETIME_SECONDS,
    PLATFORM_AUDIENCE,
)
from platform_identity.keys import require_https
from platform_identity.service_clients import ServiceClient, parse_service_clients
from platform_identity.tenancy import TenancyConfig

DECIDE_MODES = ("shadow", "record")
MAX_BUNDLE = 16
AUDIENCE_PATTERN = "^[a-z][a-z0-9-]{1,62}$"


def validate_audience_bundles(raw: object, audiences: frozenset[str],
                              cells: frozenset[str]) -> dict[str, frozenset[str]]:
    """``{"<audience>": [members]}``; ValueError unless every key is issuable, not the platform and not a cell
    audience, and every member is another issuable audience or platform-api (at most MAX_BUNDLE)."""
    if not isinstance(raw, dict):
        raise ValueError("bundles must be an object")
    bundles: dict[str, frozenset[str]] = {}
    for key, members in raw.items():
        if (not isinstance(key, str) or key not in audiences or key == PLATFORM_AUDIENCE or key in cells
                or not isinstance(members, list) or not 0 < len(members) <= MAX_BUNDLE):
            raise ValueError("bad bundle")
        if any(not isinstance(m, str) or not re.fullmatch(AUDIENCE_PATTERN, m) or m == key
               or (m not in audiences and m != PLATFORM_AUDIENCE) for m in members):
            raise ValueError("bad bundle member")
        bundles[key] = frozenset(members)
    # Bundles do not chain: a member that is itself a bundle key would not bring its own members along.
    if any(m in bundles for members in bundles.values() for m in members):
        raise ValueError("a bundle member is another bundle")
    return bundles


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
    service_clients: Mapping[str, ServiceClient] = field(default_factory=lambda: MappingProxyType({}))

    # IDENTITY_CELL_AUDIENCES: cell APIs whose exchanged tokens also carry platform-api (subset of token_audiences).
    cell_audiences: frozenset[str] = frozenset()
    # IDENTITY_AUDIENCE_BUNDLES_JSON: an exchanged token for the key also carries every member (finance-api → cells).
    audience_bundles: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))
    # IDENTITY_BUNDLE_TOKEN_TTL_SECONDS: a bundle token opens every member, so it lives shorter (app.BUNDLE_TOKEN_TTL_*).
    bundle_token_ttl: int = 300
    # SPEC-005 paid audiences: IDENTITY_ENTITLEMENT_MODE off|shadow|enforce, _URL (finance-api), _SUBJECT (uuid),
    # _CEILINGS_JSON {"finance-api": [":read"/":delete" permissions]} = what a token keeps without a current pass.
    entitlement_mode: str = "off"
    entitlement_url: str | None = None
    entitlement_subject: uuid.UUID | None = None
    entitlement_ceilings: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))
    # SPEC-006 tenancy (sign-up, members, delegation, "*" clients): None until IDENTITY_TENANCY_MODE=on, which also
    # needs platform-empire migration 0009 in the database.
    tenancy: TenancyConfig | None = field(default=None, repr=False)

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
        cells = frozenset(a.strip() for a in env.get("IDENTITY_CELL_AUDIENCES", "").split(",") if a.strip())
        if not cells <= audiences or "platform-api" in cells:
            raise ConfigError("IDENTITY_CELL_AUDIENCES must list IDENTITY_TOKEN_AUDIENCES entries other than platform-api")
        try:
            raw_bundles = env.get("IDENTITY_AUDIENCE_BUNDLES_JSON", "").strip() or "{}"
            if len(raw_bundles) > 8192:
                raise ValueError()
            bundles = validate_audience_bundles(json.loads(raw_bundles, object_pairs_hook=_unique_pairs),
                                                audiences, cells)
        except (TypeError, ValueError, RecursionError):
            raise ConfigError("IDENTITY_AUDIENCE_BUNDLES_JSON must map an issuable audience to a list of other issuable"
                              " audiences or platform-api") from None
        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                number = int(env.get(name,str(default)))
            except ValueError:
                raise ConfigError(f'{name} must be an integer') from None
            if not minimum <= number <= maximum:
                raise ConfigError(f'{name} is out of range')
            return number
        bundle_ttl = integer("IDENTITY_BUNDLE_TOKEN_TTL_SECONDS", 300, 60, 600)

        ent_mode = env.get("IDENTITY_ENTITLEMENT_MODE", "off").strip() or "off"
        if ent_mode not in ("off", "shadow", "enforce"):
            raise ConfigError("IDENTITY_ENTITLEMENT_MODE must be off, shadow or enforce")
        ent_url, ent_subject, ceilings = None, None, {}
        if ent_mode != "off":
            ent_url = env.get("IDENTITY_ENTITLEMENT_URL", "").strip()
            parts = urlsplit(ent_url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.query:
                raise ConfigError("IDENTITY_ENTITLEMENT_URL must be a plain https origin")
            try:
                ent_subject = uuid.UUID(env.get("IDENTITY_ENTITLEMENT_SUBJECT", "").strip())
            except ValueError:
                raise ConfigError("IDENTITY_ENTITLEMENT_SUBJECT must be a UUID") from None
            try:
                raw = json.loads(env.get("IDENTITY_ENTITLEMENT_CEILINGS_JSON", "") or "{}", object_pairs_hook=_unique_pairs)
                if not isinstance(raw, dict) or not raw:
                    raise ValueError()
                for aud, perms in raw.items():
                    if aud not in bundles or not isinstance(perms, list) or not 1 <= len(perms) <= 128:
                        raise ValueError()
                    if any(not isinstance(p, str) or not _CANONICAL.fullmatch(p)
                           or not p.endswith((":read", ":delete")) for p in perms):
                        raise ValueError()
                    ceilings[aud] = frozenset(perms)
            except (TypeError, ValueError, RecursionError):
                raise ConfigError("IDENTITY_ENTITLEMENT_CEILINGS_JSON must map bundle audiences to 1..128 :read or"
                                  " :delete permissions") from None

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
        try:
            service_clients = parse_service_clients(env.get('IDENTITY_SERVICE_CLIENTS_JSON', ''))
        except ValueError as exc:
            raise ConfigError(f'IDENTITY_SERVICE_CLIENTS_JSON: {exc}') from None
        if any(not c.audiences <= audiences for c in service_clients.values()):
            raise ConfigError('IDENTITY_SERVICE_CLIENTS_JSON registers an audience missing from IDENTITY_TOKEN_AUDIENCES')
        if any(s not in scope_mapping and not _CANONICAL.fullmatch(s)
               for c in service_clients.values() for s in c.scopes):
            raise ConfigError('IDENTITY_SERVICE_CLIENTS_JSON scopes must be mapped shared scopes or canonical permissions')
        if any(c.delegable_scopes and c.delegate_from not in audiences for c in service_clients.values()):
            raise ConfigError('IDENTITY_SERVICE_CLIENTS_JSON delegate_from must be an issuable audience')
        tenancy = parse_tenancy(env, service_clients, audiences, cells, bundles)
        return cls(
            tenancy=tenancy,
            service_clients=MappingProxyType(service_clients),
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
            cell_audiences=cells,
            audience_bundles=MappingProxyType(bundles),
            bundle_token_ttl=bundle_ttl,
            entitlement_mode=ent_mode,
            entitlement_url=ent_url,
            entitlement_subject=ent_subject,
            entitlement_ceilings=MappingProxyType(ceilings),
            decide_mode=mode,
            host=env.get("HOST", "0.0.0.0"),
            port=int(env.get("PORT", "8080")),
        )


def parse_tenancy(env: Mapping[str, str], clients: Mapping[str, ServiceClient], audiences: frozenset[str],
                  cells: frozenset[str], bundles: Mapping[str, frozenset[str]]) -> TenancyConfig | None:
    """SPEC-006 settings; every value is checked here so a bad one stops identity from starting."""
    mode = env.get("IDENTITY_TENANCY_MODE", "off").strip() or "off"
    if mode not in ("off", "on"):
        raise ConfigError("IDENTITY_TENANCY_MODE must be off or on")
    if mode == "off":
        if any(c.workspace is None for c in clients.values()):
            raise ConfigError('IDENTITY_SERVICE_CLIENTS_JSON "*" clients need IDENTITY_TENANCY_MODE=on')
        return None
    if "identity-api" not in audiences or "identity-api" in cells or "identity-api" in bundles:
        raise ConfigError("IDENTITY_TENANCY_MODE=on needs identity-api in IDENTITY_TOKEN_AUDIENCES (not a cell or bundle)")
    key = env.get("IDENTITY_INVITE_KEY", "").strip()
    previous = env.get("IDENTITY_INVITE_KEY_PREVIOUS", "").strip()
    if len(key) < 32 or (previous and len(previous) < 32):
        raise ConfigError("IDENTITY_INVITE_KEY (and _PREVIOUS when set) must be at least 32 characters")
    products = frozenset(p.strip() for p in env.get("IDENTITY_SIGNUP_PRODUCTS", "").split(",") if p.strip())
    hashes = frozenset(h.strip().lower() for h in env.get("IDENTITY_SIGNUP_CODES_SHA256", "").split(",") if h.strip())
    if any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
        raise ConfigError("IDENTITY_SIGNUP_CODES_SHA256 must list SHA-256 hex digests")
    try:
        templates_raw = json.loads(env.get("IDENTITY_ROLE_TEMPLATES_JSON", "") or "{}", object_pairs_hook=_unique_pairs)
        installs_raw = json.loads(env.get("IDENTITY_DEFAULT_INSTALLS", "") or "{}", object_pairs_hook=_unique_pairs)
        if not isinstance(templates_raw, dict) or not isinstance(installs_raw, dict):
            raise ValueError()
        templates: dict[str, dict[str, tuple[str, ...]]] = {}
        for product, roles in templates_raw.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", product) or not isinstance(roles, dict) or "owner" not in roles:
                raise ValueError()
            templates[product] = {}
            for role, perms in roles.items():
                if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", role) or not isinstance(perms, list)
                        or not 0 < len(perms) <= 256 or any(not isinstance(p, str) or not _CANONICAL.fullmatch(p)
                                                            for p in perms)):
                    raise ValueError()
                templates[product][role] = tuple(perms)
        installs: dict[str, tuple[str, ...]] = {}
        for product, ids in installs_raw.items():
            if not isinstance(ids, list) or any(i not in clients or clients[i].workspace is not None
                                                or product not in clients[i].products for i in ids):
                raise ValueError()
            installs[product] = tuple(ids)
    except (TypeError, ValueError, RecursionError):
        raise ConfigError("IDENTITY_ROLE_TEMPLATES_JSON / IDENTITY_DEFAULT_INSTALLS are invalid (product → role →"
                          " canonical permissions with an owner role; product → \"*\" clients registered for it)") from None

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(env.get(name, str(default)))
        except ValueError:
            raise ConfigError(f"{name} must be an integer") from None
        if not minimum <= number <= maximum:
            raise ConfigError(f"{name} is out of range")
        return number

    try:
        return TenancyConfig(
            invite_key=key.encode(), invite_key_previous=previous.encode() if previous else None,
            signup_products=products, signup_mode=env.get("IDENTITY_SIGNUP_MODE", "closed").strip() or "closed",
            signup_code_hashes=hashes, signup_per_hour=integer("IDENTITY_SIGNUP_PER_HOUR", 20, 1, 10_000),
            max_owned_workspaces=integer("IDENTITY_MAX_OWNED_WORKSPACES", 3, 1, 100),
            role_templates=MappingProxyType(templates), default_installs=MappingProxyType(installs))
    except ValueError as exc:
        raise ConfigError(f"tenancy settings: {exc}") from None
