"""Service clients: the client_credentials grant with private_key_jwt (docs/SERVICE-CLIENTS.md)."""

from __future__ import annotations

import json
import time
import unittest
import uuid
from typing import Any

try:
    import jwt
    from fastapi.testclient import TestClient

    from platform_identity.app import ACCESS_TOKEN_TTL_SECONDS, create_app
    from platform_identity.auth import TokenVerifier
    from platform_identity.config import Config, ConfigError
    from platform_identity.google import GoogleVerifier
    from platform_identity.keys import SigningKey, StaticKey
except ImportError:  # pragma: no cover - dependencies absent
    TestClient = None  # type: ignore[assignment,misc]

if TestClient is not None:  # a missing service_clients module must fail, not skip
    from platform_identity.service_clients import (
        ASSERTION_TYPE,
        CLIENT_CREDENTIALS,
        ReplayCache,
        parse_service_clients,
    )

from service.test_app import (
    GOOGLE_CLIENT,
    ISSUER,
    PLATFORM_ISSUER,
    FakeDatabase,
    key_pair,
)

TOKEN_URL = f"{ISSUER}/v1/token"


@unittest.skipIf(TestClient is None, "service dependencies are not installed")
class ServiceClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform_private, cls.platform_public = key_pair()
        cls.google_private, cls.google_public = key_pair()
        cls.signing_private, cls.signing_public = key_pair()
        cls.client_private, cls.client_public = key_pair()
        cls.other_private, cls.other_public = key_pair()

    def setUp(self) -> None:
        self.subject, self.ws = uuid.uuid4(), uuid.uuid4()
        self.registry = {"ai-gateway": {
            "subject": str(self.subject), "workspace": str(self.ws), "audiences": ["data-api"],
            "scopes": ["quotas:service", "data.quota:read"], "public_key_pem": self.client_public,
        }}
        self.db = FakeDatabase()
        self.client = self.make_client()

    def make_client(self, **overrides: Any) -> TestClient:
        options: dict[str, Any] = dict(
            connect=self.db.connect,
            platform_verifier=TokenVerifier(StaticKey(self.platform_public), PLATFORM_ISSUER),
            signing_key=SigningKey(self.signing_private),
            issuer=ISSUER,
            google=GoogleVerifier(StaticKey(self.google_public), frozenset({GOOGLE_CLIENT})),
            token_audiences=frozenset({"tasks-api", "data-api", "workflow-api"}),
            service_clients=parse_service_clients(json.dumps(self.registry)),
        )
        options.update(overrides)
        return TestClient(create_app(**options))

    def assertion(self, key: str | None = None, *, ttl: int = 60, **claims: Any) -> str:
        now = int(time.time())
        body = {"iss": "ai-gateway", "sub": "ai-gateway", "aud": TOKEN_URL, "iat": now, "exp": now + ttl,
                "jti": str(uuid.uuid4()), **claims}
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, key or self.client_private, algorithm="RS256")

    def request(self, assertion: str | None = None, **form: Any):
        data = {"grant_type": CLIENT_CREDENTIALS, "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": assertion or self.assertion(), "audience": "data-api", **form}
        return self.client.post("/v1/token", data={k: v for k, v in data.items() if v is not None})

    def decode(self, response) -> dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        return jwt.decode(response.json()["access_token"], self.signing_public, algorithms=["RS256"],
                          audience="data-api", issuer=ISSUER)

    # --- issuing ------------------------------------------------------------

    def test_issues_a_token_for_the_registered_subject_and_workspace(self) -> None:
        response = self.request(scope="quotas:service")
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual((body["token_type"], body["expires_in"]), ("Bearer", ACCESS_TOKEN_TTL_SECONDS))
        self.assertNotIn("issued_token_type", body)
        claims = self.decode(response)
        self.assertEqual((claims["sub"], claims["ws"], claims["scope"], claims["client_id"]),
                         (str(self.subject), str(self.ws), "quotas:service", "ai-gateway"))
        self.assertEqual(claims["exp"] - claims["iat"], ACCESS_TOKEN_TTL_SECONDS)
        self.assertEqual(self.db.queries, [], "service tokens never touch the platform database")

    def test_default_scope_is_every_registered_scope(self) -> None:
        claims = self.decode(self.request())
        self.assertEqual(claims["scope"], "data.quota:read quotas:service")

    def test_the_issuer_itself_is_an_accepted_assertion_audience(self) -> None:
        self.decode(self.request(self.assertion(aud=ISSUER)))

    def test_matching_client_id_form_field_is_accepted(self) -> None:
        self.decode(self.request(client_id="ai-gateway"))

    # --- refusals -----------------------------------------------------------

    def test_client_authentication_failures_are_all_invalid_client(self) -> None:
        now = int(time.time())
        cases = {
            "wrong key": self.assertion(self.other_private),
            "unknown client": self.assertion(iss="nobody", sub="nobody"),
            "sub differs from iss": self.assertion(sub="someone-else"),
            "wrong audience": self.assertion(aud="https://evil.example/v1/token"),
            "audience list": self.assertion(aud=[TOKEN_URL, "https://other.example"]),
            "expired": self.assertion(iat=now - 120, exp=now - 60),
            "lifetime too long": self.assertion(ttl=301),
            "no jti": self.assertion(jti=None),
            "no exp": self.assertion(exp=None),
            "no iat": self.assertion(iat=None),
            "not a jwt": "not-a-jwt",
            "hs256": jwt.encode({"iss": "ai-gateway", "sub": "ai-gateway", "aud": TOKEN_URL, "iat": now,
                                 "exp": now + 60, "jti": "j"}, "a" * 32, algorithm="HS256"),
        }
        for name, assertion in cases.items():
            with self.subTest(name):
                response = self.request(assertion)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.json(), {"error": "invalid_client",
                                                   "error_description": "client authentication failed"})
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_client_id_form_field_must_match_the_assertion(self) -> None:
        self.assertEqual(self.request(client_id="other").json()["error"], "invalid_client")

    def test_an_assertion_is_used_once(self) -> None:
        assertion = self.assertion()
        self.decode(self.request(assertion))
        response = self.request(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "invalid_client")

    def test_a_rejected_request_does_not_burn_the_assertion(self) -> None:
        assertion = self.assertion()
        self.assertEqual(self.request(assertion, audience="workflow-api").json()["error"], "invalid_target")
        self.decode(self.request(assertion))

    def test_audience_and_scope_are_bounded_by_the_registration(self) -> None:
        cases = [
            ({"audience": "workflow-api"}, 400, "invalid_target"),
            ({"audience": "graph-api"}, 400, "invalid_target"),
            ({"scope": "jobs:work"}, 400, "invalid_scope"),
            ({"scope": "quotas:service tasks.task:write"}, 400, "invalid_scope"),
            ({"scope": "not a scope"}, 400, "invalid_scope"),
            ({"client_assertion_type": "urn:x"}, 400, "invalid_request"),
            ({"client_assertion_type": None}, 400, "invalid_request"),
            ({"client_assertion": ""}, 400, "invalid_request"),
        ]
        for form, status, error in cases:
            with self.subTest(form=form):
                response = self.request(**form)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["error"], error)

    def test_no_registered_clients_means_no_client_credentials(self) -> None:
        self.client = self.make_client(service_clients=None)
        self.assertEqual(self.request().json()["error"], "unsupported_grant_type")
        config = self.client.get("/.well-known/openid-configuration").json()
        self.assertNotIn(CLIENT_CREDENTIALS, config["grant_types_supported"])

    def test_token_exchange_still_requires_its_own_parameters(self) -> None:
        response = self.client.post("/v1/token", data={"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                                                       "audience": "tasks-api"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_client_credentials_requests_are_rate_limited(self) -> None:
        statuses = [self.request(self.assertion(self.other_private)).status_code for _ in range(31)]
        self.assertEqual(statuses[-1], 429)

    def test_discovery_advertises_the_grant(self) -> None:
        config = self.client.get("/.well-known/openid-configuration").json()
        self.assertIn(CLIENT_CREDENTIALS, config["grant_types_supported"])
        self.assertIn("private_key_jwt", config["token_endpoint_auth_methods_supported"])
        self.assertEqual(config["token_endpoint_auth_signing_alg_values_supported"], ["RS256"])

    def test_create_app_refuses_clients_for_unavailable_audiences(self) -> None:
        with self.assertRaises(ValueError):
            self.make_client(token_audiences=frozenset({"tasks-api"}))


@unittest.skipIf(TestClient is None, "service dependencies are not installed")
class ReplayCacheTest(unittest.TestCase):
    def test_remembers_until_expiry_then_forgets(self) -> None:
        now = [1000.0]
        cache = ReplayCache(clock=lambda: now[0])
        self.assertTrue(cache.use("ai-gateway", "j1", expires_at=1060))
        self.assertFalse(cache.use("ai-gateway", "j1", expires_at=1060))
        self.assertTrue(cache.use("other-client", "j1", expires_at=1060), "jti is scoped to its client")
        now[0] = 1061.0
        self.assertTrue(cache.use("ai-gateway", "j1", expires_at=1120))

    def test_refuses_when_full_of_live_entries(self) -> None:
        cache = ReplayCache(clock=lambda: 0.0, max_entries_per_client=2)
        self.assertTrue(cache.use("c", "a", expires_at=100))
        self.assertTrue(cache.use("c", "b", expires_at=100))
        self.assertFalse(cache.use("c", "c", expires_at=100), "fail closed instead of forgetting live jtis")
        self.assertTrue(cache.use("d", "a", expires_at=100), "one noisy client does not lock out the others")


@unittest.skipIf(TestClient is None, "service dependencies are not installed")
class ServiceClientConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.public = key_pair()[1]

    def entry(self, **overrides: Any) -> dict[str, Any]:
        return {"subject": str(uuid.uuid4()), "workspace": str(uuid.uuid4()), "audiences": ["data-api"],
                "scopes": ["quotas:service"], "public_key_pem": self.public, **overrides}

    def test_parses_a_registry(self) -> None:
        clients = parse_service_clients(json.dumps({"ai-gateway": self.entry()}))
        client = clients["ai-gateway"]
        self.assertEqual(client.audiences, frozenset({"data-api"}))
        self.assertEqual(client.scopes, frozenset({"quotas:service"}))
        self.assertIsInstance(client.subject, uuid.UUID)
        self.assertNotIn("BEGIN PUBLIC KEY", repr(client))

    def test_empty_registry(self) -> None:
        self.assertEqual(parse_service_clients(""), {})
        self.assertEqual(parse_service_clients("{}"), {})

    def test_rejects_bad_registries(self) -> None:
        small = "-----BEGIN PUBLIC KEY-----\nMFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf\n9Cnzj4p4WGeKLs1Pt8QuKUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQ==\n-----END PUBLIC KEY-----\n"
        subject = str(uuid.uuid4())
        cases = {
            "not json": "{",
            "not an object": "[]",
            "bad client id": json.dumps({"AI gateway": self.entry()}),
            "unknown field": json.dumps({"ai-gateway": self.entry(extra=1)}),
            "missing field": json.dumps({"ai-gateway": {k: v for k, v in self.entry().items() if k != "scopes"}}),
            "bad subject": json.dumps({"ai-gateway": self.entry(subject="x")}),
            "bad workspace": json.dumps({"ai-gateway": self.entry(workspace="x")}),
            "no audiences": json.dumps({"ai-gateway": self.entry(audiences=[])}),
            "bad audience": json.dumps({"ai-gateway": self.entry(audiences=["Data API"])}),
            "no scopes": json.dumps({"ai-gateway": self.entry(scopes=[])}),
            "bad scope": json.dumps({"ai-gateway": self.entry(scopes=["everything"])}),
            "not a key": json.dumps({"ai-gateway": self.entry(public_key_pem="nope")}),
            "small key": json.dumps({"ai-gateway": self.entry(public_key_pem=small)}),
            "nil subject": json.dumps({"ai-gateway": self.entry(subject=str(uuid.UUID(int=0)))}),
            "nil workspace": json.dumps({"ai-gateway": self.entry(workspace=str(uuid.UUID(int=0)))}),
            "subject is the workspace": json.dumps({"ai-gateway": self.entry(subject=subject, workspace=subject)}),
            "deeply nested": "[" * 30000 + "]" * 30000,
            "shared subject": json.dumps({"a-one": self.entry(subject=subject), "a-two": self.entry(subject=subject)}),
            "duplicate client": '{"ai-gateway": %s, "ai-gateway": %s}' % (json.dumps(self.entry()),
                                                                        json.dumps(self.entry())),
            "too many": json.dumps({f"client-{i}": self.entry() for i in range(33)}),
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_service_clients(raw)

    def test_config_reads_the_registry_from_the_environment(self) -> None:
        signing_private = key_pair()[0]
        env = {"IDENTITY_DATABASE_URL": "postgresql://x", "IDENTITY_SIGNING_KEY_PEM": signing_private,
               "IDENTITY_ISSUER": ISSUER, "IDENTITY_PLATFORM_ISSUER": PLATFORM_ISSUER,
               "IDENTITY_PLATFORM_PUBLIC_KEY_PEM": self.public, "IDENTITY_TOKEN_AUDIENCES": "data-api",
               "IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON": json.dumps({"quotas:service": "data.quota:write"})}
        self.assertEqual(dict(Config.from_env(env).service_clients), {})
        env["IDENTITY_SERVICE_CLIENTS_JSON"] = json.dumps({"ai-gateway": self.entry()})
        self.assertEqual(set(Config.from_env(env).service_clients), {"ai-gateway"})
        env["IDENTITY_SERVICE_CLIENTS_JSON"] = json.dumps({"ai-gateway": self.entry(scopes=["data.quota:read"])})
        self.assertEqual(set(Config.from_env(env).service_clients), {"ai-gateway"}, "canonical permissions are fine")
        for bad in (self.entry(audiences=["workflow-api"]), self.entry(scopes=["quota:service"])):
            env["IDENTITY_SERVICE_CLIENTS_JSON"] = json.dumps({"ai-gateway": bad})
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                Config.from_env(env)
        env["IDENTITY_SERVICE_CLIENTS_JSON"] = "{"
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(env)
        self.assertNotIn("{", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
