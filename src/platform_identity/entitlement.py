"""Paid audiences (SPEC-005): without a current pass, a token keeps only a read/erase scope ceiling.

A finance-api bundle token reaches the finance cells directly, so the pass gate cannot live in finance-api alone.
The platform PDP enforces a token's ``scope`` claim as a ceiling, so a workspace without a pass gets a token whose
scope holds only the ``:read`` and ``:delete`` permissions configured for that audience: its people can still see,
export and erase their own data (PDPA), but write nothing until they pay.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from platform_identity.keys import SigningKey

log = logging.getLogger("platform_identity")

MODES = ("off", "shadow", "enforce")
SCOPE = "finance-api.entitlement:read"
ACTIVE_SECONDS = 60.0
INACTIVE_SECONDS = 5.0
UNKNOWN_SECONDS = 15.0
TIMEOUT = 3.0  # a token request waits at most this long on finance-api
MAX_CEILING = 48  # the ceiling rides in every no-pass token: keep it well under proxy header limits
CANONICAL = re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|delete)$")


class Misconfigured(Exception):
    """finance-api refused identity's own lookup (401/403/404): a configuration fault, not an outage."""


class Entitlement:
    """Mode, the scope ceiling per paid audience, and a cached ``lookup(workspace) -> bool``."""

    def __init__(
        self,
        mode: str,
        ceilings: Mapping[str, frozenset[str]],
        lookup: Callable[[uuid.UUID], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"entitlement mode must be one of {MODES}")
        for audience, scopes in ceilings.items():
            if not scopes or len(scopes) > MAX_CEILING:
                raise ValueError(f"{audience}: a ceiling holds 1..{MAX_CEILING} permissions")
            if any(not CANONICAL.fullmatch(s) for s in scopes):
                raise ValueError(f"{audience}: a ceiling holds only :read and :delete permissions")
        self.mode, self.ceilings, self.lookup, self.clock = mode, dict(ceilings), lookup, clock
        self._cache: dict[uuid.UUID, tuple[float, bool | None]] = {}
        self._lock = threading.Lock()
        self._flights: dict[uuid.UUID, threading.Lock] = {}

    def _cached(self, workspace: uuid.UUID) -> tuple[bool, bool | None]:
        with self._lock:
            hit = self._cache.get(workspace)
        if hit is None:
            return False, None
        age, cached = self.clock() - hit[0], hit[1]
        limit = UNKNOWN_SECONDS if cached is None else ACTIVE_SECONDS if cached else INACTIVE_SECONDS
        return age < limit, cached

    def active(self, workspace: uuid.UUID) -> bool | None:
        """True / False, or None when finance-api is down or slow (the caller fails open).

        A refusal of identity's own lookup (finance-api not configured for it) counts as "no pass": a broken
        configuration must not switch enforcement off silently.
        """
        fresh, cached = self._cached(workspace)
        if fresh:
            return cached
        with self._lock:
            flight = self._flights.setdefault(workspace, threading.Lock())
        with flight:  # one lookup per workspace at a time; the others wait and read its answer
            fresh, cached = self._cached(workspace)
            if fresh:
                return cached
            answer: bool | None
            try:
                answer = bool(self.lookup(workspace))
            except Misconfigured as exc:
                log.error("entitlement lookup refused (%s): check FINANCE_API_ENTITLEMENT_SUBJECT; no pass assumed",
                          exc)
                answer = False
            except Exception as exc:  # timeouts, connection errors, 5xx: cannot tell
                log.warning("entitlement unknown reason=%s", type(exc).__name__)
                answer = None
            with self._lock:
                self._cache[workspace] = (self.clock(), answer)
                self._flights.pop(workspace, None)
            return answer


def finance_lookup(
    base_url: str,
    subject: uuid.UUID,
    signing_key: SigningKey,
    issuer: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Callable[[uuid.UUID], bool]:
    """Ask finance-api SPEC-006 with a 60-second token identity signs for itself."""
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.query:
        raise ValueError("the entitlement URL must be a plain https origin")
    url = base_url.rstrip("/") + "/v1/internal/entitlement"
    client = httpx.Client(transport=transport, timeout=httpx.Timeout(TIMEOUT, connect=1.0), follow_redirects=False)

    def lookup(workspace: uuid.UUID) -> bool:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer, "aud": "finance-api", "sub": str(subject), "ws": str(workspace),
            "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()), "scope": SCOPE,
        }
        response = client.get(url, headers={"Authorization": f"Bearer {signing_key.sign(claims)}"})
        if response.status_code in (401, 403, 404):
            raise Misconfigured(f"entitlement answered {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"entitlement answered {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("active"), bool):
            raise RuntimeError("entitlement body")
        return body["active"]

    return lookup
