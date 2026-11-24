# Verify recipe (platform-identity)

The `verify` skill follows this file. The checks are the ones CI runs.

## Checks

```bash
set -e
# 3.12: the postgres-test extra (platform-backend-core) installs only on 3.12+; CI's service job
# also runs 3.11 (`uv sync --frozen --all-extras --python 3.11` for a version-specific change).
uv sync --frozen --all-extras --python 3.12
uvx ruff@0.17.1 check .          # CI lint (format-check is off; rules in pyproject.toml)
uv pip install "mypy==2.4.0" && uv run --no-sync mypy
uv run --no-sync python scripts/check.py && uv run --no-sync python scripts/check_schema_pin.py \
  && uv run --no-sync python -m unittest discover -s tests -v
# admission tests (CI: Python 3.12 job), only with a throwaway Postgres you started yourself:
# PLATFORM_TEST_DSN=postgresql://localhost:5433/identity_scratch uv run --no-sync python -m pytest -q runtime_tests
```

Service tests use a fake database. No coverage gate: changed logic needs its own test. Say in the
report whether `runtime_tests` ran or were skipped (no disposable DSN).

## End to end

Start `identity-api` locally with a throwaway signing key and a local database, then:
`curl -fsS localhost:8080/health`, `/readyz`, `/.well-known/jwks.json`. For token changes, show one
token that must be issued and one that must be refused, with the claims (no key material). For
`/v1/authz/decide` changes, call it directly with a local token and show both answers: one action
that must return `{"decision": "allow"}` and one that must not (`deny`, or 403 `scope_insufficient`
for an action outside the token's scopes). Issuing a token does not exercise the decide handler.

## Never

- `identity-rate-limit-migrate` / `python -m platform_identity.rate_limit` against any non-disposable DSN.
- A production deploy from a working tree (production deploys are a manual `git archive main`).
- Real keys from Infisical in a local run.
