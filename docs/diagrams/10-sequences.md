# 10 — Sequences

## 1. Token exchange (person, shared scope)

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller (owner)
  participant API as identity-api /v1/token
  participant RL as Rate limiter (local or PostgreSQL)
  participant G as GoogleVerifier + Google JWKS
  participant DB as Platform PostgreSQL
  participant K as SigningKey
  C->>API: POST form grant_type token-exchange, subject_token, audience, workspace, scope
  API->>RL: allow(client address)
  alt admission store unavailable
    RL-->>API: RateLimitUnavailable
    API-->>C: 503 temporarily_unavailable
  else over limit
    API-->>C: 429 slow_down
  end
  API->>API: check token type, audience in IDENTITY_TOKEN_AUDIENCES, scope format
  API->>G: external_subject(id_token)
  G-->>API: google:sub or InvalidIdToken (400 invalid_grant)
  API->>DB: set_config app.workspace_id, SELECT identity JOIN membership (both active)
  loop each requested shared scope
    API->>DB: platform.authorize(actor, ws, mapped permission, shared-infra, scope, ..., api)
    DB-->>API: decision
    alt not allow
      API-->>C: 403 invalid_scope
    end
  end
  API->>DB: rollback
  API->>K: sign claims iss aud sub ws iat exp jti scope (600 s)
  API-->>C: 200 access_token, no-store
```

Sources: `src/platform_identity/app.py` (`token`, `bound`), `src/platform_identity/google.py`, `src/platform_identity/rate_limit.py`, `docs/SHARED-RATE-LIMIT.md`

## 2. Client credentials (service client)

```mermaid
sequenceDiagram
  autonumber
  participant S as Service client
  participant API as identity-api /v1/token
  participant RL as Rate limiter
  participant V as verify_assertion
  participant R as ReplayCache (in process)
  participant K as SigningKey
  S->>API: POST grant_type client_credentials, client_assertion, audience, scope
  API->>RL: allow(client address)
  API->>V: unverified iss selects registered client, then verify RS256, aud, iss, exp, iat, jti
  V-->>API: client + claims or InvalidClient (401)
  API->>API: client_id field matches, audience and scopes within registration
  API->>R: use(client_id, jti, exp + 30 s)
  alt jti seen before or cache full
    API-->>S: 401 invalid_client
  end
  API->>K: sign sub = client subject, ws = client workspace, scope, client_id (600 s)
  API-->>S: 200 access_token
```

Sources: `src/platform_identity/service_clients.py`, `src/platform_identity/app.py` (`service_token`), `docs/SERVICE-CLIENTS.md`

## 3. Shadow authorization decision

```mermaid
sequenceDiagram
  autonumber
  participant P as platform-empire tasks-api
  participant API as identity-api /v1/authz/decide
  participant TV as TokenVerifier + platform JWKS
  participant DB as Platform PostgreSQL
  P->>API: POST action, resource_type, resource_id, approval_id, Bearer platform token, x-empire-channel
  API->>TV: verify(aud platform-api, issuer, lifetime, del and obo, channel)
  TV-->>API: Principal or 401 unauthenticated
  API->>API: scope present and action not in scope gives 403 scope_insufficient
  API->>DB: set_config app.workspace_id, SELECT platform.authorize(11 args)
  DB-->>API: decision, reason, decision_id
  alt decide_mode record
    API->>DB: commit
  else shadow
    API->>DB: rollback
  end
  API-->>P: 200 decision, reason, decision_id (null in shadow), header x-identity-decide-mode
```

Sources: `src/platform_identity/app.py` (`decide`, `caller`, `bound`), `src/platform_identity/auth.py`, `README.md` (step 3)

## 4. Token consumer verifies (JWKS)

```mermaid
sequenceDiagram
  participant U as Caller
  participant SVC as Book Platform service
  participant API as identity-api
  U->>SVC: request with Bearer token
  SVC->>API: GET /.well-known/jwks.json (cached)
  API-->>SVC: keys with kid, Cache-Control public max-age 300
  SVC->>SVC: verify RS256, iss, aud svc-api, scope
```

Sources: `src/platform_identity/app.py` (`jwks`), `docs/PLATFORM-SERVICES-CUTOVER.md`, `docs/CONTRACT-SURFACE.md`

## Open questions

- ฝั่ง consumer (platform-backend-core) cache JWKS นานเท่าไร: `CONTRACT-SURFACE.md` พูดถึง cellkit (10 นาที) ไม่ใช่ platform-backend-core
