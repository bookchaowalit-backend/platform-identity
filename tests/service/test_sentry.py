#!/usr/bin/env python3
"""Sentry is off without SENTRY_DSN; with it: errors only, no PII, locals, bodies or secrets."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

try:
    from platform_identity import _sentry
except ImportError:  # pragma: no cover - service not installed
    _sentry = None


def _env_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


@unittest.skipIf(_sentry is None, "platform_identity is not installed")
class SentryTests(unittest.TestCase):
    def test_no_dsn_is_a_noop(self):
        calls = []
        fake = type("Fake", (), {"init": staticmethod(lambda **kw: calls.append(kw))})
        with (
            mock.patch.dict(os.environ, _env_without("SENTRY_DSN"), clear=True),
            mock.patch.dict(sys.modules, {"sentry_sdk": fake}),
        ):
            _sentry.init_sentry()
        self.assertEqual(calls, [])

    @unittest.skipIf(
        importlib.util.find_spec("sentry_sdk") is None, "sentry-sdk not installed"
    )
    def test_dsn_initialises_errors_only_without_pii(self):
        sentry_sdk = importlib.import_module("sentry_sdk")
        calls, tags = [], {}

        env = _env_without("SENTRY_ENVIRONMENT")
        env.update(
            SENTRY_DSN="https://public@o0.ingest.sentry.io/0",
            RAILWAY_SERVICE_NAME="identity-api",
        )
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(sentry_sdk, "init", lambda **kw: calls.append(kw)),
            mock.patch.object(
                sentry_sdk, "set_tag", lambda key, value: tags.__setitem__(key, value)
            ),
            redirect_stderr(err),
        ):
            _sentry.init_sentry()
        self.assertEqual(calls[0]["environment"], "production")
        self.assertIsNone(calls[0]["traces_sample_rate"])
        self.assertIs(calls[0]["send_default_pii"], False)
        self.assertIs(calls[0]["include_local_variables"], False)
        self.assertEqual(calls[0]["max_request_body_size"], "never")
        self.assertIs(calls[0]["before_send"], _sentry._scrub_event)
        (logging_integration,) = calls[0]["integrations"]
        self.assertIsNone(logging_integration._handler)
        self.assertIsNone(logging_integration._breadcrumb_handler)
        self.assertIsNone(logging_integration._sentry_logs_handler)
        self.assertEqual(tags, {"service": "identity-api"})
        self.assertIn("sentry: enabled for identity-api", err.getvalue())

    @unittest.skipIf(
        importlib.util.find_spec("sentry_sdk") is None, "sentry-sdk not installed"
    )
    def test_real_sdk_installed(self):
        self.assertIsNotNone(importlib.import_module("sentry_sdk"))

    def test_scrub_removes_url_passwords_tokens_and_jwts(self):
        text = (
            "connect postgresql://identity:"
            "s3cr%40t"  # split so secret scanners do not read a synthetic URL as a credential
            "@ep-x.neon.tech/identity failed; "
            "password=hunter2 Authorization: Bearer abc.def-ghi "
            "eyJhbGciOiJSUzI1NiJ9"
            "."
            "eyJzdWIiOiJvd25lciJ9"
            "."
            "c2lnbmF0dXJl"
        )  # split: not a secret
        out = _sentry.scrub(text)
        for secret in ("s3cr%40t", "hunter2", "abc.def-ghi", "eyJhbGciOiJSUzI1NiJ9"):
            self.assertNotIn(secret, out)
        self.assertIn("postgresql://identity:***@ep-x.neon.tech/identity", out)

    def test_scrub_event_drops_request_details_and_extra(self):
        event = {
            "exception": {"values": [{"value": "bad url postgres://u:pw@h/db"}]},
            "request": {
                "method": "POST",
                "url": "https://x/api",
                "data": {"notes": "x"},
                "headers": {"authorization": "Bearer t"},
                "query_string": "token=t",
            },
            "breadcrumbs": {
                "values": [
                    {
                        "message": "GET with Bearer abc",
                        "data": {"url": "https://x?token=t"},
                    }
                ]
            },
            "extra": {"sys.argv": ["--token", "t"]},
        }
        out = _sentry._scrub_event(event, {})
        self.assertEqual(
            out["exception"]["values"][0]["value"], "bad url postgres://u:***@h/db"
        )
        self.assertEqual(out["request"], {"method": "POST"})
        self.assertNotIn("data", out["breadcrumbs"]["values"][0])
        self.assertNotIn("abc", out["breadcrumbs"]["values"][0]["message"])
        self.assertNotIn("extra", out)

    def test_capture_is_a_noop_without_dsn(self):
        with (
            mock.patch.dict(os.environ, _env_without("SENTRY_DSN"), clear=True),
            mock.patch.dict(sys.modules, {"sentry_sdk": None}),
        ):
            _sentry.capture(RuntimeError("x"))  # importing sentry_sdk here would raise

    def test_scrub_event_keeps_only_scrubbed_formatted_log_text(self):
        token = "opaque" + "-token-value"
        event = {
            "logentry": {
                "message": "Authorization: Bearer %s",
                "params": [token],
                "formatted": "Authorization: Bearer " + token,
            }
        }
        out = _sentry._scrub_event(event, {})
        self.assertEqual(out["logentry"], {"message": "Authorization: Bearer ***"})
        self.assertNotIn(token, str(out))

    def test_scrub_event_drops_dynamic_sampling_context(self):
        event = {
            "contexts": {
                "trace": {
                    "trace_id": "t",
                    "dynamic_sampling_context": {"user_id": "alice"},
                }
            }
        }
        out = _sentry._scrub_event(event, {})
        self.assertEqual(out["contexts"]["trace"], {"trace_id": "t"})


if __name__ == "__main__":
    unittest.main()
