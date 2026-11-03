# Core 22 SDLC diagram pack — platform-identity

ชุดภาพ Core 22 ตาม SDLC Diagram Catalog v2 วาดจากโค้ดและ config ใน repo นี้ (ณ `origin/main` `9bf770c`, 2026-11-03)
platform-identity คือ identity service ของ Book Platform (`identity-api` บน Railway): token exchange (Google id_token), `client_credentials` สำหรับ service clients, JWKS และ authz decide แบบ shadow ข้าง platform-empire
ตั้งแต่ 2026-10-29 เป็น issuer ที่ 8 platform services เชื่อ

ทุกไฟล์มี `Sources:` ใต้ภาพ และ "Open questions" สำหรับสิ่งที่ยืนยันจากโค้ดไม่ได้; ข้อเท็จจริง hosting ที่ไม่อยู่ใน repo อ้าง `ops record: book-platforms-status (2026-11-03)`

| # | Item | Status | File | Sources read |
|---|---|---|---|---|
| 01 | Business process As-Is / To-Be | done | [01-business-process.md](01-business-process.md) | `app.py`, `google.py`, `service_clients.py`, `PRODUCT.md`, `docs/DESIGN-2026-10-13.md`, `docs/PLATFORM-SERVICES-CUTOVER.md` |
| 02 | Use cases | done | [02-use-cases.md](02-use-cases.md) | `app.py`, `auth.py`, `google.py`, `service_clients.py`, `config.py`, `docs/SERVICE-CLIENTS.md` |
| 03 | User flows (API call flows) | done | [03-user-flows.md](03-user-flows.md) | `app.py`, `docs/SERVICE-CLIENTS.md`, `docs/PLATFORM-SERVICES-CUTOVER.md`, `README.md` |
| 04 | Traceability (RTM) | done | [04-traceability.md](04-traceability.md) | `PRODUCT.md`, `contract.json`, `src/`, `tests/`, `runtime_tests/`, `check.yml` |
| 05 | C4 Context | done | [05-c4-context.md](05-c4-context.md) | `src/`, `README.md`, `PRODUCT.md`, design doc, cutover doc, ops record |
| 06 | C4 Container | done | [06-c4-container.md](06-c4-container.md) | `Dockerfile`, `railway.toml`, `pyproject.toml`, `app.py`, `keys.py`, `rate_limit.py` |
| 07 | C4 Component | done | [07-c4-component.md](07-c4-component.md) | all modules in `src/platform_identity/` |
| 08 | ADRs | done (6) | [08-adr/](08-adr/) | design doc §10, `SERVICE-CLIENTS.md`, `railway.toml`, `check.yml`, `src/`, git log |
| 09 | ERD | done (partial: `platform.*` columns live in platform-empire) | [09-erd.md](09-erd.md) | SQL in `app.py`, `migrations/0001_token_rate_limit.sql`, `rate_limit.py`, `contract.json` |
| 10 | Sequences | done | [10-sequences.md](10-sequences.md) | `app.py`, `auth.py`, `google.py`, `service_clients.py`, `rate_limit.py` |
| 11 | State machines | done | [11-state-machines.md](11-state-machines.md) | `service_clients.py`, `app.py`, `rate_limit.py`, design §5 |
| 12 | API & integrations | done (reuses [`docs/CONTRACT-SURFACE.md`](../CONTRACT-SURFACE.md)) | [12-api-integration.md](12-api-integration.md) | `app.py`, `auth.py`, `keys.py`, `google.py`, `_sentry.py` |
| 13 | Threat model | done | [13-threat-model.md](13-threat-model.md) | `src/`, `tests/service/`, `SERVICE-CLIENTS.md`, `SHARED-RATE-LIMIT.md`, design §9 |
| 14 | Backup & DR | done | [14-backup-dr.md](14-backup-dr.md) | `rate_limit.py`, `SHARED-RATE-LIMIT.md`, cutover doc, central ADR-0002 |
| 15 | CI/CD pipeline | done | [15-cicd-pipeline.md](15-cicd-pipeline.md) | `.github/workflows/check.yml`, platform-ci-workflows `ci-python.yml` / `security.yml` @ `87dc5e1c` |
| 16 | Deployment | done | [16-deployment.md](16-deployment.md) | `Dockerfile`, `railway.toml`, `config.py`, cutover doc, `PRODUCT.md` |
| 17 | Test coverage matrix | done | [17-test-coverage-matrix.md](17-test-coverage-matrix.md) | `tests/`, `runtime_tests/`, `check.yml` |
| 18 | Git & release workflow | done | [18-git-release-workflow.md](18-git-release-workflow.md) | `git log --first-parent`, `gh pr list` |
| 19 | Service dependency map | done | [19-service-dependency-map.md](19-service-dependency-map.md) | `src/`, cutover doc, `SERVICE-CLIENTS.md`, ops record |
| 20 | Observability | done | [20-observability.md](20-observability.md) | `_sentry.py`, `app.py`, `railway.toml`, `PRODUCT.md` |
| 21 | Incident response | reused + extended ([`docs/PLATFORM-SERVICES-CUTOVER.md`](../PLATFORM-SERVICES-CUTOVER.md)) | [21-incident-response.md](21-incident-response.md) | cutover doc, design §7, `app.py`, `railway.toml` |
| 22 | DORA | done (deploy-based values partial: CLI deploys not in git/gh) | [22-dora.md](22-dora.md) | `git log`, `gh run list` (35 runs), `gh pr list`, cutover doc |

## ADR list

- [ADR-0001](08-adr/ADR-0001-python-fastapi-shadow-service.md) Python/FastAPI shadow service ข้าง platform-empire
- [ADR-0002](08-adr/ADR-0002-google-oidc-no-passwords.md) Google OIDC, ไม่เก็บรหัสผ่าน
- [ADR-0003](08-adr/ADR-0003-own-rs256-issuer-and-jwks.md) RS256 issuer + JWKS ของตัวเอง
- [ADR-0004](08-adr/ADR-0004-shared-platform-db-rls-role.md) ใช้ DB ของ platform-empire ผ่าน role `identity_api` + RLS
- [ADR-0005](08-adr/ADR-0005-service-clients-private-key-jwt.md) Service clients `private_key_jwt` (สรุป, record เต็มคือ [`docs/SERVICE-CLIENTS.md`](../SERVICE-CLIENTS.md))
- [ADR-0006](08-adr/ADR-0006-railway-cli-deploy-singapore.md) Railway Singapore always on, deploy ด้วย CLI

## Findings worth the owner's attention

- `IDENTITY_GOOGLE_CLIENT_IDS` ยังไม่ตั้ง: person token ทุกตัวถูกปฏิเสธ (01, 02)
- Sentry ของ identity ยังใช้ regex scrub ที่ส่ง exception text (หลัง scrub); 8 platform services เปลี่ยนเป็น "no free text" แล้ว 2026-11-03 (13, 20)
- uptime probe ใช้ `/health` ซึ่งตอบ 200 แม้ DB ล่ม (20, 21)
- JWKS มี key เดียว ไม่มีโค้ดหมุน key; ไม่มี revocation (token ≤ 600 s) (11, 13)
- `/openapi.json` ยังเปิด (FastAPI default) แม้ปิด `/docs` (12)
- replay cache ของ service clients อยู่ใน process: ต้องย้ายก่อนเพิ่ม replica (13)
- ไม่มีบันทึก deploy ใน git/gh และไม่มี coverage gate (15, 22)
- `contract.json`, `CONTRACT-SURFACE.md`, README ("scaffolded", interface `pilot`/`planned`) และ `DOCPACK/00-product-card.md` ล้าหลังสิ่งที่ deploy อยู่จริง

Validate: `node ~/work/core22/tools/validate.mjs docs/diagrams/*.md docs/diagrams/08-adr/*.md`
