# 21 — Incident response

repo นี้ไม่มี RUNBOOK แยก; ขั้นตอน deploy/verify ที่ใช้จริงอยู่ใน [`docs/PLATFORM-SERVICES-CUTOVER.md`](../PLATFORM-SERVICES-CUTOVER.md) (reused) และ rollback ของขั้น 3 อยู่ใน [`docs/DESIGN-2026-10-13.md`](../DESIGN-2026-10-13.md) §7

```mermaid
flowchart TD
  subgraph Detect["Alert channels found"]
    A1["platform-uptime-worker: /health down, Telegram"]
    A2["Sentry error event"]
    A3["Railway health check fails, restart ON_FAILURE up to 3"]
    A4["Downstream service 401s: tokens rejected"]
    A5["platform-empire log identity_shadow outcome=mismatch"]
  end
  A1 --> T["Triage by owner"]
  A2 --> T
  A3 --> T
  A4 --> T
  A5 --> T
  T --> Q1{"/health 200?"}
  Q1 -->|no| F1["Check Railway deployment and logs; redeploy clean git archive main"]
  Q1 -->|yes| Q2{"/readyz 200?"}
  Q2 -->|no| F2["DB or limiter: Neon quota or outage, role grants, migration checksum"]
  Q2 -->|yes| Q3{"JWKS kid unchanged and discovery 200?"}
  Q3 -->|no| F3["Signing key or issuer variable changed: restore previous IDENTITY_SIGNING_KEY_PEM / IDENTITY_ISSUER"]
  Q3 -->|yes| F4["Config: audiences, shared scope map, service client registry; verify with Config.from_env, railway variables --skip-deploys, redeploy"]
  A5 --> RB["Shadow rollback: remove IDENTITY_SHADOW_URL on tasks-api"]
  F1 --> V["Verify: /health, /readyz, JWKS, discovery, one token per caller"]
  F2 --> V
  F3 --> V
  F4 --> V
  RB --> V
  V --> PM["Postmortem: append to docs/PLATFORM-SERVICES-CUTOVER.md (no template in repo)"]
```

Sources: `PRODUCT.md` (uptime probe, Sentry), `.railway/railway.ts`, `src/platform_identity/app.py` (`health`, `readyz`, `jwks`), `src/platform_identity/config.py`, `docs/PLATFORM-SERVICES-CUTOVER.md` (variable change + `Config.from_env` check + redeploy + checks), `docs/DESIGN-2026-10-13.md` ขั้น 3 rollback, `docs/SHARED-RATE-LIMIT.md` (readiness failure causes); Telegram per central [ADR-0002](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0002-production-baseline-monitoring-backups.md)

## Blast radius reminder

identity เป็น issuer ของ 8 Book Platform services ตั้งแต่ 2026-10-29 และเป็น token source ของ posture-monitor และ mcp-hub; incident ที่ทำให้ JWKS เปลี่ยนหรือ `/v1/token` ล่มกระทบทั้งหมด (ดู [19](19-service-dependency-map.md))

## Open questions

- ไม่มี severity levels, on-call หรือ postmortem template สำหรับ service นี้
- uptime probe ใช้ `/health` จึงไม่แจ้งเมื่อ DB ล่ม (ควร probe `/readyz` หรือไม่)
- rollback plan หลัง 8 services เชื่อ issuer นี้ (ลบ `IDENTITY_SHADOW_URL` ใช้ได้แค่กับ shadow)
