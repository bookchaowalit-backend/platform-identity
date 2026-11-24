# Verify recipe (platform-identity)

The `verify` skill follows this file. The checks are the ones CI runs.

## Checks

```bash
uv sync --frozen --all-extras
uv pip install "mypy==2.4.0" && uv run mypy
uv run python scripts/check.py && uv run python scripts/check_schema_pin.py \
  && uv run python -m unittest discover -s tests -v
# admission tests, only with a throwaway Postgres you started yourself:
# PLATFORM_TEST_DSN=postgresql://localhost:5433/identity_scratch uv run python -m pytest -q runtime_tests
```

Service tests use a fake database. No coverage gate: changed logic needs its own test. Say in the
report whether `runtime_tests` ran or were skipped (no disposable DSN).

## End to end

Start `identity-api` locally with a throwaway signing key and a local database, then:
`curl -fsS localhost:8080/health`, `/readyz`, `/.well-known/jwks.json`. For token or decide changes,
show one token that must be issued and one that must be refused, with the claims (no key material).

## Never

- `identity-rate-limit-migrate` / `python -m platform_identity.rate_limit` against any non-disposable DSN.
- A production deploy from a working tree (production deploys are a manual `git archive main`).
- Real keys from Infisical in a local run.
