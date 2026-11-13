# SPEC-007 — `token.issue_failed`: one log line per refused token request

Status: built 2026-11-13. Consumer: `platform-security-signals` rule SIG-06 (its SPEC-001 R9).

## Why

Identity logged issued tokens but not refused ones, so a brute force against `/v1/token` (forged client
assertions, stolen or replayed Google id_tokens, scope probing) left no countable trace. The security
signals job reads structured stdout lines from Railway logs, the same way it reads the gateway's
`request.completed`.

## What

Every `/v1/token` answer in 400..499 writes exactly one JSON line to stdout:

```json
{"event":"token.issue_failed","level":"warning","grant_type":"client_credentials","reason":"invalid_client","client_hash":"3f0c…16 hex"}
```

| Field | Values |
|---|---|
| `grant_type` | `client_credentials`, `token_exchange`, or `other` (the submitted name is never echoed) |
| `reason` | the OAuth error: `invalid_request`, `invalid_client`, `invalid_grant`, `invalid_scope`, `invalid_target`, `unauthorized_client`, `unsupported_grant_type`; the rate limiter's `slow_down` (429) is `rate_limited` |
| `client_hash` | first 16 hex of HMAC-SHA256 of the claimed client id, **only when that id is a registered service client** (form `client_id`, else the unverified assertion `iss`); `""` otherwise, and always `""` for token exchange |

Never in the line: subject token, client assertion, scope list, audience, workspace, IP address, an
unregistered client name. 5xx answers (outages) are not refusals and write nothing.

The HMAC key is derived from `IDENTITY_SIGNING_KEY_PEM` with a fixed label
(`failure_event.hash_key_from`), so there is no new secret to manage and the hash says nothing about
the signing key. Rotating the signing key changes every client hash once; SIG-06 then sees each failing
client as new for one alert.

## How

`/v1/token` stores `(grant_type, client_id, client_assertion)` on `request.state`; the shared
HTTPException handler emits the line for `/v1/token` errors in the OAuth shape, and the validation
handler emits `invalid_request`. Nothing changes in any answer, and a failing stdout write is ignored.

## Tests

`tests/service/test_failure_event.py`: forged assertion → `invalid_client` with a hash and no client
name; unregistered name → empty hash; scope and target refusals; token exchange `invalid_grant` without
the token; 429 → `rate_limited`; malformed form → `invalid_request` / `other`; issued token → no line;
unknown grant name not echoed; 5xx not reported; a broken writer changes nothing. The service client
suite runs again with the hash key set.
