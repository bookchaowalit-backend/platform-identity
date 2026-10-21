# platform-identity — Product Brief

Status on 2026-10-22: **online in shadow mode** (migration step 3). It answers requests but nothing depends on its
answers yet. This brief describes what exists in this repository; plans are marked as plans.

## Who it is for

- **Today:** `platform-empire`, the owner's platform API, which mirrors each authorization decision to this service
  (when `IDENTITY_SHADOW_URL` is set) and logs whether the answers match.
- **Later (planned, step 6 cutover):** the domain cells that today trust `platform-empire` tokens. They will verify
  tokens issued here once the owner approves the cutover.

The only human user is the owner (single-owner platform). No customer data is stored here.

## Problem

Authorization decisions and token issuing live inside `platform-empire`. Moving them to a separate identity service
needs proof that the new service gives the same answers before anything relies on it. This service exists to
collect that proof and then take over.

## What exists

| Capability | Endpoint | State |
|---|---|---|
| Authorization decision (same body, tokens and `platform.authorize` function as the platform endpoint) | `POST /v1/authz/decide` | live; `shadow` mode rolls the transaction back so nothing is recorded twice |
| Token exchange: Google id_token → access token bound to one audience and one workspace, signed with this service's key | `POST /v1/token` | live; no cell trusts the key yet |
| Key discovery | `GET /.well-known/jwks.json`, `GET /.well-known/openid-configuration` | live |
| Health | `GET /health` | live; Railway health check |
| Error reporting | Sentry when `SENTRY_DSN` is set (errors only, scrubbed) | live |
| Contract and drift checks | `scripts/check.py`, `check_schema_pin.py`, `check_registry_alignment.py` | in CI |

- Host: Railway project service `identity-api` (Singapore), URL in the platform registry. Deployed by hand; the Railway
  GitHub app is not installed on org `bookchaowalit-backend`, so merging to `main` does not deploy.
- Data: PostgreSQL, role `identity_api`, via `IDENTITY_DATABASE_URL`. Passwords are never stored (owner decision D3).

## Non-goals

- No user interface. No password login. No billing.
- No API keys, tenant administration or RBAC editing yet, although the boundary owns them in the contract.

## Success measures

| Measure | How it is read | Target | Status |
|---|---|---|---|
| Shadow parity | `identity_shadow outcome=match` / all outcomes in `platform-empire` logs | 100% match over 14 days before cutover | log line exists; no report yet |
| Availability | uptime monitor probe of `/health` | ≥ 99.5% monthly | probe exists in `platform-uptime-worker` targets |
| Error events | Sentry issues for this service | no unresolved error before cutover | Sentry live |

## Roadmap

| Step | Outcome | State |
|---|---|---|
| 3 Shadow | Decisions mirrored and compared | running |
| 4–5 | Parity report, rollback rehearsal on a disposable database | planned [ต้องยืนยัน] |
| 6 Cutover | Cells trust this issuer; `platform-empire` stops issuing | needs owner approval |

Design and order: [`docs/DESIGN-2026-10-13.md`](docs/DESIGN-2026-10-13.md). Contract surface:
[`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md). Note: `DOCPACK/00-product-card.md` (2026-10-08) predates the
service code and still says there is no runtime.
