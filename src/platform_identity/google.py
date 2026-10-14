"""Verify Google OIDC id_tokens (owner decision D3: Google proves who a person is).

This service never sees a password. It accepts a Google id_token only for one
of its own OAuth client IDs and maps the person by ``google:<sub>``; the email
is never used as an identifier because it can change hands.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

from platform_identity.auth import KeySource
from platform_identity.keys import KeyUnavailable, decode_with_any

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SUBJECT_PREFIX = "google:"
LEEWAY_SECONDS = 30


class InvalidIdToken(Exception):
    pass


@dataclass(frozen=True)
class GoogleVerifier:
    keys: KeySource
    client_ids: frozenset[str]

    def external_subject(self, id_token: str) -> str:
        """``google:<sub>`` for a valid id_token issued to one of our clients."""
        if not self.client_ids:
            raise InvalidIdToken("no Google client is configured")
        try:
            claims = decode_with_any(id_token, self.keys, algorithms=["RS256"], audience=list(self.client_ids),
                                     options={"require": ["exp", "iat", "sub", "iss", "aud"]}, leeway=LEEWAY_SECONDS)
        except (jwt.PyJWTError, KeyUnavailable, ValueError, TypeError) as exc:
            raise InvalidIdToken("invalid id_token") from exc
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise InvalidIdToken("invalid id_token")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.isdigit() or len(subject) > 64:
            raise InvalidIdToken("invalid id_token")
        return SUBJECT_PREFIX + subject
