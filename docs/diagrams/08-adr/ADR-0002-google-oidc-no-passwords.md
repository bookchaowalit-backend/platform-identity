# ADR-0002 — ยืนยันตัวคนด้วย Google OIDC; ไม่เก็บรหัสผ่าน

- Status: accepted (retro-documented)
- Date of decision: 2026-10-14 (owner decision D3)

## Context

ก่อนหน้านี้มี login 3 แบบ (Google OAuth ของ life-dashboard-web, service JWT + allowlist, Clerk ของ remote MCP) และ allowlist ซ้ำหลายที่ (`docs/DESIGN-2026-10-13.md` §1)

## Decision

- platform-identity เป็น login กลาง; Google พิสูจน์ว่าเป็นใคร; service นี้ไม่เก็บ password และไม่ทำ MFA เอง
- รับ id_token เฉพาะ audience ที่เป็น client id ของเราเอง (`IDENTITY_GOOGLE_CLIENT_IDS`), issuer `accounts.google.com`, `sub` เป็นตัวเลข ≤ 64 ตัว
- map คนด้วย `google:<sub>` ไม่ใช้ email เพราะ email เปลี่ยนมือได้
- คนที่ไม่รู้จักกับคนที่ไม่ใช่สมาชิกได้คำตอบเดียวกัน (`invalid_grant`) เพื่อไม่ให้รั่วว่าเป็นสมาชิกหรือไม่

## Evidence

- `src/platform_identity/google.py` (`GOOGLE_ISSUERS`, `SUBJECT_PREFIX = "google:"`, `LEEWAY_SECONDS = 30`)
- `src/platform_identity/app.py` (`token`: query `platform.identities` + `platform.memberships`, ข้อความ "Same answer for an unknown person and a non-member")
- `tests/service/test_app.py` `test_token_exchange_refusals`
- `docs/DESIGN-2026-10-13.md` §10 D3; `PRODUCT.md` (Data: "Passwords are never stored")

## Consequences

- ถ้าไม่ตั้ง `IDENTITY_GOOGLE_CLIENT_IDS` จะปฏิเสธ Google token ทุกตัว ซึ่งเป็นสถานะ production ณ 2026-11-03 (`docs/PLATFORM-SERVICES-CUTOVER.md`)
- เพิ่ม provider อื่นได้ภายหลังผ่าน prefix ของ subject (design เสนอ `external_issuer` คู่ `external_subject`, ยังไม่ทำ)
