# ADR-0006 — Host บน Railway (Singapore, always on), deploy ด้วย CLI; CI ไม่ deploy

- Status: accepted (retro-documented)
- Date of decision: 2026-10-14 (first deploy, step 3)

## Decision

- build จาก `Dockerfile` (python:3.12-slim, user ไม่ใช่ root uid 10001), health check `/health` timeout 30 s, restart `ON_FAILURE` สูงสุด 3 ครั้ง
- region ตั้งด้วย `railway scale southeast-asia=1` ไม่ใช่ใน `railway.toml`; always on (ไม่ sleep)
- deploy มือ: `railway up` จาก `git archive main` ที่สะอาด เพราะ Railway GitHub app ไม่ได้ติดตั้งใน org `bookchaowalit-backend`; merge เข้า `main` จึงไม่ deploy
- secrets (`IDENTITY_DATABASE_URL`, `IDENTITY_SIGNING_KEY_PEM`) เป็น Railway variables เท่านั้น

## Evidence

- `railway.toml`, `Dockerfile`
- `.github/workflows/check.yml` (comment "CI never deploys. identity-api on Railway is deployed by hand")
- `PRODUCT.md` (Host), `docs/PLATFORM-SERVICES-CUTOVER.md` ("clean `git archive main`, `railway up --path-as-root`, deployment `7a4e4280`")
- commits `f61e742`, `e2b9b50`

## Consequences

- ไม่มีประวัติ deploy ใน git/GitHub: ต้องอ่านจาก Railway หรือ docs (ดู [22-dora.md](../22-dora.md))
- ต่างจาก 8 platform services ที่เปิด sleep (serverless) ตาม ops record: identity เปิดตลอดเพราะเป็น issuer ที่ทุก service ต้องพึ่ง (เหตุผลนี้อนุมานจากบทบาท ไม่มีเอกสารยืนยัน → open question ใน 16)
