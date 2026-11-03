# 01 — Business process (As-Is / To-Be)

platform-identity เป็น backend platform service ไม่มี UI ผู้ใช้คนเดียวคือ owner (single-owner platform, `PRODUCT.md`)
"business process" ของ repo นี้จึงเป็นกระบวนการ **ออก token และตัดสินสิทธิ์** ให้ service อื่นใน Book Platform

## As-Is (จากโค้ดและ docs ณ `origin/main` `9bf770c`, 2026-11-03)

```mermaid
flowchart TD
  subgraph Person["Person path (owner)"]
    P1["Owner signs in with Google, gets id_token"] --> P2["POST /v1/token grant token-exchange"]
    P2 --> P3{"IDENTITY_GOOGLE_CLIENT_IDS set?"}
    P3 -->|"no (today)"| P4["400 invalid_grant: no Google client is configured"]
    P3 -->|yes| P5["Verify id_token, map google:sub"]
    P5 --> P6["Active identity + active membership in workspace?"]
    P6 -->|no| P7["400 invalid_grant"]
    P6 -->|yes| P8["Each shared scope: platform.authorize must allow"]
    P8 -->|"deny or require_approval"| P9["403 invalid_scope"]
    P8 -->|allow| P10["Sign RS256 access token, 600 s, one audience + one workspace"]
  end
  subgraph Machine["Service client path"]
    M1["Service signs client assertion with its own RSA key"] --> M2["POST /v1/token grant client_credentials"]
    M2 --> M3["Verify assertion vs IDENTITY_SERVICE_CLIENTS_JSON"]
    M3 -->|fail| M4["401 invalid_client"]
    M3 -->|ok| M5["Audience and scopes within registration?"]
    M5 -->|no| M6["400 invalid_target or invalid_scope"]
    M5 -->|yes| M7["Consume jti once, sign token 600 s with client_id"]
  end
  subgraph Shadow["Shadow decision path"]
    S1["platform-empire answers a decide request"] --> S2["Mirror to POST /v1/authz/decide when IDENTITY_SHADOW_URL is set"]
    S2 --> S3["identity calls platform.authorize then rolls back (shadow)"]
    S3 --> S4["platform-empire logs identity_shadow outcome=match or mismatch"]
  end
  P10 --> USE["Caller presents token to a Book Platform service"]
  M7 --> USE
  USE --> V["Service verifies via identity JWKS, audience svc-api"]
```

Sources: `src/platform_identity/app.py`, `src/platform_identity/google.py`, `src/platform_identity/service_clients.py`, `README.md`, `PRODUCT.md`, `docs/PLATFORM-SERVICES-CUTOVER.md`, `docs/SERVICE-CLIENTS.md`, ops record: book-platforms-status (2026-11-03) ("Only open: owner `IDENTITY_GOOGLE_CLIENT_IDS`")

ข้อสังเกต As-Is:
- person path ยังปิดอยู่จริงในวันนี้: `GoogleVerifier.external_subject` โยน `InvalidIdToken("no Google client is configured")` เมื่อไม่มี client id และ cutover doc ระบุว่า owner ยังไม่ได้ตั้ง `IDENTITY_GOOGLE_CLIENT_IDS`
- service client path ใช้งานจริงแล้ว: `posture-monitor` → `security-api` (2026-11-03) และ `mcp-hub` (ops record)
- cells เดิม (life-*, finance-*) ยังเชื่อ issuer `empire-platform` ไม่ใช่ issuer นี้ (`docs/PLATFORM-SERVICES-CUTOVER.md` "Not covered yet")

## To-Be (จาก `docs/DESIGN-2026-10-13.md`, `PRODUCT.md` roadmap, `docs/UPGRADE-PLAN.md`)

```mermaid
flowchart LR
  S3["Step 3 Shadow: running"] --> S45["Steps 4-5: parity report, cells point decide here, automations use API keys"]
  S45 --> S6["Step 6 Cutover: cells trust this issuer, logins unified (web, metadata APIs, remote MCP)"]
  S6 --> S7["Step 7: own Neon project for identity, logical replication"]
  S7 --> S8["Step 8: drop identity tables from platform-empire after 30 days"]
  S45 -.-> ADM["Planned: /v1/workspaces, members, roles, api-keys, delegations, /v1/me"]
```

Sources: `docs/DESIGN-2026-10-13.md` §6–§7, `PRODUCT.md` (Roadmap), `contract.json` (`identity.http.workspace-admin` planned)

## Open questions

- Shadow parity gate (100% match ถึง 2026-10-21) ผ่านหรือไม่: `PRODUCT.md` บอก "log line exists; no report yet" ไม่มีรายงานใน repo
- ขั้น 4–5 ยังเป็น `[ต้องยืนยัน]` ใน `PRODUCT.md`; ลำดับจริงเมื่อเทียบกับการที่ 8 platform services เชื่อ issuer นี้ไปก่อนแล้ว (2026-10-29) ยังไม่มีเอกสาร To-Be ที่อัปเดต
- API keys (`emp_<prefix>_<secret>`) ใน design ยังไม่มีโค้ด ยังอยู่ในแผนหรือถูกแทนด้วย service clients
