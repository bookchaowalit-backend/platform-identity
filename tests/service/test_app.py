"""Service tests with a fake database; the real rules are exercised in platform-empire's parity test.

Skipped when the service dependencies are not installed, so the stdlib-only
contract check (``scripts/check.sh``) still runs anywhere.
"""

from __future__ import annotations

import time
import unittest
import uuid
from typing import Any

try:
    import httpx
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient

    from platform_identity.app import (
        ACCESS_TOKEN_TTL_SECONDS,
        ID_TOKEN_TYPE,
        TOKEN_EXCHANGE,
        create_app,
    )
    from platform_identity.auth import TokenVerifier
    from platform_identity.google import GoogleVerifier
    from platform_identity.keys import RemoteJwks, SigningKey, StaticKey, public_jwk
except ImportError:  # pragma: no cover - dependencies absent
    TestClient = None  # type: ignore[assignment,misc]

PLATFORM_ISSUER = "empire-platform-test"
ISSUER = "https://identity.example.test"
GOOGLE_CLIENT = "client-1.apps.googleusercontent.com"


def key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


class FakeConnection:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db
        self.row_factory: Any = None
        self.workspace: str | None = None
        self.committed = False

    def execute(self, query: str, params: tuple = ()) -> FakeConnection:
        self.db.queries.append(query)
        self._row: Any = None
        if "set_config('app.workspace_id'" in query:
            self.workspace = params[0]
        elif "platform.authorize" in query:
            self.db.authorize_args.append(params)
            if self.db.authorize_returns_row:
                self._row = {"decision": self.db.decision, "reason": self.db.reason, "decision_id": uuid.uuid4()}
        elif "platform.identities" in query:
            workspace, subject = params
            assert str(workspace) == self.workspace, "query must run in the bound workspace"
            identity = self.db.members.get((str(workspace), subject))
            self._row = {"id": identity} if identity else None
        return self

    def fetchone(self) -> Any:
        return self._row

    def commit(self) -> None:
        self.committed = True
        self.db.commits += 1

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeDatabase:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.authorize_args: list[tuple] = []
        self.commits = 0
        self.decision = "allow"
        self.reason = "allowed"
        self.authorize_returns_row = True
        self.members: dict[tuple[str, str], uuid.UUID] = {}

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


@unittest.skipIf(TestClient is None, "service dependencies are not installed")
class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform_private, cls.platform_public = key_pair()
        cls.google_private, cls.google_public = key_pair()
        cls.signing_private, cls.signing_public = key_pair()

    def setUp(self) -> None:
        self.db = FakeDatabase()
        self.client = self.make_client("shadow")
        self.actor, self.ws = uuid.uuid4(), uuid.uuid4()

    def make_client(self, mode: str) -> TestClient:
        app = create_app(
            connect=self.db.connect,
            platform_verifier=TokenVerifier(StaticKey(self.platform_public), PLATFORM_ISSUER),
            signing_key=SigningKey(self.signing_private),
            issuer=ISSUER,
            google=GoogleVerifier(StaticKey(self.google_public), frozenset({GOOGLE_CLIENT})),
            token_audiences=frozenset({"tasks-api"}),
            decide_mode=mode,
        )
        return TestClient(app)

    def platform_token(self, ttl: int = 300, **extra: Any) -> dict[str, str]:
        now = int(time.time())
        claims = {"iss": PLATFORM_ISSUER, "aud": "platform-api", "sub": str(self.actor), "ws": str(self.ws),
                  "iat": now, "exp": now + ttl, "jti": str(uuid.uuid4()), **extra}
        return {"Authorization": "Bearer " + jwt.encode(claims, self.platform_private, algorithm="RS256")}

    def decide(self, headers: dict[str, str], **body: Any):
        payload = {"action": "decisions.decision:read", "resource_type": "decision", "resource_id": "d-1", **body}
        return self.client.post("/v1/authz/decide", headers=headers, json=payload)

    # --- decide -------------------------------------------------------------

    def test_shadow_decide_answers_like_the_platform_and_records_nothing(self) -> None:
        response = self.decide(self.platform_token())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"decision": "allow", "reason": "allowed", "decision_id": None})
        self.assertEqual(response.headers["x-identity-decide-mode"], "shadow")
        self.assertEqual(self.db.commits, 0)
        args = self.db.authorize_args[0]
        self.assertEqual(args[:6], (self.actor, self.ws, "decisions.decision:read", "decision", "d-1", self.ws))
        self.assertEqual(args[6:], (None, None, None, "api", False))

    def test_decide_fails_closed_when_authorize_returns_no_row(self) -> None:
        self.db.authorize_returns_row = False
        response = self.decide(self.platform_token())
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"], {"error": "unavailable"})
        self.assertNotIn("x-identity-decide-mode", response.headers)

    def test_record_mode_commits_and_returns_the_decision_id(self) -> None:
        self.client = self.make_client("record")
        response = self.decide(self.platform_token())
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["decision_id"])
        self.assertEqual(self.db.commits, 1)

    def test_delegated_token_passes_delegation_and_on_behalf_of(self) -> None:
        delegation, grantor = uuid.uuid4(), uuid.uuid4()
        response = self.decide(self.platform_token(ttl=7200, **{"del": str(delegation), "obo": str(grantor)}))
        self.assertEqual(response.status_code, 401, "delegated tokens are capped by the configured max lifetime")
        response = self.decide(self.platform_token(**{"del": str(delegation), "obo": str(grantor)}),
                               approval_id=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.authorize_args[-1][6:8], (delegation, grantor))

    def test_refuses_what_the_platform_refuses(self) -> None:
        cases = [
            ({}, 401, "missing bearer token"),
            ({"Authorization": "Bearer nonsense"}, 401, "invalid token"),
            (self.platform_token(ttl=7200), 401, "token lifetime too long"),
            (self.platform_token(**{"del": str(uuid.uuid4())}), 401, "delegation and obo must be given together"),
            ({**self.platform_token(), "x-empire-channel": "fax"}, 401, "unknown channel"),
            (self.platform_token(scope="tasks.task:read"), 403, "scope_insufficient"),
        ]
        for headers, status, reason in cases:
            with self.subTest(reason=reason):
                response = self.decide(headers)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["detail"]["reason"], reason)
        self.assertEqual(self.db.authorize_args, [])

    def test_rejects_tokens_for_another_audience_or_issuer(self) -> None:
        now = int(time.time())
        for claims in ({"aud": "tasks-api"}, {"iss": "someone-else"}):
            token = jwt.encode({"iss": PLATFORM_ISSUER, "aud": "platform-api", "sub": str(self.actor),
                                "ws": str(self.ws), "iat": now, "exp": now + 60, "jti": "j", **claims},
                               self.platform_private, algorithm="RS256")
            self.assertEqual(self.decide({"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_invalid_action_is_a_validation_error(self) -> None:
        self.assertEqual(self.decide(self.platform_token(), action="not a permission").status_code, 422)

    # --- token exchange ------------------------------------------------------

    def id_token(self, **extra: Any) -> str:
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": GOOGLE_CLIENT, "sub": "1234567890",
                  "iat": now, "exp": now + 300, **extra}
        return jwt.encode(claims, self.google_private, algorithm="RS256")

    def exchange(self, subject_token: str, **form: Any):
        data = {"grant_type": TOKEN_EXCHANGE, "subject_token": subject_token, "subject_token_type": ID_TOKEN_TYPE,
                "audience": "tasks-api", "workspace": str(self.ws), **form}
        return self.client.post("/v1/token", data=data)

    def test_exchanges_a_google_id_token_for_a_workspace_member(self) -> None:
        identity = uuid.uuid4()
        self.db.members[(str(self.ws), "google:1234567890")] = identity
        response = self.exchange(self.id_token(), scope="tasks.task:read")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual(body["expires_in"], ACCESS_TOKEN_TTL_SECONDS)
        header = jwt.get_unverified_header(body["access_token"])
        self.assertEqual(header["kid"], public_jwk(self.signing_public)["kid"])
        claims = jwt.decode(body["access_token"], self.signing_public, algorithms=["RS256"], audience="tasks-api",
                            issuer=ISSUER)
        self.assertEqual((claims["sub"], claims["ws"], claims["scope"]), (str(identity), str(self.ws),
                                                                          "tasks.task:read"))
        self.assertLessEqual(claims["exp"] - claims["iat"], ACCESS_TOKEN_TTL_SECONDS)

    def test_token_exchange_refusals(self) -> None:
        self.db.members[(str(self.ws), "google:1234567890")] = uuid.uuid4()
        other_key, _ = key_pair()
        forged = jwt.encode({"iss": "https://accounts.google.com", "aud": GOOGLE_CLIENT, "sub": "1",
                             "iat": int(time.time()), "exp": int(time.time()) + 60}, other_key, algorithm="RS256")
        cases = [
            (self.exchange(self.id_token(), audience="graph-api"), 400, "invalid_target"),
            (self.exchange(self.id_token(), grant_type="password"), 400, "unsupported_grant_type"),
            (self.exchange(self.id_token(), subject_token_type="urn:x"), 400, "invalid_request"),
            (self.exchange(self.id_token(), scope="everything"), 400, "invalid_scope"),
            (self.exchange(forged), 400, "invalid_grant"),
            (self.exchange(self.id_token(aud="someone-else")), 400, "invalid_grant"),
            (self.exchange(self.id_token(iss="https://evil.example")), 400, "invalid_grant"),
            (self.exchange(self.id_token(sub="999")), 400, "invalid_grant"),
            (self.exchange(self.id_token(), workspace=str(uuid.uuid4())), 400, "invalid_grant"),
        ]
        for response, status, error in cases:
            with self.subTest(error=error):
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["error"], error)

    def test_token_endpoint_is_rate_limited_by_the_last_proxy_hop(self) -> None:
        def call(forwarded: str) -> int:
            data = {"grant_type": TOKEN_EXCHANGE, "subject_token": "x", "subject_token_type": ID_TOKEN_TYPE,
                    "audience": "nope", "workspace": str(self.ws)}
            return self.client.post("/v1/token", data=data, headers={"x-forwarded-for": forwarded}).status_code
        statuses = [call(f"10.0.0.{i}, 203.0.113.7") for i in range(31)]
        self.assertEqual(statuses[-1], 429, "a spoofed first hop must not reset the limit")
        self.assertEqual(call("203.0.113.8"), 400)

    def test_token_validation_errors_do_not_echo_the_subject_token(self) -> None:
        response = self.client.post("/v1/token", data={"subject_token": "very-secret-token"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")
        self.assertNotIn("very-secret-token", response.text)

    def test_kidless_platform_tokens_verify_against_any_published_key(self) -> None:
        _, other_public = key_pair()
        jwks = {"keys": [public_jwk(other_public), public_jwk(self.platform_public)]}
        source = RemoteJwks("https://platform.test/jwks", client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks))))
        verifier = TokenVerifier(source, PLATFORM_ISSUER)
        principal = verifier.verify(self.platform_token()["Authorization"], None)
        self.assertEqual(principal.actor, self.actor)

    def test_jwks_outage_keeps_the_last_good_keys(self) -> None:
        now = [0.0]
        state = {"up": True}

        def handler(_: httpx.Request) -> httpx.Response:
            if not state["up"]:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"keys": [public_jwk(self.platform_public)]})

        source = RemoteJwks("https://platform.test/jwks", client=httpx.Client(transport=httpx.MockTransport(handler)),
                            clock=lambda: now[0])
        self.assertEqual(len(source.keys(None)), 1)
        state["up"], now[0] = False, 10_000.0
        self.assertEqual(len(source.keys(None)), 1)
        with self.assertRaises(ValueError):
            RemoteJwks("http://localhost.evil.example/jwks")

    # --- discovery -----------------------------------------------------------

    def test_jwks_publishes_only_the_public_signing_key(self) -> None:
        keys = self.client.get("/.well-known/jwks.json").json()["keys"]
        self.assertEqual(keys, [public_jwk(self.signing_public)])
        self.assertNotIn("d", keys[0])
        config = self.client.get("/.well-known/openid-configuration").json()
        self.assertEqual(config["issuer"], ISSUER)
        self.assertEqual(config["jwks_uri"], f"{ISSUER}/.well-known/jwks.json")
        self.assertEqual(self.client.get("/health").json()["decide_mode"], "shadow")


if __name__ == "__main__":
    unittest.main()
