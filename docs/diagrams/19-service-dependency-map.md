# 19 — Service dependency map (runtime)

```mermaid
flowchart LR
  subgraph Upstream["identity-api depends on"]
    PG["PostgreSQL schema platform (required for decide, token exchange, readyz)"]
    RL["PostgreSQL identity_rate_limit (only postgres mode; fail closed)"]
    GJ["Google JWKS (token exchange)"]
    PJ["Platform JWKS or pinned PEM (decide)"]
    SE["Sentry (optional)"]
  end
  ID["identity-api"]
  subgraph Downstream["depend on identity-api"]
    S1["communication-api"]
    S2["media-api"]
    S3["data-api"]
    S4["workflow-api"]
    S5["commerce-api"]
    S6["logistics-api"]
    S7["developer-api"]
    S8["security-api"]
    PM["posture-monitor (client)"]
    HUB["remote MCP hub mcp-hub (client)"]
    EMP["platform-empire tasks-api (shadow mirror)"]
  end
  ID --> PG
  ID --> RL
  ID --> GJ
  ID --> PJ
  ID -.-> SE
  S1 --> ID
  S2 --> ID
  S3 --> ID
  S4 --> ID
  S5 --> ID
  S6 --> ID
  S7 --> ID
  S8 --> ID
  PM --> ID
  HUB --> ID
  EMP -.-> ID
```

Sources: `src/platform_identity/app.py`, `config.py`, `keys.py`, `google.py`, `rate_limit.py`, `_sentry.py`, `docs/PLATFORM-SERVICES-CUTOVER.md` (services trust issuer; posture-monitor), ops record: book-platforms-status (2026-11-03) (mcp-hub; JWT trust; payment audience)

## Failure impact

| If down | Effect (from code) |
|---|---|
| Platform PostgreSQL | decide → 503 `unavailable`; token exchange → 503; `/readyz` 503; **client_credentials still works** (no DB touch) unless postgres limiter shares that DB |
| Limiter DB (postgres mode) | every `/v1/token` → 503 `temporarily_unavailable` (no local fallback) |
| Google JWKS | first fetch fails → `invalid_grant`; after a good fetch, last good keys are kept |
| identity-api | services cannot verify new tokens once their JWKS cache expires; no new tokens issued |

Sources: `src/platform_identity/app.py` (`bound`, `readyz`, `token`), `src/platform_identity/keys.py` (`RemoteJwks._fetch`), `docs/SERVICE-CLIENTS.md`, `docs/SHARED-RATE-LIMIT.md`

## Open questions

- payment-api: ops record บอกว่ามี audience `payment-api` แล้วแต่ service ยังไม่ ready จึงไม่ได้วาด
- consumer JWKS cache TTL ใน platform-backend-core (กำหนดว่าทน identity ล่มได้นานแค่ไหน)
