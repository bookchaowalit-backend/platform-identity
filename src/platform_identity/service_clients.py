"""Service clients: ``client_credentials`` authenticated with ``private_key_jwt`` (docs/SERVICE-CLIENTS.md).

The operator registers each client in ``IDENTITY_SERVICE_CLIENTS_JSON`` with its
RSA public key, the one subject and workspace its tokens carry, and the most
audiences and scopes it may receive. Identity never holds a client secret.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key

CLIENT_CREDENTIALS = "client_credentials"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MAX_ASSERTION_LIFETIME_SECONDS = 300
ASSERTION_LEEWAY_SECONDS = 30
MAX_CLIENTS = 32
MAX_REGISTRY_BYTES = 65536

CLIENT_ID = re.compile(r"[a-z][a-z0-9-]{2,62}")
AUDIENCE = re.compile(r"[a-z][a-z0-9-]{1,62}")
# A concrete shared scope (``quotas:service``) or a canonical permission; both pass core's scope check.
SCOPE = re.compile(r"[a-z][a-z0-9_.-]{0,63}:[a-z][a-z0-9_.-]{0,63}")
FIELDS = frozenset({"subject", "workspace", "audiences", "scopes", "public_key_pem"})
# SPEC-006: optional keys. ``workspace: "*"`` makes a multi-workspace client that needs an installation per
# workspace; ``delegate_from`` (its own audience) and ``delegable_scopes`` (⊆ scopes) let it exchange a token it
# received for one to the next service; ``products`` lists where an admin may install it.
OPTIONAL_FIELDS = frozenset({"delegate_from", "delegable_scopes", "products"})
ANY_WORKSPACE = "*"
PRODUCT = re.compile(r"[a-z][a-z0-9-]{1,31}")


class InvalidClient(Exception):
    pass


@dataclass(frozen=True)
class ServiceClient:
    client_id: str
    subject: uuid.UUID
    workspace: uuid.UUID | None  # None = any workspace that installed it (SPEC-006 "*")
    audiences: frozenset[str]
    scopes: frozenset[str]
    public_key: rsa.RSAPublicKey = field(repr=False)
    delegate_from: str | None = None
    delegable_scopes: frozenset[str] = frozenset()
    products: frozenset[str] = frozenset()


def _unique_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate key")
        values[key] = value
    return values


def _names(value: Any, pattern: re.Pattern[str], limit: int) -> frozenset[str]:
    if (not isinstance(value, list) or not 0 < len(value) <= limit
            or any(not isinstance(v, str) or not pattern.fullmatch(v) for v in value)):
        raise ValueError("invalid list")
    return frozenset(value)


def parse_service_clients(raw: str) -> dict[str, ServiceClient]:
    """Parse and validate the registry; raises ValueError without echoing the input."""
    if not raw.strip():
        return {}
    if len(raw) > MAX_REGISTRY_BYTES:
        raise ValueError("service client registry is too large")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("service client registry is not JSON") from None
    if not isinstance(data, dict) or len(data) > MAX_CLIENTS:
        raise ValueError(f"service client registry must be an object of at most {MAX_CLIENTS} clients")
    clients: dict[str, ServiceClient] = {}
    for client_id, entry in data.items():
        if (not CLIENT_ID.fullmatch(client_id) or not isinstance(entry, dict) or not FIELDS <= set(entry)
                or not set(entry) <= FIELDS | OPTIONAL_FIELDS):
            raise ValueError("invalid service client entry")
        try:
            subject = uuid.UUID(entry["subject"])
            workspace = None if entry["workspace"] == ANY_WORKSPACE else uuid.UUID(entry["workspace"])
            key = load_pem_public_key(entry["public_key_pem"].replace("\\n", "\n").encode())
        except (AttributeError, TypeError, ValueError):
            raise ValueError("invalid service client entry") from None
        nil = uuid.UUID(int=0)
        if nil in (subject, workspace) or subject == workspace:
            raise ValueError("service client subject and workspace must be distinct, non-nil UUIDs")
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("service client keys must be RSA public keys of at least 2048 bits")
        scopes = _names(entry["scopes"], SCOPE, 32)
        delegate_from = entry.get("delegate_from")
        if delegate_from is not None and (not isinstance(delegate_from, str) or not AUDIENCE.fullmatch(delegate_from)):
            raise ValueError("invalid service client entry")
        delegable = _names(entry["delegable_scopes"], SCOPE, 32) if "delegable_scopes" in entry else frozenset()
        if (delegable or delegate_from) and not (delegable and delegate_from and workspace is None):
            raise ValueError("a delegating client needs workspace \"*\", delegate_from and delegable_scopes")
        if not delegable <= scopes:
            raise ValueError("delegable_scopes must be a subset of scopes")
        products = _names(entry["products"], PRODUCT, 16) if "products" in entry else frozenset()
        clients[client_id] = ServiceClient(
            client_id=client_id, subject=subject, workspace=workspace,
            audiences=_names(entry["audiences"], AUDIENCE, 16), scopes=scopes,
            public_key=key, delegate_from=delegate_from, delegable_scopes=delegable, products=products)
    if len({c.subject for c in clients.values()}) != len(clients):
        raise ValueError("service clients must not share a subject")
    froms = [c.delegate_from for c in clients.values() if c.delegate_from]
    if len(froms) != len(set(froms)):
        raise ValueError("two service clients must not delegate from the same audience")
    return clients


class ReplayCache:
    """Assertion ``jti`` values per client, remembered until the assertion expires.

    Per process: enough for one identity-api replica (see docs/SERVICE-CLIENTS.md, "Limits").
    Each client has its own bound, so one noisy client cannot lock out the others; a client
    whose bound is full of live entries is refused rather than having one forgotten.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time, max_entries_per_client: int = 1_000) -> None:
        self._clock = clock
        self._max_entries = max_entries_per_client
        self._seen: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def use(self, client_id: str, jti: str, *, expires_at: float) -> bool:
        with self._lock:
            now = self._clock()
            seen = self._seen.setdefault(client_id, {})
            if seen.get(jti, -1.0) >= now:
                return False
            if len(seen) >= self._max_entries:
                seen = self._seen[client_id] = {k: v for k, v in seen.items() if v >= now}
                if len(seen) >= self._max_entries:
                    return False
            seen[jti] = expires_at
            return True


def verify_assertion(assertion: str, clients: dict[str, ServiceClient], audiences: list[str]) -> tuple[ServiceClient, dict[str, Any]]:
    """The registered client that signed this assertion, and its verified claims.

    Every failure raises the same InvalidClient so the answer does not say which check failed.
    The caller still has to consume the ``jti`` once it decides to issue a token.
    """
    try:
        # Read only ``iss`` to pick the registered key; the signature is verified with that key below.
        unverified = jwt.decode(assertion, options={"verify_signature": False})  # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        claimed = unverified.get("iss")
        client = clients.get(claimed) if isinstance(claimed, str) else None
        if client is None:
            raise InvalidClient()
        claims = jwt.decode(assertion, client.public_key, algorithms=["RS256"], audience=audiences,
                            issuer=client.client_id, leeway=ASSERTION_LEEWAY_SECONDS,
                            options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]})
    except (jwt.PyJWTError, ValueError, TypeError):
        raise InvalidClient() from None
    jti, issued, expires = claims["jti"], claims["iat"], claims["exp"]
    # A single audience only (draft-ietf-oauth-rfc7523bis): a list naming another server is refused.
    if (not isinstance(claims["aud"], str) or claims["sub"] != client.client_id or not isinstance(jti, str) or not 0 < len(jti) <= 200
            or not isinstance(issued, int) or not isinstance(expires, int)
            or not 0 < expires - issued <= MAX_ASSERTION_LIFETIME_SECONDS):
        raise InvalidClient()
    return client, claims
