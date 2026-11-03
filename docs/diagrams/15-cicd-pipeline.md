# 15 — CI/CD pipeline

workflow เดียว: `.github/workflows/check.yml` (name `Check`) เรียก reusable workflows ของ `bookchaowalit/platform-ci-workflows` pin ที่ `87dc5e1c` (v1.1.1); public repo จึงใช้ GitHub-hosted runner (default `ubuntu-24.04`)

```mermaid
flowchart TD
  T1["push to main"] --> W["Check workflow, permissions contents read, concurrency check-ref cancel-in-progress"]
  T2["pull_request"] --> W
  T3["workflow_dispatch"] --> W
  W --> J1["service: ci-python.yml, matrix Python 3.11 and 3.12"]
  W --> J2["admission: ci-python.yml, Python 3.12, postgres true"]
  W --> J3["security: security.yml"]
  J1 --> S1["Checkout, uv + Python, install deps"]
  S1 --> S2["Ruff lint (format-check false)"]
  S2 --> S3["Typecheck: mypy 2.4.0 on src"]
  S3 --> S4["Test: scripts/check.py, check_schema_pin.py, unittest discover tests"]
  J2 --> A1["Start PostgreSQL, install deps"]
  A1 --> A2["pytest -q runtime_tests with PLATFORM_TEST_DSN"]
  A2 --> A3["Drop CI database"]
  J3 --> X1["gitleaks"]
  J3 --> X2["semgrep"]
  J3 --> X3["osv-scanner"]
  J3 --> X4["trivy-fs"]
  S4 --> END["Green check, no deploy"]
  A3 --> END
  X1 --> END
  END -.->|"manual"| DEP["Operator: railway up from git archive main"]
```

Sources: `.github/workflows/check.yml`; `platform-ci-workflows` @ `87dc5e1c`: `.github/workflows/ci-python.yml` (steps Checkout, Set up uv and Python, Install dependencies, Start PostgreSQL, Lint (Ruff), Typecheck, Test, Coverage gate, Drop CI database), `.github/workflows/security.yml` (jobs gitleaks, semgrep, osv-scanner, trivy-fs)

หมายเหตุ:
- `coverage-threshold` ไม่ได้ตั้งใน `check.yml` (default `""` = gate ปิด)
- `admission` ปิด lint/typecheck (`lint-command: ""`, `typecheck-command: ""`)
- ไม่มี job ใด deploy (comment ใน `check.yml`: "CI never deploys")

## Open questions

- ไม่มี branch protection / required checks ให้ดูใน repo
