# SPEC-006 — Tenant lifecycle and delegated service tokens

Standard: ADR-0004 — TDD + SDD (architecture-docs `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-006 / 1.1 |
| Status | **in review**: checker R1 revise → 0.3; R2 revise → 0.4; R3 **pass** (3 P2 fixed in 0.5); **approved** by owner 2026-11-12 ("อนุมัติ ใช้ตามแนะนำ"); **implemented** on branches (1.1 records the implementation decisions below) |
| Repository / path | bookchaowalit-backend/platform-identity / `docs/SPEC-006-TENANCY-AND-DELEGATION.md`; schema part in bookchaowalit/platform-empire `services/platform/migrations/0009_tenancy.sql` |
| Owner (maker) | Claude Code (session "Microservice SaaS integration adjustments") |
| Reviewer (checker) | Fresh Claude reviewer subagent (no maker context): R1 revise, R2 revise, R3 pass |
| Approver | Owner (bookchaowalit) |
| Requirement links | Owner request 2026-11-11: "every microservice will be used in a SaaS, and one product may use several services together" → gap review items 1 (tenant lifecycle) and 3 (service-to-service calls for a tenant) |
| Related docs | finance-api `docs/PHASE-6-SAAS.md` (provisioning proposal this spec absorbs; its author agreed 2026-11-12), docs/PAID-AUDIENCES.md (SPEC-005), audience bundles (`IDENTITY_AUDIENCE_BUNDLES_JSON`), docs/SPEC-SHARED-REPLAY-AND-LIMITS.md, docs/SERVICE-CLIENTS.md, platform-empire 0001/0002/0003/0008 |
| Main-only | no: branch `spec/001-tenancy-delegation` → PR, merged locally |
| Created / approved | 2026-11-11 / 2026-11-12 |

## 1. Problem and intent

- **Problem:**
  1. The schema already supports many tenants (`platform.workspaces`, `memberships`, `roles`, `membership_roles`,
     `delegations`), and every platform service already isolates rows by the token's `ws` with forced RLS. But
     **nothing can create a tenant**. identity serves only `/v1/token` and `/v1/authz/decide`; workspaces, members
     and grants are made by operator CLI (`empire_platform.cells grant --workspace owner`). Production has one
     workspace, `owner`.
  2. **A service cannot call another service for a tenant.** A `client_credentials` token carries one fixed
     workspace per client (max 32 clients). When commerce must call payment, then credits, then communication
     *for customer X*, nothing issues a token with `ws = X`. Forwarding the customer's token fails on `aud`.
  3. Token exchange does not check `workspaces.status`; a suspended or closed workspace still gets tokens.
  4. Direct exchange signs canonical-permission scopes without calling `platform.authorize` (only shared scopes are
     checked). Today that is harmless because each cell's PDP call decides, but a delegation built on "the subject
     token's scope" would inherit it (R1 #1).
- **Outcome:** a person signs in with Google, creates a workspace for a product (or accepts an invitation) and gets
  tokens for it. Admins manage members and roles through the API. An installed service exchanges a token it
  received for a narrower token to the next service in the same workspace, carrying who started the call. A worker
  gets a token for any workspace that installed it. Suspended or closed workspaces get no token.
- **Why now:** this is the base for the other SaaS gaps (per-workspace workers, plans and quotas, events per tenant).

### Decisions

- **D1 — identity owns the API; platform-empire owns the schema.** Writes go through `SECURITY DEFINER` functions in
  migration 0009, owned by a NOLOGIN role `platform_tenancy` with `BYPASSRLS`. The migrator cannot create such a
  role (NOSUPERUSER, NOCREATEROLE), so — like every runtime role — it is created **before** the migration by
  `db/bootstrap/roles.sql` locally and by `empire-neon provision` / `ensure_runtime_roles` hosted (Neon's
  `neondb_owner` can grant BYPASSRLS; verified on a branch before rollout). The bootstrap also runs `GRANT platform_tenancy TO empire_migrator` with the migrator `NOINHERIT` toward it
  (needed for `ALTER FUNCTION … OWNER TO`; BYPASSRLS never applies to migrator sessions without `SET ROLE`). 0009 then grants it `USAGE` on the
  schema and exactly the table privileges the functions use, and runs `ALTER FUNCTION … OWNER TO platform_tenancy`
  for each. Each function: `SET search_path = pg_catalog, pg_temp` with schema-qualified names (the existing
  convention); `REVOKE EXECUTE … FROM PUBLIC`;
  `GRANT EXECUTE … TO identity_api` only; it takes the caller's identity and workspace as parameters that identity
  fills **only from a verified token** (never from the request body or path); it sets `app.workspace_id` to that
  workspace itself before touching tenant rows; it writes its audit event in the same transaction. Because the owner bypasses RLS, workspace safety rests on the
  function code, so each function has its own test that it uses only the token-derived identity and workspace.
  identity calls them in a committing transaction (today's `bound()` always rolls back; tenancy writes get a
  `bound_write()` that commits on success). `identity_api` keeps no table write grant (0008's rule).
- **D2 — no new user store.** People stay `platform.identities` (kind `human`, `external_subject` = Google `sub`).
  Google is still the only provider.
- **D3 — delegation follows the platform's existing claim meaning, not a new one.** In this platform `sub` is the
  identity *performing* the call and `act.sub` is the identity that *initiated* it (`auth.py`: `initiated_by`;
  `check_execution` requires the initiator to be an active member). So a delegated token is
  `{sub: <service account>, ws: W, act: {sub: <initiator>}, via: [<client ids>]}`. Downstream PDP checks run for the installed service account in W (`platform.authorize` uses `sub`; it does not
  see `act`). Because the PDP never checks the initiator, **identity does it at every hop**: each requested scope
  must be allowed by `platform.authorize` for the initiator P in W **and** for the service account in W, so a
  delegated token never exceeds what both may do now. Only person-origin chains are delegable: the subject token's
  `sub` (or, for a delegated subject, its `act.sub`) must be a `human` identity; worker-origin tokens are refused. Services that
  refuse delegated tokens (finance-api: `delegated_token_refused` for `del`/`obo`/`act`) keep refusing them, because
  `act` is always present.
- **D4 — an installation is a membership of the service account.** A client registered with `"workspace": "*"` (or
  any client used for delegation) has a `platform.identities` row (kind `service_account`, id = its registered
  `subject`). It is *installed* in W when that identity is an active member of W with the role `installation:<client_id>`,
  created from the product's installation template (a fixed permission list, never edited through the members API).
  Signup installs the product's default clients. Uninstall = remove that membership.
- **D5 — invitations are bound to a verified email and returned to the caller once.** Stored as HMACs only. Email
  delivery through communication-api is a follow-up spec.
- **D6 — sign-up is per product (absorbs finance-api PHASE-6-SAAS.md).** `POST /v1/signup` names a `product`. One
  workspace per (identity, product); a repeat returns it. The product fixes role and installation templates.
  `IDENTITY_SIGNUP_PRODUCTS` is the kill switch (empty = off, the default).
- **D7 — delegation keeps every existing gate.** Paid-audience ceilings (SPEC-005) apply to delegated tokens; a
  delegated token is never a bundle token; `identity-api` is never a delegation target.

## 2. Scope

| In scope | Out of scope (non-goals) |
|---|---|
| Signup per product: identity + workspace + owner membership + role templates + default installations | Other login providers (email/password, LINE, SAML) |
| List my workspaces | Signup / members UI |
| Invitations: create, list, revoke, accept | Sending invitation emails (communication-api follow-up) |
| Members: list, change roles, suspend, reactivate, remove; owner role humans only; last owner protected | Plans, quotas, billing per workspace (finance-api SPEC-005 covers finance; generic = gap item 6) |
| Workspace: rename, suspend, reactivate, close (owner only) | PDPA export/delete per workspace (gap item 8) |
| Exchange refuses non-active workspaces and non-active identities | Changing the 8 platform services |
| Direct exchange authorizes canonical scopes too (closes R1 #1 for both paths) | Caller library for delegation in platform-backend-core (follow-up) |
| Delegated exchange for installed service clients | Worker fan-out in communication/media/data/credits (gap item 2; uses AC-20/AC-21) |
| `"workspace": "*"` clients + installation check | Workspace domain events on Kafka |
| Installations read for service clients | Shared replay store and limits across replicas (docs/SPEC-SHARED-REPLAY-AND-LIMITS.md) |

## 3. Behaviour — scenarios

Common rules (each has its own test in `tests/service/test_tenancy_common.py`):
- **R-a** every tenancy endpoint uses the `/v1/token` per-address limiter, and `POST /v1/signup` also a global
  limit `IDENTITY_SIGNUP_PER_HOUR` (default 20);
- **R-b** errors use `{"error", "error_description"}` and `Cache-Control: no-store`;
- **R-c** admin endpoints take an access token issued by this identity with `aud` containing `identity-api`,
  verified by a second verifier for identity's own issuer and key (not the platform verifier); its `ws` is the
  workspace acted on;
- **R-d** every admin call re-checks in the database that the caller is an active identity with an active membership
  in an active W and holds the permission now (a removed admin's 600 s token stops working at once);
- **R-e** no answer reveals whether an email, Google subject, workspace or invitation exists outside the caller's own
  workspace; the "not found / not allowed / wrong" outcomes of one endpoint share one status and body. Slugs are public
  names, so `slug_taken` is an accepted disclosure.

Permissions used (canonical, so they pass the `platform.permission` domain): `identity.member:read`,
`identity.member:admin`, `identity.workspace:admin`, `identity.installation:read`.

### Tenant lifecycle

### AC-01 — Sign up creates a workspace

- **Given** `finance` in `IDENTITY_SIGNUP_PRODUCTS`, `IDENTITY_SIGNUP_MODE=open`, and a valid Google id_token with
  `email_verified = true` whose subject has no identity
- **When** `POST /v1/signup` `{id_token, product: "finance", slug, name}`
- **Then** 201 `{workspace_id, slug, identity_id, created: true}`
- **Test:** `runtime_tests/test_tenancy.py::test_signup_creates_workspace` · integration

### AC-02 — Signup writes the full tenant in one transaction

- **Given** AC-01
- **Then** exactly: one `identities` row (human, active); one `workspaces` row (active, owner = that identity,
  `product = finance`); an active membership; roles `owner`, `admin`, `member` with the product template's permissions;
  `owner` assigned; one installation membership + role per client in the product's `IDENTITY_DEFAULT_INSTALLS`;
  audit `workspace.created`. A failure in any step leaves no row
- **Test:** `runtime_tests/test_tenancy.py::test_signup_rows_and_atomicity` · integration

### AC-03 — Signup gates

- **When** the product is not in `IDENTITY_SIGNUP_PRODUCTS`, or the mode is `closed` (default), or the mode is
  `invite_code` and the code's SHA-256 is not in `IDENTITY_SIGNUP_CODES_SHA256` (constant-time compare), or the
  id_token's email is not verified
- **Then** 403 `signup_closed` (unverified email: 400 `invalid_grant`), nothing written
- **Test:** `tests/service/test_tenancy.py::test_signup_gates` · unit

### AC-04 — Signup idempotency per (subject, product)

- **Given** the subject already has a workspace for `finance`
- **When** it signs up again for `finance`, with any slug
- **Then** 200 `{workspace_id, slug, identity_id, created: false}` with the existing workspace's slug; no row added.
  Key = (`subject_hmac`, `product`), unique in `platform.signups`
- **Test:** `runtime_tests/test_tenancy.py::test_signup_replay_returns_existing` · integration

### AC-05 — Signup conflicts and limits

- **When** the slug is taken → 409 `slug_taken`. Two concurrent signups with the same new slug → one 201, one 409.
  The subject already owns `IDENTITY_MAX_OWNED_WORKSPACES` (default 3) → 409 `workspace_limit`. Global hourly limit
  reached → 429 `slow_down`
- **Test:** `runtime_tests/test_tenancy.py::test_signup_conflicts_and_limits` · integration

### AC-06 — List my workspaces

- **Given** a person active in A and B, suspended in C, and active in a closed D
- **When** `POST /v1/me/workspaces` `{id_token}`
- **Then** 200 with A and B only, each `{workspace_id, slug, name, product, status, roles}`. An invalid id_token →
  400 `invalid_grant`. An unknown subject → 200 with an empty list (same shape as a member of nothing)
- **Test:** `runtime_tests/test_tenancy.py::test_list_my_workspaces` · integration

### AC-07 — Create and list invitations

- **Given** an admin token for W (`identity.member:admin`)
- **When** `POST /v1/invitations` `{email, role}`, `role` ∈ W's `admin` or `member`
- **Then** 201 `{invitation_id, code, expires_at}` (7 days). `code` = 32 random bytes, base64url, shown only here.
  Stored: HMAC-SHA256(`IDENTITY_INVITE_KEY`, code) and HMAC-SHA256(`IDENTITY_INVITE_KEY`, lower-cased email). Audit
  `member.invited`. `GET /v1/invitations` lists W's open invitations without code or email (only a 4-char email hint
  stored at creation, e.g. `an…@e…`)
- **And** `role = owner` or an `installation:*` role → 400 `invalid_role`; the email already belongs to an active
  member of W → 201 anyway (no membership oracle) and acceptance becomes a no-op success
- **Test:** `runtime_tests/test_tenancy.py::test_invitation_create_and_list` · integration

### AC-08 — Accept an invitation, once

- **Given** an open invitation to W for `ann@example.com`
- **When** `POST /v1/invitations/accept` `{id_token, code}` and the id_token's verified email is that address
- **Then** 200 `{workspace_id}`; identity created if missing; membership active with the invited role; the
  invitation moves `open → accepted` with `UPDATE … WHERE status = 'open' AND expires_at > now() RETURNING` so two
  concurrent accepts give one success; audit `member.joined`
- **And** wrong email, unverified email, unknown, expired, revoked or used code, or W not active → the same 400
  `invalid_grant`, after the same work (HMAC + one indexed lookup) so timing does not separate them
- **Test:** `runtime_tests/test_tenancy.py::test_accept_invitation_once` · integration

### AC-09 — Revoke an invitation

- **When** an admin `DELETE /v1/invitations/{id}` for an open invitation of W → 204, status `revoked`, audit.
  An id of another workspace or an unknown id → 404
- **Test:** `runtime_tests/test_tenancy.py::test_revoke_invitation` · integration

### AC-10 — Members: list and change

- **Given** an admin token for W
- **When** `GET /v1/members` (`identity.member:read`), `PATCH /v1/members/{identity_id}` `{roles?, status?}`
  (`active`/`suspended`), `DELETE /v1/members/{identity_id}`
- **Then** the list holds only W's members with `{identity_id, kind, display_name, status, roles}`; a change replaces
  roles or status and writes one audit event. A suspended member's next exchange for W → 400 `invalid_grant`;
  reactivating restores it
- **And** for a `service_account` member, PATCH → 400 `installation_fixed`; DELETE = uninstall (allowed). PATCH
  that gives a human an `installation:*` role → 400 `invalid_role`; giving `owner` follows AC-11
- **Test:** `runtime_tests/test_tenancy.py::test_members_list_patch_delete` · integration

### AC-11 — Owner role and last owner

- **Then** only an active owner may grant or remove `owner`; `owner` may only be held by an active `human`
  member. Demoting, suspending or removing the last active human owner → 409 `last_owner`. The function locks W's
  owner rows (`SELECT … FOR UPDATE`) first, so two concurrent demotions of two owners leave at least one
- **Test:** `runtime_tests/test_tenancy.py::test_owner_rules_and_concurrent_demotion` · integration

### AC-12 — Workspace settings and status

- **Given** an owner token for W (`identity.workspace:admin`, held only by `owner`)
- **When** `PATCH /v1/workspace` `{name?}` or `{status: suspended | active | closed}`
- **Then** 200; `closed` is final (any later PATCH → 409 `workspace_closed`); a non-owner → 403; audit
  `workspace.updated`. Reactivation is the one exception to "active W only": `platform.authorize` refuses everything in a
  non-active W, so for a **suspended** W the owner's exchange may ask for `identity.workspace:admin` alone, `/v1/token` skips AC-15's `authorize` call for exactly that one scope and
  instead `tenancy_active` checks an active human `owner` membership of the suspended W; `tenancy_update_workspace`
  checks the same directly.
  No other endpoint or scope works in a suspended W. Tests: `test_workspace_patch_status` (owner reactivates; admin cannot) and
  `tests/service/test_tenancy.py::test_suspended_owner_exchange_only_workspace_admin` (at `/v1/token`: the owner gets
  that scope alone, any other scope or a non-owner → `invalid_grant`)
- **Test:** `runtime_tests/test_tenancy.py::test_workspace_patch_status` · integration

### AC-13 — No tokens for a non-active workspace or identity

- **Given** W suspended or closed, or the identity disabled
- **When** a member exchanges a Google id_token for W (except AC-12's owner case), or any client asks for a W
  token (AC-16, AC-20)
- **Then** 400 `invalid_grant`, the same body as a non-member
- **Test:** `runtime_tests/test_tenancy.py::test_token_refused_inactive` · integration

### AC-14 — Cross-workspace isolation

- **Given** an admin token for W1
- **When** it reads or changes a member, invitation or setting of W2 using W2 ids
- **Then** 404, as for an unknown id; no W2 row changes. Every tenancy function called with `app.workspace_id`
  unset or a workspace argument that differs from the token's → error, nothing written
- **Test:** `runtime_tests/test_tenancy.py::test_isolation_and_fail_closed` · integration

### AC-15 — Direct exchange authorizes every requested scope

- **Given** a member whose roles do not include `payments.intent:write`
- **When** it exchanges with `scope = payments.intent:write`
- **Then** 403 `invalid_scope` (today it is signed). Allowed canonical and shared scopes still work; more than 32
  scopes → 400. A request with no `scope` still yields a scope-less token, as today
- **Test:** `runtime_tests/test_tenancy.py::test_direct_exchange_authorizes_canonical_scopes` · integration

### Delegated service tokens

Registry changes (`IDENTITY_SERVICE_CLIENTS_JSON`, parse errors stop identity from starting):
`workspace` may be `"*"`; new optional `delegate_from` (one audience, and no two clients may share it),
`delegable_scopes` (⊆ `scopes`), `products` (where it may be installed). A delegation target may not be
`identity-api` or a bundle audience.

### AC-16 — Delegate on behalf of the initiator

- **Given** client `commerce-api` (`workspace: "*"`, `delegate_from: commerce-api`, `audiences ⊇ {payment-api}`,
  `delegable_scopes ⊇ {payments.intent:write}`) installed in W (a delegating client is always a `"*"` client,
  because the token's workspace comes from the subject), P a human member allowed `payments.intent:write`, and a subject token issued by this identity
  `{sub: P, ws: W, aud: [commerce-api, platform-api], scope: "commerce.order:write payments.intent:write"}`
- **When** the client calls `/v1/token` with the token-exchange grant, `subject_token_type` = access_token,
  `audience = payment-api`, `scope = payments.intent:write`, and its client assertion
- **Then** 200 with `{sub: <commerce-api subject>, ws: W, aud: <same form as a direct exchange: [payment-api, platform-api]
  when payment-api is a cell audience, otherwise "payment-api">, scope: "payments.intent:write", act: {sub: P}, via: ["commerce-api"]}` and `exp` = min(now + 600,
  subject `exp`); a bundle subject token (300 s) therefore yields ≤ 300 s
- **Test:** `tests/service/test_delegation.py::test_delegated_token_claims` · unit

### AC-17 — Delegation only narrows

- **Then** refused, with no token, when: a requested scope is missing from the subject token's `scope`; the subject
  token has **no** `scope` (unrestricted tokens cannot be delegated → 400 `invalid_scope`); a scope is outside
  `delegable_scopes`; `platform.authorize` does not allow it for the initiator P in W or for the service account in W; the chain's
  initiator is not a `human` (a worker's `client_credentials` token as subject → `invalid_grant`); `delegate_from` is not in
  the subject `aud`; the target is not in `audiences`, is `identity-api` or a bundle → `invalid_target`; the subject
  token is from another issuer, expired, badly signed, or itself a `client_credentials` token of a fixed-workspace
  client → `invalid_grant`
- **Test:** `tests/service/test_delegation.py::test_delegation_never_widens` (one sub-test per refusal) · unit + integration

### AC-18 — Chains, re-checks and paid audiences

- **Given** a subject token that is itself delegated (`via` has 1 entry)
- **Then** the next hop keeps `act.sub` = the original initiator and appends to `via`; `via` may hold at most 3
  client ids, a 4th hop → 400 `invalid_grant`. Every hop re-checks: the initiator is an active identity and active
  member of active W; the calling service account is installed in W; each scope is authorized for both (D3), so
  demoting P stops the next hop. A target in
  `IDENTITY_ENTITLEMENT_CEILINGS_JSON` (or a member of such a bundle) gets SPEC-005's ceiling when W has no
  current pass
- **And** uninstalling the client or removing the initiator makes the next hop fail; existing hop tokens die at
  `exp` (≤ 600 s; tested with a fake clock)
- **Test:** `runtime_tests/test_delegation.py::test_chain_recheck_and_ceiling` · integration

### AC-19 — Downstream sees a normal token

- **When** a delegated token from AC-16 reaches a cell, which calls `/v1/authz/decide` and then
  `platform.begin_execution`
- **Then** decide answers for the service account only (`allow` for a permission its installation role holds,
  `deny` otherwise; decide ignores `act`, as today); `begin_execution(initiated_by = P, executed_by = service account,
  on_behalf_of = service account)` succeeds while P is an active member and fails after P is removed
- **Test:** `runtime_tests/test_delegation.py::test_delegated_token_through_decide_and_execution` · integration

### AC-20 — Multi-workspace worker token

- **Given** `communication-worker` registered with `"workspace": "*"`, installed in W1, not in W2
- **When** `client_credentials` with `workspace = W1`, then `W2`, then none
- **Then** W1 → 200 `ws = W1` (scopes ≤ registry `scopes` and authorized for it in W1); W2 → 400 `invalid_grant`;
  none → 400 `invalid_request`
- **Test:** `runtime_tests/test_delegation.py::test_star_client_needs_installation` · integration

### AC-21 — List my installations

- **Given** the client of AC-20 with a token `aud identity-api`, scope `identity.installation:read`
- **When** `GET /v1/service/installations?after=<workspace uuid>&limit=500`
- **Then** 200 `{workspaces: [ids ordered ascending], next: <last id> | null}` for active W where its own identity
  (`sub`) is installed; an `after` that is not a UUID → 400 `invalid_request`
- **Test:** `runtime_tests/test_delegation.py::test_installations_paged_and_own` · integration

### AC-22 — Backward compatibility

- **Then** existing registries (fixed workspace, no new keys) parse unchanged; person exchange, bundle and
  paid-audience behaviour and existing tests pass unchanged except AC-15's refusal of unauthorized canonical
  scopes, which is listed as the one intended change (owner risk 7)
- **And** a fixed-workspace client that now also sends `workspace`: equal to its own → accepted; different → 400
  `invalid_request` (today the field is ignored; no current caller sends it — checked in the 5 caller repos before deploy)
- **Test:** `tests/service/test_app.py`, `tests/service/test_service_clients.py` + `test_registry_new_keys` · unit

### AC-23 — Install and reinstall

- **Given** an admin token for W of product `finance`
- **When** `POST /v1/installations` `{client_id}` for a client whose registry `products` includes `finance`
- **Then** 201 and the client is installed with the product's installation template (or 200 if already installed),
  audit `installation.created`; the caller must hold every permission in that template itself (`platform.authorize`
  for each), otherwise 403 `insufficient_privilege`, so installing never grants more than the admin has;
  a client not allowed for the product → 400 `invalid_client`. This is also how existing workspaces get a client
  that a product adds later
- **Test:** `runtime_tests/test_tenancy.py::test_install_and_reinstall`, `::test_install_needs_template_permissions` · integration

## 4. Interfaces and data

| Interface | Change | Contract |
|---|---|---|
| `POST /v1/signup` | new | `{id_token, product, slug, name, code?}` → 201/200 `{workspace_id, slug, identity_id, created}` |
| `POST /v1/me/workspaces` | new | `{id_token}` → 200 `{workspaces: [...]}` |
| `POST`, `GET /v1/invitations`; `DELETE /v1/invitations/{id}` | new | `identity.member:admin` |
| `POST /v1/invitations/accept` | new | `{id_token, code}` → 200 `{workspace_id}` |
| `GET /v1/members`; `PATCH`, `DELETE /v1/members/{identity_id}` | new | `identity.member:read` / `:admin` |
| `PATCH /v1/workspace` | new | `identity.workspace:admin` |
| `POST /v1/installations` | new | `identity.member:admin`; AC-23 |
| `GET /v1/service/installations` | new | `identity.installation:read` |
| `POST /v1/token`: token exchange, `subject_token_type=access_token` + client assertion | new | AC-16..AC-19 |
| `POST /v1/token`: `client_credentials` + `workspace` | changed | AC-20, AC-22 |
| `POST /v1/token`: direct exchange | changed | AC-13, AC-15 |
| Env | new | `IDENTITY_SIGNUP_PRODUCTS`, `IDENTITY_SIGNUP_MODE`, `IDENTITY_SIGNUP_CODES_SHA256`, `IDENTITY_SIGNUP_PER_HOUR`, `IDENTITY_MAX_OWNED_WORKSPACES`, `IDENTITY_ROLE_TEMPLATES_JSON` and `IDENTITY_DEFAULT_INSTALLS` (keyed by product), `IDENTITY_INVITE_KEY` (secret, rotation: `IDENTITY_INVITE_KEY_PREVIOUS` is also tried on accept for 7 days) |
| Audience `identity-api` | new | added to `IDENTITY_TOKEN_AUDIENCES`, not a cell audience, never a delegation target |

- **Migration 0009 (platform-empire, with a down script):** role `platform_tenancy` (NOLOGIN, BYPASSRLS);
  `platform.workspaces.product text`; `platform.signups (subject_hmac, product, workspace_id, created_at,
  PRIMARY KEY (subject_hmac, product))`; `platform.invitations (id, workspace_id, email_hmac, email_hint, code_hmac
  UNIQUE, role_id, status, expires_at, created_by, accepted_by, created_at)` with forced RLS by workspace; the
  functions `tenancy_signup`, `tenancy_my_workspaces`, `tenancy_invite`, `tenancy_list_invitations`,
  `tenancy_revoke_invitation`, `tenancy_accept`, `tenancy_list_members`, `tenancy_set_member`,
  `tenancy_remove_member`, `tenancy_update_workspace`, `tenancy_installations`, `tenancy_active` (AC-13/18/20 check)
  per D1. A snapshot branch of the `empire` Neon project is taken before it runs.
- **Backward compatibility:** AC-22. The `owner` workspace and its grants stay; `owner` workspace gets
  `product = NULL`. Services need no change.

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Performance | delegated exchange p95 < 150 ms warm (one DB round trip: `tenancy_active` + authorize in one call); tenancy endpoints < 300 ms warm | timing assertion in runtime tests; check after deploy |
| Security / privacy | HMAC only for email, code, subject; R-e no oracle; AC-17 narrowing; AC-18 chain ≤ 3 hops; AC-11 owner rules; `identity_api` still cannot `INSERT` into any `platform.*` table | listed ACs + `test_identity_api_has_no_table_writes` |
| Reliability / observability | one log line per tenancy change with ids only; audit row per change; Sentry no-free-text rule kept | runtime tests assert audit rows; log assertion |
| Accessibility / UX | N/A: API only | N/A |

## 6. Test plan (TDD)

- **Test levels:** unit (`tests/service/`) with fake DB and fake Google; integration (`runtime_tests/`) against a
  throwaway `postgres:17-alpine` with platform-empire migrations 0001–0009, connecting as the real `identity_api` role.
- **Test data:** generated RSA keys, random UUIDs, `@example.com` addresses. No production data.
- **Coverage:** keep the repository's CI gate; this change must not lower it.
- **Not test-first (exceptions):** none.

### Implementation decisions (1.1)

- **I1 — no BYPASSRLS.** `platform_tenancy` is NOLOGIN, NOBYPASSRLS, NOINHERIT. Cross-workspace reads use
  SELECT-only policies `TO platform_tenancy` (identities, workspaces, memberships, roles, membership_roles,
  signups, invitations). Writes stay under the workspace policies, because each function sets `app.workspace_id`
  itself. This is risk 8's fallback, chosen up front so Neon needs no special attribute.
- **I2 — the migrator holds `platform_tenancy` WITH INHERIT FALSE, SET TRUE** (PG16 needs SET for
  `ALTER FUNCTION … OWNER TO`). 0009 grants the role `CREATE` on the schema only for the hand-over, then revokes it.
  ACLs are set before the hand-over, because a revoke by a non-owner is a no-op.
- **I3 — off switch.** `IDENTITY_TENANCY_MODE=off` (default) keeps today's behaviour exactly, AC-13/AC-15
  included. `on` needs 0009 in the database. Deploy order: migration first, then identity with `on`.
- **I4 — the AC-21 listing authenticates with the client's own assertion.** `POST /v1/service/installations`
  takes a form `client_assertion` instead of a bearer token. A `"*"` client has no workspace to put in a token
  before it knows its installations. It still consumes the `jti`.
- **I5 — installation template = the client's registered `scopes`** (shared scopes mapped to their permission).
  `IDENTITY_DEFAULT_INSTALLS` and `products` decide where a client may be installed.
- **I6 — integration tests live in platform-empire**, like the existing identity parity tests:
  `tests/test_tenancy.py` (SQL, 29 tests) and `tests/test_identity_tenancy.py` (identity HTTP against 0009,
  7 tests; skipped when `platform_identity` is not installed). Unit tests in identity:
  `tests/service/test_tenancy_config.py`.
- **I7 — delegated tokens carry no `client_id` claim.** That claim marks a `client_credentials` token, which is
  refused as a subject. `via` names the clients instead.

### Red / green log

| Scenario | Test | Red | Green | Refactor note |
|---|---|---|---|---|
| AC-01..AC-14, AC-20, AC-21, AC-23 (SQL) | platform-empire `tests/test_tenancy.py` | platform-empire b2df9db · `pytest tests/test_tenancy.py` · `function platform.tenancy_signup(...) does not exist` (27 failed) | 9cbe027 local: 543 + 29 passed; PR CI run | ACL order fixed after a test caught PUBLIC EXECUTE |
| Tenancy owner role (D1, I2) | platform-empire `tests/test_neon.py::test_tenancy_owner_role_is_nologin_and_settable_by_the_migrator` | b2df9db · `(True, True, …) != (False, False, …)` | 9cbe027 | — |
| AC-01..AC-23 over HTTP | platform-empire `tests/test_identity_tenancy.py` | **not test-first**: written after the identity code (exception E1 below) | local 7 passed; full suite 554 passed | first run caught 2 bugs: header annotation scope (FastAPI) and rate limiter in tests |
| AC-22 registry, settings, person | identity `tests/service/test_tenancy_config.py` | **not test-first** (exception E1) | local 117 passed | — |

**Exception E1 (needs owner approval in the DoD):** the identity HTTP layer and its unit tests were written
together, not red first. The SQL rules, where the security decisions live, were red first. The HTTP tests were
then run against the real database and caught two real bugs before any commit.

## 7. Risks and open questions

| # | Risk or question | Impact | Owner | Resolution / date |
|---|---|---|---|---|
| 1 | Signup mode at launch: `closed`, `invite_code` (recommended) or `open`? | who can create a tenant | Owner | `invite_code` (owner 2026-11-12) |
| 2 | Role templates per product. Proposal for `finance`: owner = the ten cells' read/write/delete + `finance-api` + the `identity.*` permissions; admin = owner minus `identity.workspace:admin`; member = cells' read/write | what a customer can do | Owner | as proposed (owner 2026-11-12) |
| 3 | `IDENTITY_GOOGLE_CLIENT_IDS` still unset: no person token, signup or acceptance works in production | blocks go-live, not build | Owner | — |
| 4 | Migration 0009 runs on the shared `empire` database; deploy goes through the Railway approval gate | shared DB change | Claude + owner | snapshot branch first |
| 5 | Repo ownership: platform-identity / platform-empire were treated as owned by a "finance-suite peer" session; confirm this session may write to them | parallel edits | Owner | this session may write, on branches + PR; no deploy without a second approval (owner 2026-11-12) |
| 6 | Revocation is by expiry (≤ 600 s; admin calls re-check, R-d) | short exposure of downstream hop tokens | Owner accepts | — |
| 7 | AC-15 makes direct exchange refuse canonical scopes the person lacks; a caller that asked for more than it holds would now get 403 | behaviour change | Owner | accepted (owner 2026-11-12); callers checked before deploy |
| 8 | `platform_tenancy` needs BYPASSRLS on Neon; checked on a branch before rollout, else fall back to explicit per-function policies | migration plan | Claude | — |

## 8. Change log

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-11-11 | First draft from the SaaS gap review (items 1 and 3) |
| 0.2 | 2026-11-12 | Renumbered SPEC-006; absorbed finance-api per-product sign-up (D6); bundle and paid-audience rules (D7) |
| 0.3 | 2026-11-12 | R1 fixes: D1 owner role + fail-closed functions; D3 claim meaning aligned with `auth.py` (`sub` = service account, `act.sub` = initiator, `via` chain); D4 installation = templated membership; AC-15 authorize every scope; scope-less tokens not delegable; R-a..R-e; atomic accept, owner locking, signup key; split ACs; new scenarios (concurrent slug, reactivate, expired code, existing member, uninstall mid-chain, PDP pass-through) |
| 0.4 | 2026-11-12 | R2 fixes: identity authorizes every scope for the initiator and the service account at each hop (PDP never sees `act`); AC-19 rewritten around decide + begin_execution; `platform_tenancy` created by bootstrap/provision, functions re-owned, `pg_catalog` search_path, committing write path; suspended-W reactivation exception; person-origin chains only, delegating clients are `"*"`; audience form pinned; installation roles refused for humans; AC-23 install/reinstall; slugs public |
| 0.5 | 2026-11-12 | R3 P2 fixes: suspended-owner exchange bypass is explicit at `/v1/token`; install requires the caller to hold the template's permissions + audit; migrator granted `platform_tenancy` NOINHERIT; ordering |
| 1.0 | 2026-11-12 | Owner approved with the recommended answers to risks 1, 2, 5, 7 |
| 1.1 | 2026-11-12 | Implementation decisions I1–I7, red/green log, exception E1 |
