# Opt-in shared token-exchange admission

Correlation: `SHARED-INFRA-2026-10-15`. Scope: staged identity serving resources
and token-exchange rate admission. The existing identity design's shadow parity
and rollback gates remain release prerequisites.

## Implementation plan

- [x] Add failing tests for explicit configuration, staged proxy trust, dependency
  failure, nonprivileged real PostgreSQL admission and multi-process limits.
- [x] Add an identity-owned additive SQL migration and explicit admin CLI;
  implement global-first atomic admission using database time and hashed keys.
- [x] Wire optional limiter and bounded pools into the existing application,
  preserving default local mode and the authorization/issuer migration gates.
- [x] Verify the full owning suite, contract checks and wheel; commit only this
  bounded implementation for independent review. No deployment or cutover.

Review focus: direct clients cannot select their limiter key through forwarded
headers in staged mode; denied per-key attempts still consume global admission;
missing schema/grants and database outages fail closed without exposing details;
all processes share the same database-clock windows; shutdown closes bounded
pools and migrations never run during app startup.


## Configuration and serving limits

All variables are injected by the operator; this service never discovers local
credentials. `IDENTITY_RATE_LIMIT_MODE=local` remains the default. Only explicit
`postgres` mode creates the shared limiter; it requires a separately configured
`IDENTITY_RATE_LIMIT_DATABASE_URL`. No fallback to local admission occurs after
a PostgreSQL failure.

| Variable | Default | Bounds / meaning |
| --- | --- | --- |
| `IDENTITY_RATE_LIMIT_MODE` | `local` | `local` or `postgres` |
| `IDENTITY_RATE_LIMIT_DATABASE_URL` | unset | required in postgres mode; serving role only |
| `IDENTITY_RATE_LIMIT_PER_KEY` | 30 | 1..1000000 admissions per client/window |
| `IDENTITY_RATE_LIMIT_GLOBAL` | 300 | 1..1000000 attempts per window across replicas |
| `IDENTITY_RATE_LIMIT_POOL_MAX` | 4 | 1..32 limiter connections per replica |
| `IDENTITY_DB_POOL_MAX` | 8 | 1..32 primary serving connections per replica |
| `IDENTITY_DB_TIMEOUT_SECONDS` | 5 | .1..30 seconds acquisition; connection timeout rounded up |
| `IDENTITY_DB_STATEMENT_TIMEOUT_MS` | 5000 | 100..30000 ms SQL/lock/idle-transaction timeout |
| `IDENTITY_MAX_CONCURRENCY` | 64 | 1..1024 server requests/connections |
| `IDENTITY_TRUSTED_PROXIES` | empty | at most 32 explicit proxy CIDRs; no wildcard network |
| `IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON` | `{}` | optional bounded scope-to-RBAC map |

Pools have a bounded waiting queue (`4 * pool_max`), zero minimum, five-minute
idle expiration and 30-minute connection lifetime. Main uses pools for both
the primary identity connection and optional limiter. Lifespan shutdown closes
both. Budget connections as replicas times `(primary max + limiter max)` plus
migration/operational capacity; defaults use up to twelve per postgres-mode
replica. Local mode uses only the primary pool. Keep all replicas on the same
limits and mapping configuration. Server concurrency is bounded and keep-alive
is five seconds. Dependencies must still fit the database and server budget.

## Proxy policy

Postgres mode retains the actual socket peer (`proxy_headers=False`) and applies
only `IDENTITY_TRUSTED_PROXIES`. Empty trust ignores `X-Forwarded-For`. A direct
untrusted peer cannot choose its rate key. For a trusted proxy, parse the chain
from right to left and select the first untrusted address. Malformed/oversized
chains fall back to the socket peer; limits are 2048 header bytes / 32 hops.
The deployment must ensure configured proxies append or replace the header
with the verified connecting address, and must restrict direct ingress as
appropriate. Do not trust broad client networks as proxies.

The original local profile retains its last-forwarded-hop behavior for backward
compatibility. Setting the new trust list alone does not switch that legacy
profile; use explicit postgres mode for the staged policy. Programmatic callers
can pass `trusted_proxies=()` to `create_app` for direct-peer-only admission.

## Admission correctness and retention

The migration seeds one global counter row. Every attempt locks it first,
reads `clock_timestamp()` after obtaining the lock, selects a fixed 60-second
window, and then updates the global counter before the client counter in the
same transaction. Per-key rejections consume global capacity. Once the global
cap is exhausted no new client rows are created. Both SQL transaction commit
and PostgreSQL row locks are the shared correctness boundary, including across
processes. Fixed windows can admit up to two window budgets near a rollover;
this is not a sliding-window limiter.

Client keys are SHA-256 hashes with a versioned namespace; no IP or forwarded
header is stored in the table. Hashing is pseudonymization, not anonymization.
Counters expire after the following window and each attempt removes at most
100 expired client rows using an expiry index. The singleton is never removed.
No background worker or unbounded cleanup runs on the serving path. Expired
rows can remain while traffic is stopped; they are cleaned on subsequent
attempts. The current window's unique rows are bounded by the global budget.

A missing migration, checksum drift, missing grants, unsafe serving role,
closed/exhausted pool, lock timeout or database outage fails token admission
with generic OAuth `503 temporarily_unavailable`; no raw dependency error is
returned. A real budget refusal returns the existing `429 slow_down` shape.
No token is minted when admission cannot be established.

`GET /health` remains the original liveness endpoint. `GET /readyz` verifies
the primary pool and configured limiter's schema version/checksum, expected
columns, singleton, role flags and exact DML grants. Failure gives generic 503,
without exposing which dependency or its details. No DDL is executed by probes
or app startup. This does not certify identity's shadow parity or consumer trust.

## Explicit migration and grants

The migration owns only `identity_rate_limit.windows` and its version ledger.
It neither creates nor writes the `platform` identity/membership tables. Provision
a separate runtime role with `NOSUPERUSER NOBYPASSRLS`, no schema ownership and
no DDL rights. Runtime authentication and CONNECT rights are operator-owned.
Use an explicit separate migration/admin DSN with DDL/grant rights:

```bash
# Operator injects IDENTITY_RATE_LIMIT_MIGRATION_DATABASE_URL separately.
identity-rate-limit-migrate --runtime-role identity_rate_limiter
# Equivalent module command, covered by a real disposable CLI test:
python -m platform_identity.rate_limit --runtime-role identity_rate_limiter
```

The CLI applies version `0001_token_rate_limit` under a transaction advisory
lock, records its SHA-256 checksum, seeds the global row, and grants exactly:

- `USAGE` on schema `identity_rate_limit`;
- `SELECT, INSERT, UPDATE, DELETE` on `identity_rate_limit.windows`;
- `SELECT` on `identity_rate_limit.schema_migrations`.

Public grants are revoked. The runtime role cannot create admission tables or
change the migration ledger. Reapplication is idempotent; checksum drift fails.
The CLI prints a generic failure without the DSN or database exception text.
The primary identity pool continues to use its existing `identity_api` grants.
No new onboarding or production database writer is introduced.

## Explicit shared scope bridge

Shared consumers need concrete scopes such as `jobs:write`, while the existing
identity endpoint accepts canonical RBAC actions such as `tasks.task:write`.
The new map defaults to `{}`, preserving legacy scope behavior. An operator may
explicitly map a new shared scope to an existing canonical permission, for
example the structural configuration `{"jobs:write":"tasks.task:write"}`.
This is an example, not an approved production permission assignment. Choose
each mapping according to the target service's actual authorization policy.

For every requested mapped scope, identity first verifies active membership in
the requested workspace, then calls the existing `platform.authorize` using:

- actor = the active identity, workspace/resource workspace = requested workspace;
- action = configured canonical permission;
- resource type `shared-infra`, resource id = the requested shared scope;
- no delegation/approval, channel `api`.

Only `allow` permits minting. `deny` and `require_approval` return 403
`invalid_scope` without a token. Unknown shared scopes return 400. Every scope
must pass before signing; the requested shared string is minted unchanged.
These checks run inside the existing rollback-only membership transaction,
so authorization audit side effects are not committed. Existing canonical
scope requests keep their legacy behavior. The map cannot redefine canonical
scope keys, contains at most 64 mappings / 16 KiB, rejects duplicates/wildcards,
and limits each entire shared scope to 128 characters including the colon in
both environment and programmatic app configuration,
and never takes values from request bodies. Audiences remain explicitly allowed
by `IDENTITY_TOKEN_AUDIENCES`.

Before enabling a map, the operator must register/grant its canonical action
through the owning platform's existing RBAC process and verify workspace and
resource policy. This change creates no permissions, memberships, role bindings
or approval bypass. It changes no existing issuer/trust configuration and does
not authorize cells to accept the staged issuer.

## Verification and release gates

```bash
# Python >=3.12 for the optional common test fixture (runtime still >=3.11).
python -m pip install -e '.[dev,postgres-test]'
# Operator supplies PLATFORM_TEST_DSN for a disposable local admin database.
python -m pytest -q
bash scripts/check.sh
python -m pip wheel --no-deps . --wheel-dir dist
```

The optional `postgres-test` extra pins core commit
`85b066fa5e02c960f013ed39a2f0473f2b2ef451`; it is not a runtime dependency.
Publication of that reviewed core revision is a prerequisite for remote CI
installation. Local verification uses the already installed common fixture.
CI configures real PostgreSQL 16 and Python 3.12; no CI production migration or
provider credentials are configured.

The new tests first failed on absent configuration/proxy/shared-state behavior;
shared-scope tests separately failed before the bridge implementation. Local
acceptance uses Python 3.14 and rootless PostgreSQL 18 with unique disposable
`identity_test_` databases and NOSUPERUSER/NOBYPASSRLS roles. It covers two
independent processes, two app replicas, global/per-key concurrency, spoofed
forwarding, outage, missing/drifted migration, exact grants, bounded expiration,
primary pool exhaustion/privilege/shutdown, and the real migration CLI. Scope
policy tests use a synthetic Google identity and fake existing authorization
point; an allowed token is consumed by shared core verification and a scope
protected FastAPI endpoint. No Google/provider traffic occurs.

Production activation still requires independent review, publishing the pinned
core, running CI on the supported versions, provisioning roles, applying the
reviewed migration, selecting real limits/proxy CIDRs/RBAC mappings and verifying
the staged deployment. Existing migration step 3 retains its 100% shadow parity
gate through 2026-10-21 and existing rollback procedure. Issuer replacement,
consumer trust, onboarding and cell cutover remain outside this change.


Final local verification: `python -m pytest -q` passed 79 tests and 15 subtests,
with zero skips. The dependency-free `bash scripts/check.sh` passed its contract
and schema checks (13 service tests visibly skipped by the system interpreter);
the isolated interpreter separately ran all 49 unittest cases without skips.
Wheel construction and isolated wheel import, including the packaged SQL
migration, passed. One upstream Starlette/httpx deprecation warning remains.
No production database, issuer, proxy, audience or RBAC configuration changed.


Review regression: total scope lengths of 128 and 129 characters are tested
through both environment configuration and programmatic `create_app` mappings.
The 129-character cases failed before the guard; all four boundary cases pass
afterward. The accepted 128-character scope also passes core principal and
scope-dependency validation. Canonical permission validation remains unchanged.
