# 03 — User flows (API call flows per caller)

ไม่มีหน้า UI; "user flow" ในที่นี้คือลำดับการเรียก API ของ caller แต่ละแบบ

## Flow A — Person (owner) ขอ token ไปเรียก service

```mermaid
flowchart TD
  A1["Get Google id_token (outside this service)"] --> A2["GET /.well-known/openid-configuration (optional)"]
  A2 --> A3["POST /v1/token form: grant_type token-exchange, subject_token, subject_token_type id_token, audience, workspace, scope"]
  A3 --> A4{"Response"}
  A4 -->|"200"| A5["access_token, Bearer, expires_in 600, Cache-Control no-store"]
  A4 -->|"429 slow_down"| A6["Wait for next 1-minute window"]
  A4 -->|"400 invalid_grant / invalid_scope / invalid_target"| A7["Fix request or membership"]
  A4 -->|"403 invalid_scope"| A8["RBAC did not allow the shared scope: owner grant needed"]
  A4 -->|"503 temporarily_unavailable"| A9["Admission store down: retry later"]
  A5 --> A10["Call svc-api with Bearer token until exp"]
  A10 --> A3
```

Sources: `src/platform_identity/app.py` (`token`, `_token_error`), `docs/PLATFORM-SERVICES-CUTOVER.md` step 5

## Flow B — Service client (เช่น posture-monitor, mcp-hub)

```mermaid
flowchart TD
  B1["Sign assertion: iss = sub = client_id, aud = issuer/v1/token or issuer, exp - iat at most 300 s, new jti"] --> B2["POST /v1/token grant_type client_credentials, client_assertion_type jwt-bearer, client_assertion, audience, optional scope, optional client_id"]
  B2 --> B3{"Response"}
  B3 -->|"200"| B4["access_token 600 s with scope and client_id; cache it"]
  B3 -->|"401 invalid_client"| B5["Same body for every auth failure, incl. replayed jti"]
  B3 -->|"400 invalid_target / invalid_scope / invalid_request"| B6["Registration mismatch"]
  B4 --> B7["Call target svc-api"]
  B7 -->|"shortly before expires_in"| B1
```

Sources: `src/platform_identity/service_clients.py`, `src/platform_identity/app.py` (`service_token`), `docs/SERVICE-CLIENTS.md` (Request, Errors, Operating)

## Flow C — platform-empire shadow mirror

```mermaid
flowchart LR
  C1["tasks-api answers its own decide"] --> C2["POST /v1/authz/decide with same body and platform token"]
  C2 --> C3["200 decision, reason, decision_id null, header x-identity-decide-mode shadow"]
  C3 --> C4["tasks-api logs identity_shadow outcome"]
```

Sources: `README.md` (Service, step 3), `src/platform_identity/app.py` (`decide`), `docs/DESIGN-2026-10-13.md` step 3

## Flow D — Token consumer (Book Platform service)

```mermaid
flowchart LR
  D1["Service receives Bearer token"] --> D2["GET identity /.well-known/jwks.json, Cache-Control public max-age 300"]
  D2 --> D3["Verify RS256 with kid, iss = identity URL, aud = svc-api"]
```

Sources: `src/platform_identity/app.py` (`jwks`), `docs/CONTRACT-SURFACE.md` (Behaviour notes), `docs/PLATFORM-SERVICES-CUTOVER.md`

## Open questions

- Flow C ยังทำงานอยู่หรือไม่ (`IDENTITY_SHADOW_URL` ยังตั้งบน tasks-api?) ไม่มีหลักฐานหลัง 2026-10-14 ใน repo นี้
- ใครเป็นผู้ได้ Google id_token ให้ owner (web app ใด) ยังไม่ถูกกำหนด
