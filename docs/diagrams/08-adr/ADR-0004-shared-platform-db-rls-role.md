# ADR-0004 — ใช้ DB เดิมของ platform-empire (schema `platform`) ผ่าน role `identity_api`; แยก DB เมื่อมี tenant ภายนอกรายแรก

- Status: accepted (retro-documented)
- Date of decision: 2026-10-14 (owner decision I2)

## Context

ตาราง identities, workspaces, memberships, roles, policies, delegations และ `platform.authorize` อยู่ใน schema `platform` ของ platform-empire; shadow ต้องเรียก function เดียวกันเพื่อให้คำตอบเทียบกันได้

## Decision

- ขั้น 3 ใช้ DB เดิม schema `platform` ผ่าน role ใหม่ `identity_api` (migration 0008 ของ platform-empire) ที่ grant เฉพาะตาราง identity
- ทุก request ตั้ง `app.workspace_id` ใน transaction (`set_config(..., true)`) แล้ว rollback ตอนจบ; RLS เป็น forced RLS บน `app.workspace_id`
- pool ปฏิเสธ role ที่เป็น superuser หรือ BYPASSRLS
- แยก DB (ขั้น 7) รอ tenant ภายนอกรายแรก
- ตาราง admission ของ rate limit แยกเป็น schema `identity_rate_limit` ของ identity เอง และ migrate ด้วย CLI แยก ไม่รันตอน startup

## Evidence

- `docs/DESIGN-2026-10-13.md` §4, §7 ขั้น 3 และ 7, §10 I2; ตารางสถานะขั้น 3 (role `identity_api`, migration 0008)
- `contract.json` `identity.table.workspace-rbac` ("forced RLS on app.workspace_id")
- `src/platform_identity/app.py` `bound`; `src/platform_identity/rate_limit.py` `bounded_pool.configure` ("serving role must be NOSUPERUSER NOBYPASSRLS"), `apply_migration`
- `src/platform_identity/migrations/0001_token_rate_limit.sql`

## Consequences

- identity ขึ้นกับ quota ของ Neon project ของ platform (cutover doc: activation รอ quota `empire-platform` reset 2026-11-01)
- service token (`client_credentials`) ไม่แตะ platform DB จึงยังออกได้แม้ DB นั้นล่ม (`docs/SERVICE-CLIENTS.md`)
- schema ของตาราง `platform.*` เป็นของ platform-empire; repo นี้ไม่มี migration ของตารางเหล่านั้น
