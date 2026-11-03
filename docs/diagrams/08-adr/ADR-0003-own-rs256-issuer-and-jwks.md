# ADR-0003 — Signing key RS256 ของตัวเอง, JWKS ที่ `kid` = RFC 7638 thumbprint, issuer เป็น HTTPS URL

- Status: accepted (retro-documented)
- Date of decision: 2026-10-14 (I4 issuer เป็น URL); 2026-10-29 (owner: Book Platform services เชื่อ issuer นี้)

## Context

เดิม token ถูก mint ด้วยมือบนเครื่อง owner ด้วย issuer `empire-platform` และทุก cell pin PEM ไว้ การหมุน key ต้องแก้ทุก cell พร้อมกัน (`docs/DESIGN-2026-10-13.md` §1)

## Decision

- service เซ็น access token ด้วย RSA ≥ 2048 bit ของตัวเอง (`IDENTITY_SIGNING_KEY_PEM`, Railway variable เท่านั้น), header มี `kid`
- เผยแพร่ public key ที่ `/.well-known/jwks.json` (cache 300 s) และ discovery `/.well-known/openid-configuration`
- `IDENTITY_ISSUER` ต้องเป็น HTTPS URL และต้องต่างจาก platform issuer
- access token อายุ 600 s, claims `iss aud sub ws iat exp jti` (+ `scope`, + `client_id` สำหรับ service client)
- 2026-10-29: 7 platform services (ต่อมา + security) ตั้ง `JWT_ISSUER=https://identity-api-production-fad1.up.railway.app` และ `JWT_JWKS_URL=<issuer>/.well-known/jwks.json`, audience `<service>-api`

## Evidence

- `src/platform_identity/keys.py` (`SigningKey`, `rsa_jwk`), `src/platform_identity/app.py` (`jwks`, `openid_configuration`, `ACCESS_TOKEN_TTL_SECONDS = 600`)
- `src/platform_identity/config.py` (`require_https(issuer)`, "IDENTITY_ISSUER must differ from the platform issuer")
- `tests/service/test_app.py` `test_jwks_publishes_only_the_public_signing_key`
- `docs/PLATFORM-SERVICES-CUTOVER.md` ("Done on 2026-10-29"; "The signing key is unchanged, so the JWKS `kid` … stay valid")
- ops record: book-platforms-status (2026-11-03) (JWT section)

## Consequences

- JWKS แสดงได้ key เดียว (`{"keys": [signing_key.jwk]}`): การหมุน key แบบ `next → active → retiring` ใน design §5 ยังไม่มีโค้ด การเปลี่ยน key ตอนนี้จะทำให้ token ที่ออกไปแล้ว (≤ 600 s) และ cache ของผู้ตรวจใช้ไม่ได้ชั่วคราว
- ไม่มี revocation: token มีชีวิตไม่เกิน 600 s
