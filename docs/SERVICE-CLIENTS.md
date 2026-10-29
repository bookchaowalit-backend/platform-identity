# Service clients: `client_credentials` with `private_key_jwt` (decision 2026-10-29)

Status: accepted by the owner on 2026-10-29 ("Yes, start it"). Not deployed;
it ships with the identity-api deploy in
[PLATFORM-SERVICES-CUTOVER.md](PLATFORM-SERVICES-CUTOVER.md).

## Problem

`POST /v1/token` only exchanges a person's Google id_token. Two callers are
machines, not people:

- the AI gateway calls platform-data `/v1/service/admissions` with
  `quotas:service` in the system workspace (`PLATFORM_SYSTEM_WORKSPACE_ID`);
- a workflow worker calls platform-workflow `/v1/worker/*` with `jobs:work` in
  the workspace whose jobs it runs.

Neither can hold a Google identity, and a long-lived static bearer token would
be a secret that cannot be narrowed, rotated or revoked per caller.

## Decision

`/v1/token` also accepts the OAuth 2.0 `client_credentials` grant (RFC 6749
§4.4), authenticated with a signed client assertion (`private_key_jwt`,
RFC 7523 §2.2). There are no shared client secrets: each client signs with its
own RSA key, and identity holds only the public half.

Clients are registered by the operator in `IDENTITY_SERVICE_CLIENTS_JSON`,
which holds no secret:

```json
{
  "ai-gateway": {
    "subject": "7a1c…-uuid",
    "workspace": "system-workspace-uuid",
    "audiences": ["data-api"],
    "scopes": ["quotas:service"],
    "public_key_pem": "-----BEGIN PUBLIC KEY-----\n…"
  }
}
```

- `client_id`: 3–63 characters, `^[a-z][a-z0-9-]{2,62}$`; at most 32 clients.
- `subject`: the UUID that becomes the token's `sub`. It is the service's
  stable actor id (data records it as `service_actor`). Two clients never share
  one.
- `workspace`: the token's `ws`. A client acts in exactly one workspace. A
  second workspace needs a second client.
- `audiences`: a subset of `IDENTITY_TOKEN_AUDIENCES`.
- `scopes`: the most this client may ever receive. Each is a concrete shared
  scope (`quotas:service`) or a canonical permission.
- `public_key_pem`: RSA, at least 2048 bits.

The registration is the authorization. Issuing a service token does not call
`platform.authorize` and does not touch the database, so it keeps working while
the platform database is unavailable. Changing who may do what is a reviewed
configuration change and a redeploy, which suits the handful of internal
callers this covers.

### Request

```
POST /v1/token
grant_type=client_credentials
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion=<JWT signed RS256 with the client's private key>
audience=data-api
scope=quotas:service        (optional; default: every registered scope)
client_id=ai-gateway        (optional; must match the assertion)
```

The assertion must have:

- `iss` = `sub` = the client id;
- `aud` = the token endpoint `<issuer>/v1/token`, or the issuer itself;
- `iat` and `exp`, with `exp - iat` ≤ 300 s and `exp` in the future;
- a `jti` that is used once.

Identity remembers every `jti` until its assertion expires, so a replay is
refused. The memory is per process, which is enough while identity-api runs
one replica; see "Limits".

### Response

The same shape as token exchange, minus `issued_token_type`. The token
carries:

- `iss`, `aud` (one audience), `sub` = `subject`, `ws` = `workspace`;
- `iat`, `exp` (600 s), `jti`;
- `scope`, the requested scopes, sorted;
- `client_id`.

platform-backend-core verifies it like any other token. Nothing changes in the
services.

### Errors

| Case | Status | `error` |
|---|---|---|
| Unknown client, bad signature, wrong `aud`, expired, too long, replayed `jti`, `client_id` mismatch | 401 | `invalid_client` |
| Audience not registered for this client | 400 | `invalid_target` |
| A scope outside the client's registration | 400 | `invalid_scope` |
| Wrong or missing `client_assertion_type` | 400 | `invalid_request` |

Every `invalid_client` answer is the same, so the response does not say which
check failed. The rate limiter admits client-credentials requests exactly as it
admits token exchange.

## Alternatives rejected

- **Static long-lived JWTs** per service: no rotation, no revocation short of
  rotating identity's signing key.
- **`client_secret_basic`**: a shared secret on both sides, and identity would
  then hold secrets.
- **Database-backed client registry**: needs a migration on the platform
  database, which is over its Neon quota until 2026-11-01. It can replace the
  variable later without changing the wire protocol.
- **Calling `platform.authorize` for service subjects**: service subjects are
  not platform identities with memberships. The registration already states
  the grant.

## Limits

- Replay protection is per process. Before identity-api runs more than one
  replica, move the `jti` memory to the shared PostgreSQL admission store.
- Revoking a client means removing it from the variable and redeploying.
  Tokens already issued live at most 600 s.

## Operating

1. The calling service generates its key pair. The private key goes to that
   service's own secret store (for example Infisical `/platform-ai`), never to
   identity:
   `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out client.pem && openssl pkey -in client.pem -pubout -out client.pub.pem`
2. Add the client to `IDENTITY_SERVICE_CLIENTS_JSON` on identity-api, with a
   new random `subject` UUID, and redeploy.
3. The service signs a fresh assertion for every token request, caches the
   access token, and requests a new one shortly before `expires_in` ends.
