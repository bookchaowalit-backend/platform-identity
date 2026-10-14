"""RS256 key material: this service's signing key and remote JWK Sets it trusts.

Only public key material is ever serialized. The private signing key arrives
from the runtime secret store as a PEM and is never logged or returned.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

JWKS_CACHE_SECONDS = 600
UNKNOWN_KID_REFETCH_SECONDS = 60


class KeyUnavailable(Exception):
    pass


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def rsa_jwk(public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    """RS256 verification JWK with its RFC 7638 thumbprint as ``kid`` (same as platform-empire)."""
    numbers = public_key.public_numbers()
    required = {"e": _b64(numbers.e), "kty": "RSA", "n": _b64(numbers.n)}
    thumbprint = hashlib.sha256(json.dumps(required, separators=(",", ":"), sort_keys=True).encode()).digest()
    kid = base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode()
    return {**required, "alg": "RS256", "use": "sig", "kid": kid}


def public_jwk(public_key_pem: str) -> dict[str, Any]:
    key = load_pem_public_key(public_key_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("verification keys must be RSA public keys")
    return rsa_jwk(key)


@dataclass(frozen=True)
class SigningKey:
    """This service's active signing key; ``kid`` is the thumbprint of its public half."""

    private_pem: str = field(repr=False)

    def __post_init__(self) -> None:
        key = load_pem_private_key(self.private_pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("the signing key must be an RSA private key of at least 2048 bits")
        object.__setattr__(self, "_jwk", rsa_jwk(key.public_key()))

    @property
    def jwk(self) -> dict[str, Any]:
        return dict(self._jwk)  # type: ignore[attr-defined]

    @property
    def kid(self) -> str:
        return self._jwk["kid"]  # type: ignore[attr-defined]

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": self.kid})


def require_https(url: str) -> str:
    """HTTPS, or plain HTTP to this machine only (tests and local development)."""
    parts = urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    if not (parts.scheme == "https" and parts.hostname) and not local:
        raise ValueError(f"{url!r} must be an HTTPS URL")
    return url


class RemoteJwks:
    """A JWK Set fetched over HTTPS and cached; an unknown ``kid`` refetches at most once a minute.

    A failed refresh keeps serving the last good keys and waits before trying again, so an
    outage of the key host does not turn into one slow fetch per request.
    """

    def __init__(self, url: str, *, client: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic,
                 cache_seconds: float = JWKS_CACHE_SECONDS) -> None:
        self.url = require_https(url)
        self._client = client or httpx.Client(timeout=5.0, follow_redirects=False)
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        self._attempted_at = self._clock()
        try:
            response = self._client.get(self.url)
            response.raise_for_status()
            items = response.json().get("keys", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            if not self._keys:
                raise KeyUnavailable("could not load the JWK Set") from exc
            return  # keep the last good keys
        keys = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("kty") != "RSA" or not item.get("kid"):
                continue
            if item.get("alg", "RS256") != "RS256" or item.get("use", "sig") != "sig":
                continue
            try:
                keys[item["kid"]] = jwt.PyJWK(item, algorithm="RS256").key
            except (jwt.PyJWTError, ValueError, TypeError):
                continue  # one bad entry does not discard the set
        if not keys:
            if not self._keys:
                raise KeyUnavailable("the JWK Set has no usable RS256 key")
            return
        self._keys = keys
        self._fetched_at = self._clock()

    def keys(self, kid: str | None) -> list[Any]:
        """Candidate verification keys: the one named by ``kid``, or every key when the token names none."""
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at >= self._cache_seconds
            if stale and (self._attempted_at is None or now - self._attempted_at >= UNKNOWN_KID_REFETCH_SECONDS
                          or not self._keys):
                self._fetch()
            if kid is None:
                return list(self._keys.values())
            if kid not in self._keys and now - (self._attempted_at or 0) >= UNKNOWN_KID_REFETCH_SECONDS:
                self._fetch()
            if kid in self._keys:
                return [self._keys[kid]]
        raise KeyUnavailable("unknown signing key")


@dataclass(frozen=True)
class StaticKey:
    """A pinned PEM, the fallback when no JWKS URL is configured."""

    public_key_pem: str = field(repr=False)

    def keys(self, kid: str | None) -> list[Any]:
        return [self.public_key_pem]


def decode_with_any(token: str, source: Any, **options: Any) -> dict[str, Any]:
    """Decode with the first candidate key whose signature verifies.

    Platform tokens carry no ``kid`` today, so during a key rotation each published key is tried.
    Only a signature failure moves on to the next key; any other error (expiry, audience) is final.
    """
    kid = jwt.get_unverified_header(token).get("kid")
    candidates = source.keys(kid if isinstance(kid, str) else None)
    if not candidates:
        raise KeyUnavailable("no verification key")
    for key in candidates[:-1]:
        try:
            return jwt.decode(token, key, **options)
        except jwt.InvalidSignatureError:
            continue
    return jwt.decode(token, candidates[-1], **options)
