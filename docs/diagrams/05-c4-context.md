# 05 — C4 Context

```mermaid
C4Context
  title platform-identity system context (2026-11-03)
  Person(owner, "Owner", "Single owner; person tokens need IDENTITY_GOOGLE_CLIENT_IDS")
  System(identity, "platform-identity (identity-api)", "Token exchange, client credentials, JWKS, authz decide")
  System_Ext(google, "Google OIDC", "id_token issuer and JWKS")
  System_Ext(empire, "platform-empire", "tasks-api: platform tokens, platform.authorize, shadow mirror")
  SystemDb_Ext(pg, "Platform PostgreSQL (Neon empire-platform)", "schema platform, role identity_api")
  System_Ext(services, "Book Platform services", "communication, media, data, workflow, commerce, logistics, developer, security")
  System_Ext(clients, "Service clients", "posture-monitor, mcp-hub")
  System_Ext(sentry, "Sentry", "error events")
  System_Ext(uptime, "platform-uptime-worker", "probes /health")

  Rel(owner, identity, "POST /v1/token with Google id_token")
  Rel(clients, identity, "POST /v1/token client_credentials")
  Rel(empire, identity, "POST /v1/authz/decide (shadow)")
  Rel(identity, google, "GET JWKS")
  Rel(identity, empire, "GET platform JWKS")
  Rel(identity, pg, "SQL: identities, memberships, platform.authorize")
  Rel(services, identity, "GET /.well-known/jwks.json")
  Rel(identity, sentry, "errors when SENTRY_DSN set")
  Rel(uptime, identity, "GET /health")
```

Sources: `src/platform_identity/app.py`, `src/platform_identity/google.py` (`GOOGLE_JWKS_URL`), `src/platform_identity/config.py` (`IDENTITY_PLATFORM_JWKS_URL`), `src/platform_identity/_sentry.py`, `README.md`, `PRODUCT.md` (uptime probe), `docs/DESIGN-2026-10-13.md` step 3 (role `identity_api`, platform-empire DB), `docs/PLATFORM-SERVICES-CUTOVER.md`, ops record: book-platforms-status (2026-11-03)

หมายเหตุ: `payment-api` มี audience ใน identity ตาม ops record แต่ readyz ยัง 503 จึงไม่ได้วาดเป็นผู้ใช้ JWKS ที่ยืนยันแล้ว

## Open questions

- Neon project ที่ `IDENTITY_DATABASE_URL` ชี้: design บอกว่าเป็น DB ของ platform-empire (schema `platform`); ชื่อ Neon project `empire-platform` มาจาก cutover doc ("Neon `empire-platform` quota") ยืนยันจาก Railway variables ไม่ได้ใน repo
- `IDENTITY_PLATFORM_JWKS_URL` หรือ PEM: ใช้แบบไหนใน production ไม่ระบุใน repo
