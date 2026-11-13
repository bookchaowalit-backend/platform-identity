"""SPEC-007: one stdout JSON line per refused ``/v1/token`` request (``token.issue_failed``).

platform-security-signals reads these lines from Railway logs (rule SIG-06). The line carries the grant
type, the OAuth error as a small fixed reason set and an HMAC of a *registered* client id. It never
carries the subject token, the assertion, the scope list, the audience, the workspace or an address.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from collections.abc import Callable, Collection

import jwt

EVENT = "token.issue_failed"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
GRANTS = {"client_credentials": "client_credentials", TOKEN_EXCHANGE_GRANT: "token_exchange"}
# OAuth error code -> reason. ``slow_down`` is the 429 from the rate limiter; 5xx answers are outages,
# not refusals, and are not reported.
REASONS = {
    "invalid_request": "invalid_request",
    "invalid_client": "invalid_client",
    "invalid_grant": "invalid_grant",
    "invalid_scope": "invalid_scope",
    "invalid_target": "invalid_target",
    "unauthorized_client": "unauthorized_client",
    "unsupported_grant_type": "unsupported_grant_type",
    "slow_down": "rate_limited",
}
HASH_LABEL = b"platform-identity token.issue_failed client hash v1"


def hash_key_from(secret: str) -> bytes:
    """A key of its own, derived with a fixed label, so the hash never reveals anything about ``secret``."""
    return hmac.new(secret.encode(), HASH_LABEL, hashlib.sha256).digest()


class FailureEvents:
    def __init__(self, key: bytes | None, registered: Collection[str], write: Callable[[str], object] | None = None):
        self.key, self.registered = key, frozenset(registered)
        self.write = write or (lambda line: (sys.stdout.write(line), sys.stdout.flush()))

    def client_of(self, client_id: str | None, assertion: str | None) -> str | None:
        """The claimed client, only when it is a registered one: an unknown or forged name is never logged."""
        claimed = client_id
        if claimed is None and assertion:
            try:
                claimed = jwt.decode(assertion, options={"verify_signature": False}).get("iss")
            except jwt.PyJWTError:
                claimed = None
        return claimed if isinstance(claimed, str) and claimed in self.registered else None

    def emit(self, *, status: int, error: str, grant_type: str | None, client_id: str | None = None,
             assertion: str | None = None) -> None:
        if not 400 <= status < 500 or error not in REASONS:
            return
        # Only client_credentials authenticates a client; a client_id sent with another grant names nobody.
        client = self.client_of(client_id, assertion) if grant_type == "client_credentials" else None
        client_hash = (hmac.new(self.key, client.encode(), hashlib.sha256).hexdigest()[:16]
                       if client is not None and self.key else "")
        line = {"event": EVENT, "level": "warning", "grant_type": GRANTS.get(grant_type or "", "other"),
                "reason": REASONS[error], "client_hash": client_hash}
        try:
            self.write(json.dumps(line, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001  a log line must never change the answer
            pass
