# platform-identity

`Identity` platform boundary for the Book Platform portfolio.

## Scope

This repository owns the `identity` capability: authentication, tenant, membership, RBAC, API keys.
The current implementation lives in `platform-empire`
(`services/platform/platform_api/routes.py`, `src/empire_platform/jwks.py`). This checkout is a local scaffold; it contains no production
provider integration, database writer, credential, or customer payload.

## Boundary

- Owner: `bookchaowalit-backend`
- Repository: `platform-identity`
- Target remote: `https://github.com/bookchaowalit-backend/platform-identity.git` (published; runtime not activated)
- Contract: `book-platform.contract.v1`
- Status: `scaffolded`
- Data owner: the platform boundary identified in `contract.json`

The platform communicates through versioned API or event contracts. Consumers
must not import another platform's database, migration, or private runtime
module. `solo-empire` remains the control plane and compatibility adapter until
parity and rollback evidence permit a cutover.

## Local verification

Run from this repository (Python 3.11+, no third-party packages):

```bash
bash scripts/check.sh
```

The check validates `contract.json` against
[`schema/book-platform.contract.v1.schema.json`](schema/book-platform.contract.v1.schema.json),
confirms that the README and [`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md)
agree with the contract, and runs the unit tests in `tests/`. GitHub Actions
runs the same command on every push and pull request
(`.github/workflows/check.yml`).

`scripts/check_schema_pin.py` fails when the vendored schema no longer
matches the sha256 pinned in `schema/book-platform.contract.v1.schema.json.sha256`
(the canonical copy lives in `bookchaowalit-backend-core/contracts/`; change it
there first, then copy the schema and its pin here). Pass
`--canonical ../platform-backend-core` to also compare with a local
checkout. `python3 scripts/check_registry_alignment.py --solo-empire ../solo-empire`
compares this contract with the parent platform registry (local only; read-only).

The check validates repository shape and contract metadata only. It does not
claim deployment, provider connectivity, data migration, or production
readiness.

## Service (migration step 3: shadow)

`src/platform_identity/` is the identity service (Python, FastAPI). In step 3
it runs beside `platform-empire` without taking over anything:

- `POST /v1/authz/decide` takes the same body and platform tokens as the
  platform endpoint and calls the same `platform.authorize` as database role
  `identity_api`; in `shadow` mode the transaction is rolled back, so nothing
  is recorded twice. `platform-empire` mirrors each answered decision here when
  its `IDENTITY_SHADOW_URL` is set and logs `identity_shadow outcome=match|mismatch`.
- `POST /v1/token` exchanges a Google id_token (owner decision D3: Google
  proves who a person is; this service stores no password) for an access token
  bound to one audience and one workspace, signed with this service's own key.
  No cell trusts that key or issuer until the cutover (step 6).
- `GET /.well-known/jwks.json` and `/.well-known/openid-configuration`.

Staged replicas can explicitly opt into shared PostgreSQL token admission and
server-owned RBAC scope mappings. Defaults retain local limiting and the current
shadow authorization/issuer profile. [`docs/SHARED-RATE-LIMIT.md`](docs/SHARED-RATE-LIMIT.md)
documents configuration, separate migration/runtime roles, trusted proxies,
readiness, tests and activation gates. No migration runs during startup.

Configuration is listed in [`src/platform_identity/config.py`](src/platform_identity/config.py);
secrets (database URL, signing key) come only from the runtime environment.

```bash
uv sync --frozen --all-extras                # or: python -m pip install -e ".[dev]"
python -m unittest discover -s tests      # contract checks + service tests
identity-api                              # needs the IDENTITY_* variables
```

Railway also sets `SENTRY_DSN` (error reporting; unset = off, see
`src/platform_identity/_sentry.py`) and `SENTRY_ENVIRONMENT`.

The parity test against the real decision point lives in `platform-empire`
(`tests/test_identity_shadow.py`), which runs this service on its test database.

## Contract surface

Dependencies: none. The interfaces this boundary owns or consumes, with
their auth, idempotency and error behaviour, are listed in
[`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md). Planned work is tracked
in [`docs/UPGRADE-PLAN.md`](docs/UPGRADE-PLAN.md). The target service design and migration order
are in [`docs/DESIGN-2026-10-13.md`](docs/DESIGN-2026-10-13.md) (draft, pending owner review).

## Migration gate

Before activating a remote or changing a consumer, add sanitized fixtures for
success, duplicate delivery, timeout and provider failure; prove tenant/privacy
isolation; compare the old and new contract; and rehearse rollback on a
disposable state store. Record the evidence in the parent platform registry.
