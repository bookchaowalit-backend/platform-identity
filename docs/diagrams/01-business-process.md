# 01 — Business process (As-Is / To-Be)

platform-identity เป็น backend platform service ไม่มี UI ผู้ใช้คนเดียวคือ owner (single-owner platform, `PRODUCT.md`)
"business process" ของ repo นี้จึงเป็นกระบวนการ **ออก token และตัดสินสิทธิ์** ให้ service อื่นใน Book Platform

Last reviewed: 2026-11-27 · commit `af6bef9`

## As-Is (จากโค้ดวันนี้)

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
  P10 --> USE
  subgraph Tenancy["Tenancy path (IDENTITY_TENANCY_MODE=on, SPEC-006)"]
    T1["Person signs up or accepts an invitation: /v1/signup, /v1/invitations/accept"] --> T2["Workspace, members, roles via /v1/members, /v1/workspace, /v1/me/workspaces"]
    T2 --> T3["Non-active workspace or identity gets no token (AC-13)"]
    D1["Installed service exchanges a received token: token-exchange with client assertion"] --> D2["Delegated token only narrows scope, bounded chain, same workspace"]
  end
  T2 -.-> P6
  D2 --> USE["Caller presents token to a Book Platform service"]
  M7 --> USE
  USE --> V["Service verifies via identity JWKS, audience svc-api"]
```

Sources: `src/platform_identity/app.py`, `src/platform_identity/tenancy.py`, `src/platform_identity/config.py`, `docs/SPEC-006-TENANCY-AND-DELEGATION.md`, `src/platform_identity/google.py`, `src/platform_identity/service_clients.py`, `README.md`, `PRODUCT.md`, `docs/PLATFORM-SERVICES-CUTOVER.md`, `docs/SERVICE-CLIENTS.md`, ops record: book-platforms-status (2026-11-03) ("Only open: owner `IDENTITY_GOOGLE_CLIENT_IDS`")

ข้อสังเกต As-Is:
- person path เปิดแล้วตั้งแต่ 2026-11-28: `IDENTITY_GOOGLE_CLIENT_IDS` มี client `finance-suite-web` (ก่อนหน้านั้น `GoogleVerifier.external_subject` โยน `InvalidIdToken("no Google client is configured")`); ยังไม่มี login จริงบน production
- service client path ใช้งานจริงแล้ว: `posture-monitor` → `security-api` (2026-11-03) และ `mcp-hub` (ops record)
- tenancy path (`/v1/signup`, `/v1/members`, `/v1/invitations`, `/v1/workspace`, `/v1/me/workspaces`, `/v1/installations`, `/v1/service/installations`) และ delegated token มีในโค้ดแล้ว แต่ทำงานเมื่อ `IDENTITY_TENANCY_MODE=on` เท่านั้น (ค่าเริ่มต้น `off`) และ signup เริ่มต้นเป็น `closed` (`IDENTITY_SIGNUP_MODE`); ค่าบน production ตอนนี้ [ต้องยืนยัน]
- มี `/v1/internal/entitlement` ด้วย (`src/platform_identity/entitlement.py`) [ต้องยืนยัน] ว่าใครเรียกและ gate อะไร
- cells เดิม (life-*, finance-*) ยังเชื่อ issuer `empire-platform` ไม่ใช่ issuer นี้ [ต้องยืนยัน] ว่ายังจริงหลัง 2026-11-03 (`docs/PLATFORM-SERVICES-CUTOVER.md` "Not covered yet")

## To-Be (จาก `docs/DESIGN-2026-10-13.md`, `PRODUCT.md` roadmap, `docs/SPEC-006-TENANCY-AND-DELEGATION.md`)

```mermaid
flowchart LR
  S3["Step 3 Shadow: running"] --> S45["Steps 4-5: parity report, cells point decide here, automations use API keys"]
  S45 --> S6["Step 6 Cutover: cells trust this issuer, logins unified (web, metadata APIs, remote MCP)"]
  S6 --> S7["Step 7: own Neon project for identity, logical replication"]
  S7 --> S8["Step 8: drop identity tables from platform-empire after 30 days"]
  S45 -.-> ADM["Planned: /v1/workspaces, members, roles, api-keys, delegations, /v1/me"]
```

Sources: `docs/DESIGN-2026-10-13.md` §6–§7, `PRODUCT.md` (Roadmap), `contract.json` (`identity.http.workspace-admin` planned), `docs/SPEC-006-TENANCY-AND-DELEGATION.md` (approved 2026-11-12, implemented)

## Gap

| ID | As-Is → To-Be | Status | Owner | Evidence |
|---|---|---|---|---|
| G1 | person path ปิด (ไม่มี `IDENTITY_GOOGLE_CLIENT_IDS`) → owner login ด้วย Google ได้ (client id ตั้งแล้ว 2026-11-28; เหลือทดสอบ login จริง) | todo | owner | `docs/PLATFORM-SERVICES-CUTOVER.md` step 3 (owner ตั้ง client id); `src/platform_identity/google.py` |
| G2 | shadow รันอยู่ ยังไม่มีรายงาน parity → parity report + rollback rehearsal (steps 4–5) | todo | claude | `PRODUCT.md` Roadmap (planned [ต้องยืนยัน]) |
| G3 | cells เชื่อ issuer `empire-platform` → เชื่อ issuer นี้, ล็อกอินรวม (step 6 cutover) | decide | owner | `PRODUCT.md` Roadmap: needs owner approval |
| G4 | identity อยู่ใน DB ของ platform-empire → Neon project ของตัวเอง + logical replication, แล้วลบตารางเดิมหลัง 30 วัน (steps 7–8) | todo | claude | `docs/DESIGN-2026-10-13.md` §6–§7 |
| G5 | tenancy (signup, members, delegation) มีในโค้ดแต่ปิดโดยค่าเริ่มต้น → เปิดตาม SPEC-006 | decide | owner | `docs/SPEC-006-TENANCY-AND-DELEGATION.md` (implemented), `src/platform_identity/config.py` `IDENTITY_TENANCY_MODE`; สถานะ production [ต้องยืนยัน] |
| G6 | `/v1/workspaces`, roles, api-keys, `/v1/me` ยัง planned ใน contract → มี endpoint ตาม design | todo | claude | `contract.json` `identity.http.workspace-admin` planned; ส่วน members/invitations ทำแล้วใน SPEC-006 |

## Open questions

- Shadow parity gate (100% match ถึง 2026-10-21) ผ่านหรือไม่: `PRODUCT.md` บอก "log line exists; no report yet" ไม่มีรายงานใน repo
- ขั้น 4–5 ยังเป็น `[ต้องยืนยัน]` ใน `PRODUCT.md`; ลำดับจริงเมื่อเทียบกับการที่ 8 platform services เชื่อ issuer นี้ไปก่อนแล้ว (2026-10-29) ยังไม่มีเอกสาร To-Be ที่อัปเดต
- API keys (`emp_<prefix>_<secret>`) ใน design ยังไม่มีโค้ด (grep ใน `src` ไม่พบ) ยังอยู่ในแผนหรือถูกแทนด้วย service clients (G6)
- `docs/UPGRADE-PLAN.md` และ `README.md` ยังบอกว่าไม่มี runtime ซึ่งล้าหลังโค้ด
