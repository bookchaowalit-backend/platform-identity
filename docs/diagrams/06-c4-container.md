# 06 — C4 Container

```mermaid
C4Container
  title platform-identity containers
  System_Ext(callers, "Callers", "owner, service clients, platform-empire, Book Platform services")
  System_Boundary(rw, "Railway service identity-api (Singapore, always on)") {
    Container(api, "identity-api", "Python 3.12, FastAPI, uvicorn", "Entry point platform_identity.app:main, port 8080, proxy_headers off")
    Container(mem, "In-process state", "Python objects", "RateLimiter (local mode), ReplayCache for jti, RemoteJwks caches")
  }
  Container(cli, "identity-rate-limit-migrate", "Python CLI", "Explicit migration, run by operator, never at startup")
  ContainerDb(platformdb, "Platform PostgreSQL", "schema platform", "identities, memberships, platform.authorize; role identity_api")
  ContainerDb(rldb, "Admission PostgreSQL (optional)", "schema identity_rate_limit", "windows, schema_migrations; only in postgres mode")
  System_Ext(google, "Google JWKS", "googleapis.com oauth2 v3 certs")
  System_Ext(pjwks, "Platform JWKS", "IDENTITY_PLATFORM_JWKS_URL")
  System_Ext(sentry, "Sentry", "errors")

  Rel(callers, api, "HTTPS")
  Rel(api, mem, "uses")
  Rel(api, platformdb, "psycopg pool, max 8")
  Rel(api, rldb, "psycopg pool, max 4")
  Rel(cli, rldb, "DDL + grants via admin DSN")
  Rel(api, google, "httpx GET, cache 600 s")
  Rel(api, pjwks, "httpx GET, cache 600 s")
  Rel(api, sentry, "sentry-sdk")
```

Sources: `Dockerfile`, `railway.toml`, `pyproject.toml` (`[project.scripts]`), `src/platform_identity/app.py` (`main`, `create_app`, `RateLimiter`), `src/platform_identity/service_clients.py` (`ReplayCache`), `src/platform_identity/keys.py` (`RemoteJwks`, `JWKS_CACHE_SECONDS`), `src/platform_identity/rate_limit.py`, `src/platform_identity/config.py`, `src/platform_identity/migrations/0001_token_rate_limit.sql`, `docs/SHARED-RATE-LIMIT.md`

## Open questions

- `IDENTITY_RATE_LIMIT_MODE` ใน production เป็น `local` (default) หรือ `postgres`; ถ้า postgres, `IDENTITY_RATE_LIMIT_DATABASE_URL` ชี้ DB ไหน ไม่มีบันทึก
