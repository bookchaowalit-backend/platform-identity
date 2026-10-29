# Contract surface: platform-identity

This page documents the API, table and configuration surface that
`platform-identity` owns or will own. The machine-readable list is `interfaces` in
[`contract.json`](../contract.json); `scripts/check.py` fails when an interface
listed there is missing from this page.

- Platform: `identity` (contract `book-platform.contract.v1`, API `v1`)
- Status: `scaffolded`; source status: `adjacent-domain-repository`
- Current implementation: `platform-empire` (`services/platform/platform_api/routes.py`,
  `src/empire_platform/jwks.py`, platform migrations `0001` and `0003`)
- Depends on: none
- Target design and migration order: [`DESIGN-2026-10-13.md`](DESIGN-2026-10-13.md)

Interfaces marked `pilot` are served by `platform-empire` today; this repository
does not serve them yet. They are the compatibility surface the migration must
preserve. `planned` interfaces do not exist yet. The `source` column is a path in
`platform-empire`.

## Interfaces

| ID | Kind | Direction | Interface | Auth | Source | Status |
|---|---|---|---|---|---|---|
| `identity.http.jwks` | http | provides | `GET /.well-known/jwks.json`: RS256 verification keys, `kid` = RFC 7638 thumbprint | public | `src/empire_platform/jwks.py` | pilot |
| `identity.http.authz-decide` | http | provides | `POST /v1/authz/decide`: recorded allow / deny / require_approval for the caller's workspace | bearer JWT, audience `platform-api` | `services/platform/platform_api/routes.py` | pilot |
| `identity.table.workspace-rbac` | table | provides | identities, workspaces, memberships, roles, role_permissions, membership_roles, policies, delegations | forced RLS on `app.workspace_id` | `services/platform/migrations/0001_platform_core.sql` | pilot |
| `identity.http.token-exchange` | http | provides | `POST /v1/token`: RFC 8693-shaped exchange for an audience-bound access token | subject token | `src/empire_platform/token_exchange.py` | planned |
| `identity.http.workspace-admin` | http | provides | `/v1/workspaces`, members, roles, API keys and delegations | bearer JWT with `identity.*:admin` | none | planned |

## Behaviour notes

- `POST /v1/token` also takes `grant_type=client_credentials` with a
  `private_key_jwt` client assertion for registered service clients; see
  [SERVICE-CLIENTS.md](SERVICE-CLIENTS.md). Those tokens add `client_id` and
  always carry `scope`.
- Access tokens are RS256 JWTs with `iss`, `aud`, `sub`, `ws`, `iat`, `exp`
  and `jti` (plus `del` for delegated tokens). The issuer stays
  `empire-platform` during the transition.
- Cells verify tokens against the JWK Set when `<ENV>_JWT_JWKS_URL` is set
  (cached 10 minutes; an unknown `kid` refetches at most once a minute) and
  fall back to the pinned `<ENV>_JWT_PUBLIC_KEY_PEM`.
- The decision endpoint takes `action` (`<domain>.<resource>:<action>`),
  `resource_type`, `resource_id` and an optional `approval_id`, and returns
  `decision`, `reason` and `decision_id`.

## Migration gates

- tenant isolation
- credential rotation
- session parity

## Out of scope

No production provider integration, credential, database writer or customer
payload lives in this repository. Record parity, privacy and rollback evidence
in the parent platform registry before any cutover.
