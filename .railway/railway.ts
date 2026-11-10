import { defineRailway, github, project, service } from "railway/iac";
// Railway IaC. Replaces railway.toml / railway.json, which Railway stops reading on 2026-12-01.
// Build, healthcheck, start command, cron and restart policy live on the service; deploys do not read this file.
// Check drift: npm install railway in a scratch parent directory (not this repo), railway link, railway config plan.
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "identity-api";
export default defineRailway(() => {
  const identityApi = service("identity-api", {
    source: github("bookchaowalit-backend/platform-identity"),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/health",
    healthcheckTimeout: 30,
    replicas: { "asia-southeast1-eqsg3a": 1 },
    deploy: { restartPolicyMaxRetries: 3 },
  });

  return project("platform-identity", {
    variables: { managed: false },
    resources: [identityApi],
  });
});
