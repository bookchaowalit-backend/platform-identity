"""SPEC-005: without a current pass a paid audience's token keeps only a read/erase scope ceiling."""

from __future__ import annotations

import logging
import unittest
import uuid
from typing import Any

import httpx
import jwt
from fastapi.testclient import TestClient

from platform_identity.app import create_app
from platform_identity.auth import TokenVerifier
from platform_identity.entitlement import SCOPE, Entitlement, finance_lookup
from platform_identity.google import GoogleVerifier
from platform_identity.keys import SigningKey, StaticKey
from tests.service import test_app as base
from tests.service.test_app import GOOGLE_CLIENT, ISSUER, PLATFORM_ISSUER


class Helpers(unittest.TestCase):
    """The keys, fake database and Google exchange of test_app.ServiceTest, without its tests."""

    setUpClass = classmethod(base.ServiceTest.setUpClass.__func__)  # type: ignore[attr-defined]
    id_token = base.ServiceTest.id_token
    exchange = base.ServiceTest.exchange

    def setUp(self) -> None:
        self.db = base.FakeDatabase()
        self.actor, self.ws = uuid.uuid4(), uuid.uuid4()

CEILING = frozenset({"transactions.transaction:read", "transactions.transaction:delete", "tax.tax-item:read"})


class EntitlementTest(Helpers):
    def paid_client(self, mode: str = "enforce", answer: Any = False, **kw: Any) -> TestClient:
        self.asked: list[uuid.UUID] = []

        def lookup(ws: uuid.UUID) -> bool:
            self.asked.append(ws)
            if isinstance(answer, Exception):
                raise answer
            return answer

        ent = Entitlement(mode, {"finance-api": CEILING}, lookup, **kw)
        self.db.members[(str(self.ws), "google:1234567890")] = uuid.uuid4()
        return TestClient(create_app(
            connect=self.db.connect,
            platform_verifier=TokenVerifier(StaticKey(self.platform_public), PLATFORM_ISSUER),
            signing_key=SigningKey(self.signing_private),
            issuer=ISSUER,
            google=GoogleVerifier(StaticKey(self.google_public), frozenset({GOOGLE_CLIENT})),
            token_audiences=frozenset({"tasks-api", "transactions-api", "tax-api", "finance-api"}),
            cell_audiences=frozenset({"transactions-api"}),
            audience_bundles={"finance-api": frozenset({"transactions-api", "tax-api", "platform-api"})},
            entitlement=ent,
        ))

    def claims(self, response) -> dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        return jwt.decode(response.json()["access_token"], self.signing_public, algorithms=["RS256"],
                          audience="finance-api", issuer=ISSUER)

    def test_no_pass_in_enforce_keeps_only_the_ceiling(self) -> None:
        self.client = self.paid_client()
        claims = self.claims(self.exchange(self.id_token(), audience="finance-api"))
        self.assertEqual(set(claims["scope"].split()), CEILING)
        self.assertEqual(self.asked, [self.ws])

    def test_requested_scopes_are_cut_to_the_ceiling_or_refused(self) -> None:
        self.client = self.paid_client()
        both = "transactions.transaction:read transactions.transaction:write"
        claims = self.claims(self.exchange(self.id_token(), audience="finance-api", scope=both))
        self.assertEqual(claims["scope"], "transactions.transaction:read")
        only_write = self.exchange(self.id_token(), audience="finance-api", scope="transactions.transaction:write")
        self.assertEqual(only_write.status_code, 400)
        self.assertEqual(only_write.json(), {"error": "invalid_scope", "error_description": "pass_required"})

    def test_a_current_pass_changes_nothing(self) -> None:
        self.client = self.paid_client(answer=True)
        self.assertNotIn("scope", self.claims(self.exchange(self.id_token(), audience="finance-api")))

    def test_shadow_logs_and_changes_nothing(self) -> None:
        self.client = self.paid_client(mode="shadow")
        with self.assertLogs("platform_identity", logging.INFO) as logs:
            claims = self.claims(self.exchange(self.id_token(), audience="finance-api"))
        self.assertNotIn("scope", claims)
        self.assertTrue(any("pass missing" in line for line in logs.output))

    def test_off_never_asks(self) -> None:
        self.client = self.paid_client(mode="off")
        self.assertNotIn("scope", self.claims(self.exchange(self.id_token(), audience="finance-api")))
        self.assertEqual(self.asked, [])

    def test_an_outage_fails_open(self) -> None:
        self.client = self.paid_client(answer=RuntimeError("down"))
        with self.assertLogs("platform_identity", logging.WARNING):
            claims = self.claims(self.exchange(self.id_token(), audience="finance-api"))
        self.assertNotIn("scope", claims)

    def test_a_member_audience_asked_alone_gets_the_ceiling_too(self) -> None:
        """Review P0: audience=transactions-api on its own must not skip the pass check."""
        self.client = self.paid_client()
        response = self.exchange(self.id_token(), audience="transactions-api")
        self.assertEqual(response.status_code, 200, response.text)
        claims = jwt.decode(response.json()["access_token"], self.signing_public, algorithms=["RS256"],
                            audience="transactions-api", issuer=ISSUER)
        self.assertEqual(set(claims["scope"].split()), CEILING)
        self.assertEqual(self.asked, [self.ws])

    def test_audiences_outside_paid_bundles_are_not_asked(self) -> None:
        self.client = self.paid_client()
        self.exchange(self.id_token(), audience="tasks-api")
        self.assertEqual(self.asked, [])

    def test_a_refused_lookup_counts_as_no_pass(self) -> None:
        from platform_identity.entitlement import Misconfigured

        self.client = self.paid_client(answer=Misconfigured("403"))
        with self.assertLogs("platform_identity", logging.ERROR):
            claims = self.claims(self.exchange(self.id_token(), audience="finance-api"))
        self.assertEqual(set(claims["scope"].split()), CEILING)

    def test_workspaces_are_cached_apart(self) -> None:
        answers = {}

        def lookup(ws: uuid.UUID) -> bool:
            return answers[ws]

        a, b = uuid.uuid4(), uuid.uuid4()
        answers.update({a: True, b: False})
        ent = Entitlement("enforce", {"finance-api": CEILING}, lookup)
        self.assertEqual((ent.active(a), ent.active(b), ent.active(a)), (True, False, True))

    def test_concurrent_requests_make_one_lookup(self) -> None:
        import threading as _threading

        calls, gate = [], _threading.Event()

        def lookup(ws: uuid.UUID) -> bool:
            calls.append(ws)
            gate.wait(1)
            return True

        ent = Entitlement("enforce", {"finance-api": CEILING}, lookup)
        ws = uuid.uuid4()
        threads = [_threading.Thread(target=ent.active, args=(ws,)) for _ in range(5)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)

    def test_answers_are_cached(self) -> None:
        now = [100.0]
        self.client = self.paid_client(clock=lambda: now[0])
        for _ in range(3):
            self.exchange(self.id_token(), audience="finance-api")
        self.assertEqual(len(self.asked), 1)
        now[0] += 6  # "no pass" is kept 5 s only
        self.exchange(self.id_token(), audience="finance-api")
        self.assertEqual(len(self.asked), 2)

    def test_a_ceiling_must_name_a_bundle_audience_and_hold_only_read_or_delete(self) -> None:
        with self.assertRaises(ValueError):
            Entitlement("enforce", {"finance-api": frozenset({"transactions.transaction:write"})}, lambda ws: True)
        ent = Entitlement("enforce", {"tasks-api": frozenset({"tasks.task:read"})}, lambda ws: True)
        with self.assertRaises(ValueError):
            create_app(
                connect=self.db.connect,
                platform_verifier=TokenVerifier(StaticKey(self.platform_public), PLATFORM_ISSUER),
                signing_key=SigningKey(self.signing_private), issuer=ISSUER,
                google=GoogleVerifier(StaticKey(self.google_public), frozenset({GOOGLE_CLIENT})),
                token_audiences=frozenset({"tasks-api"}), entitlement=ent)


class OverlapTest(Helpers):
    def test_another_bundle_with_a_paid_member_is_refused(self) -> None:
        ent = Entitlement("enforce", {"finance-api": CEILING}, lambda ws: True)
        with self.assertRaises(ValueError):
            create_app(
                connect=self.db.connect,
                platform_verifier=TokenVerifier(StaticKey(self.platform_public), PLATFORM_ISSUER),
                signing_key=SigningKey(self.signing_private), issuer=ISSUER,
                google=GoogleVerifier(StaticKey(self.google_public), frozenset({GOOGLE_CLIENT})),
                token_audiences=frozenset({"tasks-api", "transactions-api", "tax-api", "finance-api"}),
                cell_audiences=frozenset({"transactions-api"}),
                audience_bundles={"finance-api": frozenset({"transactions-api", "tax-api"}),
                                  "tasks-api": frozenset({"transactions-api"})},
                entitlement=ent)


class FinanceLookupTest(Helpers):
    def test_asks_finance_api_with_a_short_self_signed_token(self) -> None:
        subject, ws, seen = uuid.uuid4(), uuid.uuid4(), []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"active": True, "expires_at": None, "mode": "enforce"})

        lookup = finance_lookup("https://finance.example", subject, SigningKey(self.signing_private), ISSUER,
                                transport=httpx.MockTransport(handler))
        self.assertTrue(lookup(ws))
        self.assertEqual(str(seen[0].url), "https://finance.example/v1/internal/entitlement")
        token = seen[0].headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, self.signing_public, algorithms=["RS256"], audience="finance-api", issuer=ISSUER)
        self.assertEqual((claims["sub"], claims["ws"], claims["scope"]), (str(subject), str(ws), SCOPE))
        self.assertLessEqual(claims["exp"] - claims["iat"], 60)

    def test_a_refusal_is_a_misconfiguration(self) -> None:
        from platform_identity.entitlement import Misconfigured

        for status in (401, 403, 404):
            lookup = finance_lookup("https://finance.example", uuid.uuid4(), SigningKey(self.signing_private),
                                    ISSUER, transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s)))
            with self.subTest(status=status), self.assertRaises(Misconfigured):
                lookup(uuid.uuid4())

    def test_anything_but_a_clear_answer_raises(self) -> None:
        for response in (httpx.Response(503, json={}), httpx.Response(200, json={"active": "yes"})):
            lookup = finance_lookup("https://finance.example", uuid.uuid4(), SigningKey(self.signing_private),
                                    ISSUER, transport=httpx.MockTransport(lambda r, resp=response: resp))
            with self.subTest(status=response.status_code), self.assertRaises(RuntimeError):
                lookup(uuid.uuid4())
        with self.assertRaises(ValueError):
            finance_lookup("http://finance.example", uuid.uuid4(), SigningKey(self.signing_private), ISSUER)


if __name__ == "__main__":
    unittest.main()


class EntitlementConfigTest(unittest.TestCase):
    def test_reads_the_entitlement_settings(self) -> None:
        import json as _json

        from platform_identity.config import Config, ConfigError

        private, public = base.key_pair()
        env = {"IDENTITY_DATABASE_URL": "postgresql://x", "IDENTITY_SIGNING_KEY_PEM": private,
               "IDENTITY_ISSUER": ISSUER, "IDENTITY_PLATFORM_ISSUER": PLATFORM_ISSUER,
               "IDENTITY_PLATFORM_PUBLIC_KEY_PEM": public,
               "IDENTITY_TOKEN_AUDIENCES": "finance-api,transactions-api,tax-api",
               "IDENTITY_CELL_AUDIENCES": "transactions-api,tax-api",
               "IDENTITY_AUDIENCE_BUNDLES_JSON": _json.dumps({"finance-api": ["transactions-api", "tax-api",
                                                                              "platform-api"]})}
        self.assertEqual(Config.from_env(env).entitlement_mode, "off")
        subject = str(uuid.uuid4())
        good = {**env, "IDENTITY_ENTITLEMENT_MODE": "enforce", "IDENTITY_ENTITLEMENT_URL": "https://finance.example",
                "IDENTITY_ENTITLEMENT_SUBJECT": subject,
                "IDENTITY_ENTITLEMENT_CEILINGS_JSON": _json.dumps({"finance-api": sorted(CEILING)})}
        cfg = Config.from_env(good)
        self.assertEqual((cfg.entitlement_mode, str(cfg.entitlement_subject)), ("enforce", subject))
        self.assertEqual(cfg.entitlement_ceilings["finance-api"], CEILING)
        for name, value in (("IDENTITY_ENTITLEMENT_MODE", "on"), ("IDENTITY_ENTITLEMENT_URL", "http://x.example"),
                            ("IDENTITY_ENTITLEMENT_SUBJECT", "nope"),
                            ("IDENTITY_ENTITLEMENT_CEILINGS_JSON", _json.dumps({"tax-api": ["tax.tax-item:read"]})),
                            ("IDENTITY_ENTITLEMENT_CEILINGS_JSON",
                             _json.dumps({"finance-api": ["transactions.transaction:write"]})),
                            ("IDENTITY_ENTITLEMENT_CEILINGS_JSON", "{}")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                Config.from_env({**good, name: value})
