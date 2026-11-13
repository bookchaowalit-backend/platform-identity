"""SPEC-007: one `token.issue_failed` stdout line per refused /v1/token request, with nothing secret in it."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from typing import Any

try:
    from fastapi.testclient import TestClient

    from platform_identity.app import RateLimiter
    from platform_identity.failure_event import FailureEvents, hash_key_from
except ImportError:  # pragma: no cover - dependencies absent
    TestClient = None  # type: ignore[assignment,misc]

from service.test_service_clients import ServiceClientTest

KEY = b"k" * 32


@unittest.skipIf(TestClient is None, "service dependencies are not installed")
class FailureEventTest(ServiceClientTest):
    """Reuses the service client fixture; every test here reads the lines the app wrote to stdout."""

    def make_client(self, **overrides: Any) -> TestClient:
        return super().make_client(**({"failure_hash_key": KEY} | overrides))

    def lines(self, call) -> tuple[Any, list[dict[str, Any]]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = call()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return response, [e for e in events if e.get("event") == "token.issue_failed"]

    def test_a_forged_assertion_logs_invalid_client_for_the_registered_name(self) -> None:
        forged = self.assertion(self.other_private)
        response, events = self.lines(lambda: self.request(forged))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["grant_type"], e["reason"], e["level"]), ("client_credentials", "invalid_client", "warning"))
        self.assertRegex(e["client_hash"], r"^[0-9a-f]{16}$")
        self.assertEqual(set(e), {"event", "level", "grant_type", "reason", "client_hash"})
        self.assertNotIn(forged, json.dumps(events))
        self.assertNotIn("ai-gateway", json.dumps(events))

    def test_an_unregistered_client_name_is_never_logged_or_hashed(self) -> None:
        _, events = self.lines(lambda: self.request(self.assertion(iss="made-up-client", sub="made-up-client")))
        self.assertEqual(events[0]["client_hash"], "")

    def test_scope_and_target_refusals_carry_their_reason(self) -> None:
        _, scope = self.lines(lambda: self.request(scope="data.quota:write"))
        _, target = self.lines(lambda: self.request(audience="tasks-api"))
        self.assertEqual(scope[0]["reason"], "invalid_scope")
        self.assertEqual(target[0]["reason"], "invalid_target")
        self.assertEqual(scope[0]["client_hash"], target[0]["client_hash"])  # same client, same hash

    def test_a_token_exchange_with_a_bad_subject_token_logs_invalid_grant(self) -> None:
        form = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "audience": "tasks-api",
                "subject_token": "not-a-jwt", "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
                "workspace": str(self.ws)}
        response, events = self.lines(lambda: self.client.post("/v1/token", data=form))
        self.assertEqual(response.status_code, 400)
        self.assertEqual((events[0]["grant_type"], events[0]["reason"], events[0]["client_hash"]),
                         ("token_exchange", "invalid_grant", ""))
        self.assertNotIn("not-a-jwt", json.dumps(events))

    def test_rate_limited_and_malformed_requests_are_reported(self) -> None:
        self.client = self.make_client(rate_limiter=RateLimiter(0))
        response, limited = self.lines(lambda: self.request())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(limited[0]["reason"], "rate_limited")
        self.client = self.make_client()
        response, malformed = self.lines(lambda: self.client.post("/v1/token", data={"grant_type": "x"}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual((malformed[0]["reason"], malformed[0]["grant_type"]), ("invalid_request", "other"))

    def test_an_issued_token_logs_no_failure(self) -> None:
        response, events = self.lines(lambda: self.request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [])

    def test_an_unknown_grant_name_is_not_echoed(self) -> None:
        _, events = self.lines(lambda: self.client.post("/v1/token", data={"grant_type": "evil<script>",
                                                                          "audience": "tasks-api"}))
        self.assertEqual(events[0]["grant_type"], "other")
        self.assertEqual(events[0]["reason"], "unsupported_grant_type")


@unittest.skipIf(TestClient is None, "service dependencies are not installed")
class UnitTest(unittest.TestCase):
    def test_outages_are_not_refusals(self) -> None:
        written: list[str] = []
        FailureEvents(KEY, {"a-b"}, written.append).emit(status=503, error="temporarily_unavailable", grant_type=None)
        self.assertEqual(written, [])

    def test_a_failing_writer_never_breaks_the_answer(self) -> None:
        def broken(_: str) -> None:
            raise OSError("closed")
        FailureEvents(KEY, set(), broken).emit(status=400, error="invalid_grant", grant_type=None)

    def test_the_hash_key_is_separated_from_its_secret(self) -> None:
        self.assertNotEqual(hash_key_from("pem"), b"pem")
        self.assertEqual(hash_key_from("pem"), hash_key_from("pem"))


# The base class is collected in its own module only; here its tests run again with a hash key set,
# which shows the failure log changes no answer.
del ServiceClientTest

if __name__ == "__main__":
    unittest.main()
