# ADR-0001 — Python/FastAPI service ที่รันแบบ shadow ข้าง platform-empire (strangler)

- Status: accepted (retro-documented)
- Date of decision: 2026-10-14 (owner decision I1; step 3 started)

## Context

identity, authz, workflow, graph และ tasks อยู่รวมใน `platform-empire` และ DB เดียว การย้ายต้องไม่มี downtime และต้องพิสูจน์ก่อนว่าคำตอบเหมือนเดิม

## Decision

- เขียน service ด้วย Python + FastAPI (เหมือน cell) — I1
- รัน `POST /v1/authz/decide` ด้วย body, token และ `platform.authorize` เดียวกับ platform; โหมด `shadow` (default) rollback transaction จึงไม่บันทึกซ้ำ, โหมด `record` commit และคืน `decision_id`
- ย้ายแบบ strangler ทีละขั้น (0–8) มี gate และ rollback ทุกขั้น

## Evidence

- `pyproject.toml` (fastapi, uvicorn, psycopg, pyjwt), `Dockerfile`
- `src/platform_identity/app.py`: `decide_mode not in ("shadow", "record")`, `conn.rollback()` ใน `bound`, `if decide_mode == "record": conn.commit()`
- `src/platform_identity/config.py`: `IDENTITY_DECIDE_MODE` default `shadow`
- `docs/DESIGN-2026-10-13.md` §7 (ขั้น 0–8), §10 (I1)
- commit `264c136` "feat: identity service in shadow mode (migration step 3)"

## Consequences

- ทุก decide ใน shadow ไม่มีผลข้างเคียงใน DB จึงเรียกซ้ำได้
- parity ต้องวัดฝั่ง platform-empire (log `identity_shadow outcome=`); repo นี้ไม่มีรายงาน parity
