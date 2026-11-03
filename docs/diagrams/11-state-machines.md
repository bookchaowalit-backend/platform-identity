# 11 — State machines

## 1. Client assertion `jti` (ReplayCache, ต่อ process)

```mermaid
stateDiagram-v2
  [*] --> Unseen
  Unseen --> Remembered: use() after all other checks pass
  Unseen --> Refused: cache for this client full of live entries
  Remembered --> Refused: same jti again before exp + 30 s
  Remembered --> Forgotten: exp + 30 s passed and cache pruned
  Forgotten --> [*]
  Refused --> [*]
```

Sources: `src/platform_identity/service_clients.py` (`ReplayCache.use`, `max_entries_per_client = 1_000`), `src/platform_identity/app.py` ("Consume the jti last, so a request refused above does not burn a valid assertion"), `tests/service/test_service_clients.py` (`ReplayCacheTest`)

## 2. Rate-limit window per key (หนึ่งนาที)

```mermaid
stateDiagram-v2
  [*] --> Open: first attempt in minute window
  Open --> Open: hits below limit, admit and count
  Open --> Exhausted: hits reach per-key limit (30 default)
  Exhausted --> Exhausted: refuse 429 slow_down, still counts toward global
  Open --> Open: new minute window resets count
  Exhausted --> Open: new minute window
  Open --> Unavailable: postgres mode and store down, checksum drift or missing grants
  Unavailable --> [*]: 503 temporarily_unavailable, no token minted
```

Sources: `src/platform_identity/app.py` (`RateLimiter`, `TOKEN_REQUESTS_PER_MINUTE = 30`), `src/platform_identity/rate_limit.py` (`PostgresRateLimiter.allow`), `docs/SHARED-RATE-LIMIT.md` (Admission correctness)

## 3. Access token (ออกโดย service นี้)

```mermaid
stateDiagram-v2
  [*] --> Issued: /v1/token signs, exp = iat + 600
  Issued --> Valid: consumer verifies with JWKS
  Valid --> Expired: after 600 s
  Expired --> [*]
```

ไม่มี state "revoked": ไม่มี revocation list ในโค้ด (`docs/SERVICE-CLIENTS.md` Limits: "Tokens already issued live at most 600 s")

Sources: `src/platform_identity/app.py` (`ACCESS_TOKEN_TTL_SECONDS`)

## 4. Identity / membership status

โค้ดอ่านเฉพาะ `status = 'active'` ของ `platform.identities` และ `platform.memberships`; สถานะอื่นและการเปลี่ยนสถานะเป็นของ platform-empire จึงวาด lifecycle ไม่ได้ (ดู Open questions)

## 5. (Planned, ไม่มีโค้ด) signing key rotation

```mermaid
stateDiagram-v2
  [*] --> next
  next --> active: after at least 1 h for consumer caches
  active --> retiring: next key becomes active
  retiring --> retired: after max token lifetime
  retired --> [*]
```

Sources: `docs/DESIGN-2026-10-13.md` §5 (planned table `identity.signing_keys`); โค้ดปัจจุบันมี key เดียว (`app.py` `jwk_set = {"keys": [signing_key.jwk]}`)

## Open questions

- ค่าที่เป็นไปได้ของ `identities.status` / `memberships.status` และใครเปลี่ยน (platform-empire migrations)
- แผนหมุน signing key ของ identity เอง (ข้อ 5) จะทำเมื่อไร
