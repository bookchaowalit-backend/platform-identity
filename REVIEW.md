# Review guide (platform-identity)

The Codex Stop-hook review reads this file from the default branch, never from the change under
review. Owner-written; keep it under 16 KiB.

## What failure looks like here

Treat these as `[P0]`/`[P1]`:

- **Scope or delegation widened** in token exchange, or the chain not re-checked
  (`docs/SPEC-006-TENANCY-AND-DELEGATION.md` AC-15, AC-17, AC-18).
- **Cross-workspace access**, or not failing closed when `app.workspace_id` is unset (SPEC-006 AC-14).
- **Tokens for an inactive workspace or identity** (SPEC-006 AC-13). Exception (AC-12): an active
  human owner of a **suspended** workspace may get `identity.workspace:admin` alone, audience
  identity, to reactivate it (`app.py` `reactivation`). Widening that exception is the finding.
- **Verification weakened** for Google id_tokens or platform JWTs: algorithms, audience, required
  claims (`google.py`, `auth.py`), or `private_key_jwt` client assertions.
- **Rate limit failing open**, or trusting a spoofable `X-Forwarded-For` (`docs/SHARED-RATE-LIMIT.md`).
- **Replay**: `jti` memory is per process (`service_clients.py`); a change that assumes one replica
  or drops the check.
- **Shadow mode recording twice** (it must roll back, `app.py`).
- **Keys or real subjects committed** to this public repository.

Identity is the live issuer for the platform services (see `docs/PLATFORM-SERVICES-CUTOVER.md`)
even where older docs say shadow: review it as production auth.

## Severity

- `[P0]` token issued with more scope or to the wrong workspace, verification bypass, key or subject in the repo.
- `[P1]` a client or a service cannot get a valid token, or `/v1/authz/decide` answers wrongly.
- `[P2]` edge case, or changed behaviour without a test.
- `[P3]` nit. **At most 3 nits.**

## Skip

- `uv.lock` hashes and generated metadata. Still review changed versions and sources (a lock-only
  bump is a real dependency change, e.g. `4d66aaa`) and the `platform-backend-core` git pin, which
  must match `pyproject.toml`.
- `.venv/`, `build/`, `dist/`, `*.egg-info`, `*_cache/`, `DOCPACK/pdf/`.
- `schema/*.json` and its `.sha256` pin change only from the canonical copy: a hand edit is a finding.

## Evidence

Every finding gives `file:line` and input -> wrong result. No finding without evidence from the diff
or the code it calls. Start the summary with the count, e.g. `2 findings (1 P1, 1 P3)`.
