# SPEC-001 — Tenant lifecycle and delegated service tokens

Standard: ADR-0004 — TDD + SDD (architecture-docs `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 0.1 |
| Status | **draft**: waiting for owner approval |
| Repository / path | bookchaowalit-backend/platform-identity / `specs/001-tenancy-and-delegation/spec.md`; schema part in bookchaowalit/platform-empire `services/platform/migrations/0009_tenancy.sql` |
| Owner (maker) | Claude Code |
| Reviewer (checker) | Fresh Claude reviewer subagent (no maker context) |
| Approver | Owner (bookchaowalit) |
| Requirement links | Owner request 2026-11-11: "every microservice will be used in a SaaS, and one product may use several services together" → gap review items 1 (tenant lifecycle) and 3 (service-to-service calls on behalf of a tenant) |
| Related docs | docs/SERVICE-CLIENTS.md, docs/PLATFORM-SERVICES-CUTOVER.md, platform-empire `0001_platform_core.sql`, `0008_identity_api_role.sql` |
| Main-only | no: branch `spec/001-tenancy-delegation` → PR, merged locally |
| Created / approved | 2026-11-11 / — |

## 1. Problem and intent

- **Problem:**
  1. The schema already supports many tenants (`platform.workspaces`, `memberships`, `roles`, `membership_roles`,
     `delegations`), and every platform service already isolates rows by the token's `ws` claim with forced RLS.
     But **nothing can create a tenant**. identity serves only `/v1/token` and `/v1/authz/decide`; workspaces,
     members and grants are created by operator CLI (`empire_platform.cells grant --workspace owner`). The only
     workspace in production is `owner`. A customer cannot sign up, invite a colleague or be removed.
  2. **A service cannot call another service for a tenant.** `client_credentials` tokens carry one fixed
     workspace per registered client (SERVICE-CLIENTS.md: "a second workspace needs a second client", max 32
     clients). When commerce must call payment, then credits, then communication *for customer X*, there is no way
     to get a token with `ws = X`. Forwarding the customer's own token fails because each service checks `aud`.
  3. Token exchange does not check `workspaces.status`, so a suspended or closed workspace still gets tokens.
- **Outcome:** a person signs in with Google, creates a workspace (or accepts an invitation) and receives tokens
  for it. Admins manage members and roles through the API. A registered service exchanges the token it received
  for a narrower token to the next service, with the same `sub` and `ws` and an `act` chain naming each hop. A
  worker can get a token for any workspace that has installed it. Suspended or closed workspaces get no token.
- **Why now:** this is the base for every other SaaS gap (per-workspace workers, plans and quotas, per-tenant
  events). Without it there is only one customer.

### Decisions

- **D1: identity owns the tenancy API; platform-empire owns the schema.** Writes go through new
  `SECURITY DEFINER` functions in migration 0009, and `identity_api` gets `EXECUTE` on those functions only, still
  with no table write grant. This keeps 0008's rule: the runtime role cannot change tables directly.
- **D2: no new user store.** People stay `platform.identities` (kind `human`, `external_subject` = Google `sub`).
  Google is still the only provider. Tokens keep the current shape (`iss`, `aud`, `sub`, `ws`, `scope`,
  `exp` ≤ 600 s), so **no downstream service needs a change** to accept them.
- **D3: delegation is RFC 8693 token exchange with an actor.** A service client authenticates as today
  (`private_key_jwt`) and sends `subject_token` = an access token this identity issued. The new token can only
  **narrow**: it keeps the subject's `sub` and `ws`, never outlives the subject token, and its scopes are inside
  both the client's registered delegable scopes and what the subject may do.
- **D4: multi-workspace workers need an installation.** A client registered with `"workspace": "*"` may get a
  token for workspace W only if its service-account identity is an active member of W. Signup installs the clients
  listed in `IDENTITY_DEFAULT_INSTALLS`. Removing that membership uninstalls the client.
- **D5: invitations are bound to a verified email and returned to the caller.** The invite code is shown once and
  stored only as an HMAC. Sending it by email through communication-api is a follow-up spec.

## 2. Scope

| In scope | Out of scope (non-goals) |
|---|---|
| Signup: Google id_token → identity + workspace + owner membership + default roles + default installs | Other login providers (email/password, LINE, SAML/SSO) |
| List my workspaces | UI / web frontend for signup and members |
| Invitations: create, accept, revoke | Sending invitation emails (communication-api follow-up) |
| Members: list, change roles, suspend, remove; last owner protected | Plans, quotas, billing per workspace (separate spec: gap item 6) |
| Workspace: rename, suspend, close (owner only) | Data export and delete per workspace (gap item 8) |
| Token exchange refuses non-active workspaces | Changing the 8 platform services (tokens keep their shape) |
| Delegated token exchange (service → service, `act` chain) | Caller library for delegation in platform-backend-core (follow-up; services use plain HTTP until then) |
| `"workspace": "*"` service clients + installation check | Per-workspace worker fan-out in communication/media/data/credits (gap item 2, uses AC-15/AC-16) |
| Service read of active workspaces for installed clients | Workspace domain events on Kafka (later: outbox in 0009 is not built here) |
| Audit row for every tenancy change | Moving the owner workspace or existing grants |

## 3. Behaviour — scenarios

Common rules: every tenancy request is rate-limited like `/v1/token`, answers use the same error body
(`{"error", "error_description"}`), responses carry `Cache-Control: no-store`, and nothing leaks whether an
email, subject or workspace exists outside the caller's own workspace. Admin endpoints take an identity access
token with `aud = identity-api`. Its `ws` is the workspace acted on, and it never comes from the URL or the body.

### Tenant lifecycle

### AC-01 — Sign up creates a workspace

- **Given** `IDENTITY_SIGNUP_MODE=open` and a valid Google id_token (`email_verified = true`) whose subject has no identity
- **When** `POST /v1/signup` with `{id_token, slug, name}`
- **Then** 201 with `{workspace_id, slug, identity_id}`
- **And** one `identities` row (kind `human`), one `workspaces` row (status `active`, `owner_identity_id` = the identity),
  one active membership, roles `owner`/`admin`/`member` created from `IDENTITY_ROLE_TEMPLATES_JSON`, `owner`
  assigned, a membership for each client in `IDENTITY_DEFAULT_INSTALLS`, and one audit event `workspace.created`
- **Test:** `runtime_tests/test_tenancy.py::test_signup_creates_workspace_owner_roles_and_installs` · integration

### AC-02 — Signup modes

- **Given** `IDENTITY_SIGNUP_MODE` is `closed` (the default), or `invite_code` with codes in `IDENTITY_SIGNUP_CODES_SHA256`
- **When** signup is called without a matching code (`invite_code`), or at all (`closed`)
- **Then** 403 `signup_closed` and nothing is written. With a matching code in `invite_code` mode it behaves like AC-01
- **Test:** `tests/service/test_tenancy.py::test_signup_modes` · unit

### AC-03 — Signup limits and idempotency

- **Given** an identity that already owns `IDENTITY_MAX_OWNED_WORKSPACES` workspaces (default 3)
- **When** it signs up again
- **Then** 409 `workspace_limit`. A taken slug → 409 `slug_taken`. A repeat of the same signup (same subject + slug)
  within 24 h → 200 with the same body and no new rows
- **Test:** `runtime_tests/test_tenancy.py::test_signup_limit_slug_and_replay` · integration

### AC-04 — List my workspaces

- **Given** a person who is an active member of workspaces A and B, and suspended in C
- **When** `POST /v1/me/workspaces` with `{id_token}` (POST so the id_token never appears in a URL)
- **Then** 200 with A and B only, each `{workspace_id, slug, name, status, roles}`. A closed workspace is not listed
- **Test:** `runtime_tests/test_tenancy.py::test_list_my_workspaces_only_active_memberships` · integration

### AC-05 — Create an invitation

- **Given** a caller token for workspace W with permission `identity.member:admin`
- **When** `POST /v1/invitations` with `{email, role}` where `role` is a role of W other than `owner`
- **Then** 201 `{invitation_id, code, expires_at}` (7 days). `code` is returned only here; the DB stores
  HMAC(`IDENTITY_INVITE_KEY`, code) and HMAC of the lower-cased email, never either in clear. Audit `member.invited`
- **And** a caller without the permission → 403. `role = owner` → 400 `invalid_role`
- **Test:** `runtime_tests/test_tenancy.py::test_invitation_create_stores_only_hashes` · integration

### AC-06 — Accept an invitation

- **Given** an open invitation to W for `ann@example.com`
- **When** `POST /v1/invitations/accept` with `{id_token, code}`, the id_token's verified email is that address
- **Then** 200 `{workspace_id}`. The identity is created if missing, the membership is active with the invited
  role, the invitation becomes `accepted`, and audit `member.joined` is written
- **And** wrong email, an unverified email, an expired, revoked or used code all → the same 400 `invalid_grant`
- **Test:** `runtime_tests/test_tenancy.py::test_accept_invitation_binds_verified_email` · integration

### AC-07 — Members: list and change

- **Given** an admin token for W
- **When** `GET /v1/members`, then `PATCH /v1/members/{identity_id}` with `{roles?, status?}` (`active`/`suspended`),
  or `DELETE /v1/members/{identity_id}`
- **Then** the list shows only W's members. A change replaces that member's roles or status and writes one audit
  event. A suspended member's next token exchange for W → 400 `invalid_grant`
- **Test:** `runtime_tests/test_tenancy.py::test_members_list_patch_delete` · integration

### AC-08 — The last owner is protected

- **Given** W has exactly one active member with role `owner`
- **When** that member is demoted, suspended or removed (by anyone)
- **Then** 409 `last_owner` and nothing changes. Only an owner may grant or remove the `owner` role
- **Test:** `runtime_tests/test_tenancy.py::test_last_owner_cannot_be_removed` · integration

### AC-09 — Workspace settings and status

- **Given** an owner token for W
- **When** `PATCH /v1/workspace` with `{name?}` or `{status: suspended | active | closed}`
- **Then** 200. `closed` is final (later PATCH → 409). A non-owner → 403. Audit `workspace.updated`
- **Test:** `runtime_tests/test_tenancy.py::test_workspace_patch_owner_only_closed_is_final` · integration

### AC-10 — No tokens for a non-active workspace

- **Given** W is `suspended` or `closed`
- **When** a member exchanges a Google id_token for W, or any service client asks for a W token (AC-12, AC-15)
- **Then** 400 `invalid_grant`, the same body as for a non-member
- **Test:** `runtime_tests/test_tenancy.py::test_token_refused_for_inactive_workspace` · integration

### AC-11 — Cross-workspace isolation

- **Given** an admin token for W1
- **When** it reads, patches or deletes a member, invitation or setting of W2, using W2 ids in the path
- **Then** 404, identical to an unknown id. No W2 row changes
- **Test:** `runtime_tests/test_tenancy.py::test_admin_cannot_touch_other_workspace` · integration

### Delegated service tokens

### AC-12 — Delegate on behalf of the caller

- **Given** service client `commerce-api`, registered with `delegate_from: "commerce-api"`,
  `audiences ⊇ {payment-api}` and `delegable_scopes ⊇ {payments:write}`, and a person token
  `{sub: P, ws: W, aud: commerce-api, scope: "orders:write payments:write"}` issued by this identity
- **When** the client calls `/v1/token` with `grant_type` = token-exchange, `subject_token` = that token,
  `subject_token_type` = access_token, `audience = payment-api`, `scope = payments:write`, and its client assertion
- **Then** 200 with a token `{sub: P, ws: W, aud: payment-api, scope: "payments:write",
  act: {sub: <commerce subject>, client_id: "commerce-api"}}`, and `exp` = min(now + 600, subject `exp`)
- **Test:** `tests/service/test_delegation.py::test_delegated_token_keeps_subject_and_workspace` · unit

### AC-13 — Delegation can only narrow

- **Given** the setup of AC-12
- **When** the requested scope is not in the subject token's scope, or not in the client's `delegable_scopes`; or the
  subject token's `aud` is not the client's `delegate_from`; or the audience is not registered for the client
- **Then** 400 `invalid_scope` / `invalid_grant` / `invalid_target` and no token. A subject token from another
  issuer, expired, or with a bad signature → 400 `invalid_grant`
- **Test:** `tests/service/test_delegation.py::test_delegation_never_widens` · unit

### AC-14 — Chains are bounded and still authorized

- **Given** a subject token that already has `act` (one earlier hop)
- **When** the next service delegates again
- **Then** the new `act` nests the previous one (`act.act`). A chain deeper than 3 hops → 400 `invalid_grant`
- **And** when the subject is a person, identity checks again that P is an active member of an active W (AC-10),
  and that each shared scope is allowed by `platform.authorize`, so removing a member stops the whole chain within
  one token lifetime
- **Test:** `runtime_tests/test_delegation.py::test_chain_depth_and_membership_rechecked` · integration

### AC-15 — Multi-workspace worker token

- **Given** client `communication-worker` registered with `"workspace": "*"`, and its subject is a
  `service_account` identity that is an active member of W1 but not of W2
- **When** it calls `client_credentials` with `workspace = W1`, and then with `workspace = W2`
- **Then** W1 → 200 with `ws = W1`. W2 → 400 `invalid_grant`. Without `workspace` → 400 `invalid_request`.
  A fixed-workspace client keeps today's behaviour and ignores nothing: sending a different `workspace` → 400
- **Test:** `runtime_tests/test_delegation.py::test_star_client_needs_installation` · integration

### AC-16 — Worker lists the workspaces that installed it

- **Given** the client of AC-15 with a token for audience `identity-api` and scope `identity.installation:read`
- **When** `GET /v1/service/installations?after=<cursor>`
- **Then** 200 with the active workspace ids where its own identity is an active member, 500 per page,
  ordered by id. It never lists other clients' installations
- **Test:** `runtime_tests/test_delegation.py::test_installations_list_is_own_and_paged` · integration

### AC-17 — Backward compatibility

- **Given** today's registry (fixed-workspace clients, no `delegate_from`) and today's person token exchange
- **When** the existing test suite runs, and existing clients call `/v1/token`
- **Then** every existing test passes unchanged and the issued claims are identical (`act` only appears on delegated tokens)
- **Test:** `tests/service/test_app.py`, `tests/service/test_service_clients.py` (unchanged) · unit

## 4. Interfaces and data

| Interface | Change | Contract |
|---|---|---|
| `POST /v1/signup` | new | body `{id_token, slug, name, code?}` → 201/200 `{workspace_id, slug, identity_id}`; 400/403/409 |
| `POST /v1/me/workspaces` | new | body `{id_token}` → 200 `{workspaces: [...]}` |
| `POST /v1/invitations` · `DELETE /v1/invitations/{id}` · `GET /v1/invitations` | new | bearer `aud=identity-api`, `identity.member:admin` |
| `POST /v1/invitations/accept` | new | body `{id_token, code}` → 200 `{workspace_id}` |
| `GET /v1/members` · `PATCH`/`DELETE /v1/members/{identity_id}` | new | `identity.member:read` / `identity.member:admin` |
| `PATCH /v1/workspace` | new | `identity.workspace:admin` (owner role only) |
| `GET /v1/service/installations` | new | service token, `identity.installation:read` |
| `POST /v1/token` token-exchange with `subject_token_type=access_token` + client assertion | new | AC-12..AC-14 |
| `POST /v1/token` `client_credentials` + `workspace` | changed | only for `"workspace": "*"` clients (AC-15) |
| `IDENTITY_SERVICE_CLIENTS_JSON` | changed | optional `delegate_from`, `delegable_scopes`; `workspace` may be `"*"` |
| New env | new | `IDENTITY_SIGNUP_MODE`, `IDENTITY_SIGNUP_CODES_SHA256`, `IDENTITY_MAX_OWNED_WORKSPACES`, `IDENTITY_ROLE_TEMPLATES_JSON`, `IDENTITY_DEFAULT_INSTALLS`, `IDENTITY_INVITE_KEY` (secret) |
| identity's own audience `identity-api` | new | added to `IDENTITY_TOKEN_AUDIENCES`; identity verifies its own tokens with its own key |

- **Data model / migration (platform-empire `0009_tenancy.sql`, reversible by a down script):**
  - `platform.invitations (id, workspace_id, email_hmac, code_hmac, role_id, status, expires_at, created_by, accepted_by, created_at)`
    with forced RLS by workspace, like the other tables.
  - `platform.signup_requests (subject_hmac, slug, workspace_id, created_at)` for AC-03 replay.
  - `SECURITY DEFINER` functions, each `SET search_path = platform, pg_temp`, each writing its audit event:
    `tenancy_signup`, `tenancy_my_workspaces`, `tenancy_invite`, `tenancy_revoke_invitation`, `tenancy_accept`,
    `tenancy_set_member`, `tenancy_remove_member`, `tenancy_update_workspace`, `tenancy_installations`,
    `tenancy_member_active` (used by AC-10/14/15).
  - `GRANT EXECUTE` on those functions to `identity_api` only. No table grant changes. Permissions
    `identity.member:read|admin`, `identity.workspace:admin`, `identity.installation:read` are canonical, so
    they pass the `platform.permission` domain.
- **Backward compatibility:** all current callers keep working (AC-17). The `owner` workspace and its grants are untouched.
  Services need no change. Delegated tokens look like normal tokens to them, plus an `act` claim they may log.

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Performance | `/v1/token` delegated p95 < 150 ms warm (one DB round trip for person subjects, none for service subjects); tenancy endpoints < 300 ms warm | timing assertion in runtime tests; manual check after deploy |
| Security / privacy | no email, code or Google subject stored in clear (HMAC); no existence oracle (AC-06, AC-10, AC-11); delegation only narrows (AC-13); chain ≤ 3 (AC-14); last owner protected (AC-08); runtime role still has no table writes | listed ACs + a test that `identity_api` cannot `INSERT` into `platform.memberships` |
| Reliability / observability | one log line per tenancy change with ids only (no email); audit event per change; Sentry keeps the no-free-text rule | runtime tests assert the audit row; log assertion |
| Accessibility / UX | N/A: API only, no UI in this spec | N/A |

## 6. Test plan (TDD)

- **Test levels:** unit (`tests/service/`) for validation, registry parsing and claim building with a fake DB; integration
  (`runtime_tests/`) against a throwaway `postgres:17-alpine` with platform-empire migrations 0001–0009 applied,
  as the real `identity_api` role.
- **Test data:** generated RSA keys, fake Google verifier, random UUIDs, `@example.com` addresses. No production data.
- **Coverage:** keep the repository's current CI gate; this change must not lower it.
- **Not test-first (exceptions):** none.

### Red / green log

| Scenario | Test | Red | Green | Refactor note |
|---|---|---|---|---|
| AC-01 … AC-17 | see §3 | — | — | — |

## 7. Risks and open questions

| # | Risk or question | Impact | Owner | Resolution / date |
|---|---|---|---|---|
| 1 | Signup mode at launch: `closed`, `invite_code` (recommended) or `open`? | who can create a tenant | Owner | — |
| 2 | Default role templates: which permissions do `admin` and `member` get for each product? Proposal: owner = all permissions in the workspace's granted cells, admin = all except `identity.workspace:admin`, member = the `:read`/`:write` permissions of product cells | what a new customer can do | Owner | — |
| 3 | `IDENTITY_GOOGLE_CLIENT_IDS` is still unset, so no person token, signup or acceptance can work in production | blocks go-live, not build | Owner | — |
| 4 | Migration 0009 runs on the `empire` Neon database shared with all life cells; deploying it needs the Railway approval gate | shared DB change | Claude + owner | snapshot branch before migrate |
| 5 | The registry is capped at 32 clients; `"*"` clients remove the need for one client per workspace, so the cap stays | none expected | Claude | — |
| 6 | Revocation is by expiry only (≤ 600 s); a removed member's chain lives at most that long | short exposure | Owner accepts or asks for a deny list | — |

## 8. Change log

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-11-11 | First draft from the SaaS gap review (items 1 and 3) |
