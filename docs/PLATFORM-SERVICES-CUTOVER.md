# Book Platform services trust this issuer (decision 2026-10-29)

The owner chose platform-identity as the issuer for the seven Book Platform
services (communication, media, data, workflow, commerce, logistics,
developer). They authorize with concrete shared scopes such as `messages:write`.
Identity mints a shared scope only after `platform.authorize` allows its mapped
canonical permission (see [SHARED-RATE-LIMIT.md](SHARED-RATE-LIMIT.md),
"Explicit shared scope bridge").

## Done on 2026-10-29

- The seven services verify tokens with `JWT_ISSUER=https://identity-api-production-fad1.up.railway.app`
  and `JWT_JWKS_URL=<issuer>/.well-known/jwks.json`, audience `<service>-api`. No
  token from the old `empire-platform` issuer is accepted by them any more; none
  was in use.
- The canonical permissions and the scope map live in platform-empire
  `src/empire_platform/permissions.py` (`PLATFORM_SHARED_SCOPES`, 31 scopes; each
  service's owner permissions are grantable with `empire-cells grant`), in
  platform-empire PR #17 together with the seven default audiences.
- identity-api has these variables staged without a deploy:
  `IDENTITY_TOKEN_AUDIENCES=tasks-api,communication-api,media-api,data-api,workflow-api,commerce-api,logistics-api,developer-api`
  and `IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON` generated from
  `PLATFORM_SHARED_SCOPES` (validated with `validate_shared_scope_permissions`).
  The live deployment is still the 2026-10-18 build.

## Activation, after the Neon `empire-platform` quota resets (2026-11-01)

1. Merge platform-empire PR #17 locally and let it deploy (approve the Railway
   deployment if it waits at NEEDS_APPROVAL).
2. Grant the owner permissions, once per service:
   `railway run --service token-rotate -- python -m empire_platform.cells grant --workspace owner --cell <service>`.
3. Owner: create a Google OAuth client ID and set `IDENTITY_GOOGLE_CLIENT_IDS`
   on identity-api. Without it `/v1/token` rejects every Google token.
4. Deploy identity-api from `main` (it is not connected to GitHub: `railway up`
   of a clean `git archive main`). This ships the shared scope bridge and picks up
   the staged variables. Check `/health`, `/readyz` and the JWKS.
5. Exchange a Google id_token at `POST /v1/token`
   (`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
   `subject_token_type=urn:ietf:params:oauth:token-type:id_token`,
   `audience=<service>-api`, `scope=<shared scopes>`) and call each service once
   (send a Telegram message, upload a file, set a flag, place an order...).

## Not covered yet

- **Service-to-service tokens.** platform-data's `quotas:service` (used by the AI
  gateway with `ws` = `PLATFORM_SYSTEM_WORKSPACE_ID`) and the workflow worker's
  `jobs:work` token have no machine path here: `/v1/token` exchanges a person's
  Google id_token only. A service credential flow needs its own decision.
- **Cells.** The existing cells still trust `empire-platform`; their cutover is
  the design's step 6 and is unchanged by this note.
