---
title: "SPEC — Shared assertion-replay store and shared token limiter, so identity-api can run more than one replica"
status: proposed
tags: [spec, sdd, identity, scale, rate-limit, replay, postgres]
created: 2026-11-12
updated: 2026-11-12
description: "Proposal from the 2026-11-12 cell audit (P1: replay cache and limiter are per process). Moves client-assertion jti replay protection into Postgres and turns on the existing postgres limiter only after the trusted-proxy list is set, so a second replica is safe. Owner approval required (security-critical)."
---

# SPEC — Shared replay store and shared limits

Standard: ADR-0004 (approved spec before code; one failing test per scenario first).
Source: `architecture-docs:docs/architecture/SCALE-READINESS.md`, P1 findings for `platform-identity`.

## Document control

| Field | Value |
|---|---|
| Status | **proposed — waits for owner approval** (owner decision D5 in SCALE-READINESS.md) |
| Maker | Claude Code (cell audit session) |
| Approver | Owner (bookchaowalit) |
| Scope | `src/platform_identity/service_clients.py` (`ReplayCache`), `app.py` wiring, one additive migration; Railway variables |
| Out of scope | Moving identity's tables out of the platform-empire Neon project (ADR-0011 "one issuer" track) |

## 1. Problem

- `ReplayCache` (`service_clients.py:102`) keeps client-assertion `jti` values in process memory.
  - With two replicas, a captured `client_credentials` assertion can be replayed once on each replica.
  - After a restart, any assertion that has not yet expired can be replayed.
- The default limiter is `local` (`config.py:110`), so every replica has its own budget.
- The `postgres` limiter already exists (`docs/SHARED-RATE-LIMIT.md`), but turning it on today with an empty
  `IDENTITY_TRUSTED_PROXIES` keys every request on Railway's proxy address. Every caller would then share one
  30-per-minute bucket, which would throttle token issuance for the whole platform.
- Today identity runs one replica (Railway `numReplicas` unset, checked 2026-11-12), so this is a ceiling, not a live bug.

## 2. Scenarios (tests first)

| AC | Given / When / Then |
|---|---|
| AC-01 | Given two app instances sharing one database, when the same assertion `jti` is presented to each, then the first gets a token and the second gets `invalid_grant`. |
| AC-02 | Given an assertion already used, when identity restarts and the same assertion is presented before it expires, then it is refused. |
| AC-03 | Given expired `jti` rows, when the cleanup runs (on insert, bounded batch), then they are deleted and the table stays bounded. |
| AC-04 | Given the replay store is unreachable, then token exchange fails closed with 503. It never falls back to the in-memory cache. |
| AC-05 | Given `IDENTITY_TRUSTED_PROXIES` set to Railway's edge ranges, when two callers with different client IPs request tokens through the edge, then they get separate limiter keys (one caller hitting its limit does not limit the other). |
| AC-06 | Given postgres limiter mode with an empty trusted-proxy list in production, then startup refuses with a configuration error, so the "everyone shares one bucket" state cannot happen. |

## 3. Design

- **Table:** add an additive migration with
  `identity_replay(client_id text, jti text, expires_at timestamptz, primary key (client_id, jti))`.
  - `use()` becomes `INSERT … ON CONFLICT DO NOTHING RETURNING 1`. A conflict means a replay.
  - Each insert also deletes up to 100 expired rows.
  - Database time is the clock, as the limiter already does.
- **Roles:** writes go through the existing bounded serving pool (`IDENTITY_DB_POOL_MAX`). The serving role gets
  INSERT/DELETE on this table only, and the migration applies before merge.
- **Proxies:** confirm the address Railway's edge connects from, using a request log or header capture on
  staging. Then set `IDENTITY_TRUSTED_PROXIES` and switch to `IDENTITY_RATE_LIMIT_MODE=postgres` as a separate,
  reversible variable change. Rollback is `local`.
- **Cost:** none new. It uses the existing platform-empire Neon project and the existing service.

## 4. Rollout

1. Tests AC-01–AC-06 red, then green; review by Codex; owner approval of this spec.
2. Apply the migration with the migrator URI, then merge, then deploy.
3. Verify a token exchange through the MCP remote and the posture monitor client.
4. Turn on postgres limiter mode after the trusted-proxy evidence.
5. Only then allow `numReplicas > 1`.

## การอนุมัติ

| Role | Status | Date | Sign-off |
|---|---|---|---|
| Owner (Accountable approver) | รอพิจารณา | — | — |
| Maker (Claude Code) | ร่างแล้ว | 2026-11-12 | — |
