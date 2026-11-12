"""SPEC-006 unit checks: registry keys, tenancy settings, Google person claims, sign-up budget.

The behaviour against the database runs in platform-empire (tests/test_tenancy.py and
tests/test_identity_tenancy.py), like the existing parity tests.
"""

from __future__ import annotations

import json
import time
import unittest
import uuid

try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from platform_identity.config import ConfigError, parse_tenancy
    from platform_identity.google import GoogleVerifier
    from platform_identity.keys import StaticKey
    from platform_identity.service_clients import parse_service_clients
    from platform_identity.tenancy import HourlyLimit, TenancyConfig, email_hint
except ImportError:  # pragma: no cover - dependencies absent
    jwt = None  # type: ignore[assignment]


def pem_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


@unittest.skipIf(jwt is None, "service dependencies are not installed")
class RegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.public = pem_pair()[1]

    def entry(self, **extra):
        return {"subject": str(uuid.uuid4()), "workspace": str(uuid.uuid4()), "audiences": ["tasks-api"],
                "scopes": ["decisions.decision:read"], "public_key_pem": self.public, **extra}

    def test_existing_entries_parse_unchanged(self) -> None:
        client = parse_service_clients(json.dumps({"old-client": self.entry()}))["old-client"]
        self.assertIsNotNone(client.workspace)
        self.assertEqual((client.delegate_from, client.delegable_scopes, client.products), (None, frozenset(),
                                                                                          frozenset()))

    def test_star_client_and_delegation_keys(self) -> None:
        client = parse_service_clients(json.dumps({"commerce-api": self.entry(
            workspace="*", delegate_from="commerce-api", delegable_scopes=["decisions.decision:read"],
            products=["finance"])}))["commerce-api"]
        self.assertIsNone(client.workspace)
        self.assertEqual(client.delegate_from, "commerce-api")

    def test_bad_delegation_registrations_are_refused(self) -> None:
        bad = [
            self.entry(delegate_from="x-api", delegable_scopes=["decisions.decision:read"]),  # fixed workspace
            self.entry(workspace="*", delegate_from="x-api", delegable_scopes=["other.thing:read"]),  # ⊄ scopes
            self.entry(workspace="*", delegate_from="x-api"),  # no delegable scopes
            self.entry(unknown="field"),
        ]
        for entry in bad:
            with self.subTest(entry=sorted(entry)), self.assertRaises(ValueError):
                parse_service_clients(json.dumps({"some-client": entry}))
        shared = self.entry(workspace="*", delegate_from="x-api", delegable_scopes=["decisions.decision:read"])
        with self.assertRaises(ValueError):
            parse_service_clients(json.dumps({"one-client": shared, "two-client": {**shared,
                                                                                  "subject": str(uuid.uuid4())}}))


@unittest.skipIf(jwt is None, "service dependencies are not installed")
class TenancySettingsTest(unittest.TestCase):
    ENV = {"IDENTITY_TENANCY_MODE": "on", "IDENTITY_INVITE_KEY": "k" * 32, "IDENTITY_SIGNUP_PRODUCTS": "finance",
           "IDENTITY_SIGNUP_MODE": "invite_code", "IDENTITY_SIGNUP_CODES_SHA256": "a" * 64,
           "IDENTITY_ROLE_TEMPLATES_JSON": json.dumps({"finance": {"owner": ["identity.member:admin"],
                                                                   "member": ["decisions.decision:read"]}})}
    AUDIENCES = frozenset({"identity-api", "tasks-api"})

    def test_off_by_default(self) -> None:
        self.assertIsNone(parse_tenancy({}, {}, self.AUDIENCES, frozenset(), {}))

    def test_on_parses(self) -> None:
        config = parse_tenancy(self.ENV, {}, self.AUDIENCES, frozenset(), {})
        assert config is not None
        self.assertEqual(config.signup_mode, "invite_code")
        self.assertEqual(config.role_templates["finance"]["member"], ("decisions.decision:read",))

    def test_invalid_settings_stop_start_up(self) -> None:
        cases = {
            "IDENTITY_TENANCY_MODE": "maybe",
            "IDENTITY_INVITE_KEY": "short",
            "IDENTITY_SIGNUP_MODE": "public",
            "IDENTITY_SIGNUP_CODES_SHA256": "not-hex",
            "IDENTITY_ROLE_TEMPLATES_JSON": json.dumps({"finance": {"member": ["decisions.decision:read"]}}),
            "IDENTITY_DEFAULT_INSTALLS": json.dumps({"finance": ["missing-client"]}),
        }
        for key, value in cases.items():
            with self.subTest(key=key), self.assertRaises(ConfigError):
                parse_tenancy({**self.ENV, key: value}, {}, self.AUDIENCES, frozenset(), {})
        with self.assertRaises(ConfigError):
            parse_tenancy(self.ENV, {}, frozenset({"tasks-api"}), frozenset(), {})  # identity-api not issuable

    def test_star_clients_need_tenancy(self) -> None:
        clients = parse_service_clients(json.dumps({"worker": {
            "subject": str(uuid.uuid4()), "workspace": "*", "audiences": ["tasks-api"],
            "scopes": ["decisions.decision:read"], "public_key_pem": pem_pair()[1]}}))
        with self.assertRaises(ConfigError):
            parse_tenancy({}, clients, self.AUDIENCES, frozenset(), {})

    def test_config_checks(self) -> None:
        with self.assertRaises(ValueError):
            TenancyConfig(invite_key=b"short")
        with self.assertRaises(ValueError):
            TenancyConfig(invite_key=b"k" * 32, signup_products=frozenset({"finance"}))  # no role templates


@unittest.skipIf(jwt is None, "service dependencies are not installed")
class PersonTest(unittest.TestCase):
    def test_email_only_when_verified(self) -> None:
        private, public = pem_pair()
        verifier = GoogleVerifier(StaticKey(public), frozenset({"c"}))
        now = int(time.time())
        base = {"iss": "https://accounts.google.com", "aud": "c", "sub": "123", "iat": now, "exp": now + 60,
                "email": "Ann@Example.com", "name": " Ann "}
        verified = verifier.person(jwt.encode({**base, "email_verified": True}, private, algorithm="RS256"))
        self.assertEqual((verified.subject, verified.email, verified.name), ("google:123", "ann@example.com", "Ann"))
        unverified = verifier.person(jwt.encode({**base, "email_verified": "true"}, private, algorithm="RS256"))
        self.assertIsNone(unverified.email)

    def test_hint_and_budget(self) -> None:
        self.assertEqual(email_hint("ann@example.com"), "an…@e…")
        clock = [0.0]
        limit = HourlyLimit(2, clock=lambda: clock[0])
        self.assertEqual([limit.allow(), limit.allow(), limit.allow()], [True, True, False])
        clock[0] = 3600
        self.assertTrue(limit.allow())


if __name__ == "__main__":
    unittest.main()
