# ADR-0005 — Service clients ใช้ `client_credentials` + `private_key_jwt`, ทะเบียนอยู่ใน env

- Status: accepted (retro-documented; decision record เต็มอยู่ใน [`docs/SERVICE-CLIENTS.md`](../../SERVICE-CLIENTS.md))
- Date of decision: 2026-10-29 (owner: "Yes, start it"); deployed 2026-11-03

## Decision (สรุป ไม่คัดลอก)

- `/v1/token` รับ `grant_type=client_credentials` ที่ยืนยันด้วย assertion RS256 ซึ่ง client เซ็นด้วย private key ของตัวเอง; identity ถือเฉพาะ public key
- ทะเบียน `IDENTITY_SERVICE_CLIENTS_JSON` (≤ 32 clients) กำหนด subject, workspace, audiences, scopes; "The registration is the authorization" ไม่เรียก `platform.authorize`
- jti ใช้ได้ครั้งเดียว จำไว้ใน process จนหมดอายุ

## Evidence

- `src/platform_identity/service_clients.py`, `src/platform_identity/app.py` `service_token`
- `tests/service/test_service_clients.py`
- `docs/PLATFORM-SERVICES-CUTOVER.md` ("Deployed 2026-11-03": `posture-monitor` → `security-api`)
- commits `434d7f2`, `20bc844`, merge `fb52d6e` (PR #5)

## Consequences

- replay protection ต่อ process: ต้องย้ายไป shared store ก่อนรันหลาย replica
- revoke client = แก้ variable + redeploy
