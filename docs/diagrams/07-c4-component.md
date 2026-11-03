# 07 — C4 Component (identity-api)

```mermaid
C4Component
  title identity-api components (src/platform_identity)
  Container_Boundary(api, "identity-api") {
    Component(app, "app.py", "FastAPI routes", "health, readyz, jwks, openid-configuration, authz decide, token, error handlers")
    Component(auth, "auth.py TokenVerifier", "PyJWT", "Platform bearer tokens: aud platform-api, lifetime, delegation, channel, scope")
    Component(google, "google.py GoogleVerifier", "PyJWT", "id_token to google:sub")
    Component(sc, "service_clients.py", "PyJWT", "Registry parse, verify_assertion, ReplayCache")
    Component(keys, "keys.py", "cryptography, httpx", "SigningKey, RemoteJwks, StaticKey, decode_with_any")
    Component(cfg, "config.py Config", "env", "IDENTITY_* variables, shared scope map validation")
    Component(rl, "rate_limit.py", "psycopg_pool", "bounded_pool, PostgresRateLimiter, apply_migration")
    Component(sentry, "_sentry.py", "sentry-sdk", "init_sentry, scrub, before_send")
  }
  ContainerDb(db, "PostgreSQL", "platform + identity_rate_limit schemas")
  System_Ext(ext, "Google and platform JWKS", "HTTPS")

  Rel(app, auth, "verify caller of decide")
  Rel(app, google, "verify subject_token")
  Rel(app, sc, "verify client assertion, consume jti")
  Rel(app, keys, "sign access tokens")
  Rel(app, rl, "allow, ready")
  Rel(app, cfg, "main reads Config.from_env")
  Rel(auth, keys, "decode_with_any")
  Rel(google, keys, "decode_with_any")
  Rel(keys, ext, "fetch JWK Sets")
  Rel(app, db, "SQL via pool")
  Rel(rl, db, "SQL via pool")
```

Sources: `src/platform_identity/app.py`, `auth.py`, `google.py`, `service_clients.py`, `keys.py`, `config.py`, `rate_limit.py`, `_sentry.py`, `__init__.py`

หมายเหตุ: `_sentry.init_sentry` ถูกเรียกจาก package `__init__` (ตาม docstring ของ `_sentry.py`)

## Open questions

- ไม่มี
