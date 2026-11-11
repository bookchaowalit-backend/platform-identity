# SPEC-005 — Paid audiences: without a current pass, a read/erase scope ceiling

Standard: ADR-0004. Approved by the owner 2026-11-12 ("ทำไปเลย เราอนุมัติทั้งหมดหลังจากนี้"). Pairs with
finance-api SPEC-006 (`GET /v1/internal/entitlement`).

## Problem
A `finance-api` bundle token also carries the ten finance cell audiences, so a caller can skip finance-api's pass
gate (finance-api SPEC-005) by calling the cells through the gateway.

## Decision
The platform PDP (`/v1/authz/decide`, `/v1/executions`) refuses an action outside a token's `scope`
(`scope_insufficient`). For an audience listed in `IDENTITY_ENTITLEMENT_CEILINGS_JSON`, identity asks finance-api
whether the workspace has a current pass before signing. A paid audience is the bundle key **and every member**
(a cell audience asked for on its own gets the same ceiling, so the cells cannot be reached around the gate):

| Answer | `enforce` | `shadow` | `off` |
|---|---|---|---|
| current pass | token as before | as before | not asked |
| no pass | `scope` = the ceiling (∩ requested scopes; none left → 400 `invalid_scope` / `pass_required`) | as before, logs `pass missing` | not asked |
| cannot tell (timeout, connection error, 5xx) | as before, logs `entitlement unknown` (fail open) | same | — |
| finance-api refused the lookup (401/403/404: misconfiguration) | **ceiling**, logs an error | logs | — |

The ceiling holds only `:read` and `:delete` permissions, so without a pass people can still read, export and erase
their own data (PDPA), but write nothing. Answers are cached per workspace: 60 s (pass), 5 s (no pass), 15 s
(unknown); the lookup waits about 3 s (1 s to connect) and runs once per workspace at a time. A lapsed pass ends
write access within about 7 minutes (finance-api cache 60 s + identity cache 60 s + bundle token 300 s; a cell
token lives up to its own TTL). The ceiling holds at most 48 permissions (it rides in every no-pass token).
Residual: tokens of the main issuer (empire-platform, owner automations) and registered service clients never pass
through this check; they are first-party.

## Lookup
identity signs its own 60-second token: `aud finance-api`, `sub = IDENTITY_ENTITLEMENT_SUBJECT`, `ws` = the
workspace, `scope finance-api.entitlement:read`, and sends it to `IDENTITY_ENTITLEMENT_URL/v1/internal/entitlement`.
finance-api accepts that subject on that route only (`FINANCE_API_ENTITLEMENT_SUBJECT`).

## Configuration
`IDENTITY_ENTITLEMENT_MODE` (`off` default), `IDENTITY_ENTITLEMENT_URL` (https origin),
`IDENTITY_ENTITLEMENT_SUBJECT` (UUID), `IDENTITY_ENTITLEMENT_CEILINGS_JSON` (`{"finance-api": [...]}`: bundle
audiences only, 1–128 `:read`/`:delete` permissions). Invalid → identity does not start. Rollback: mode `off`.

## Verification
`tests/service/test_entitlement.py` (ceiling, intersection, refusal, shadow, off, fail open, cache, lookup token,
configuration).
