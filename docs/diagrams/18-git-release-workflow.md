# 18 — Git & release workflow

สิ่งที่เห็นจาก `git log --first-parent origin/main` และ `gh pr list`: ส่วนใหญ่ push ตรงเข้า `main`; PR ใช้ 4 ครั้ง (#2–#5) merge ภายใน 1–3 นาทีหลังเปิด (#2–#4 squash, #5 merge commit); #1 (codex/shared-infrastructure) ถูกปิดแล้ว merge แบบ local เป็น `754dc85`

```mermaid
gitGraph
  commit id: "e3edc4f scaffold"
  branch claude/untitled-session
  commit id: "97679c1"
  checkout main
  merge claude/untitled-session id: "4aa077b"
  commit id: "f768891 step 0"
  commit id: "264c136 shadow service"
  commit id: "1bb9885 PR 2 squash"
  commit id: "7e553f1 PR 3 squash"
  commit id: "41d420b PR 4 squash"
  commit id: "05622c0 shared CI"
  branch codex/shared-infrastructure
  commit id: "baa4826"
  commit id: "16ba63e"
  checkout main
  merge codex/shared-infrastructure id: "754dc85 PR 1 local merge"
  commit id: "17b3bbc"
  branch service-client-credentials
  commit id: "434d7f2"
  commit id: "9888b7c"
  checkout main
  merge service-client-credentials id: "fb52d6e PR 5"
  commit id: "a9bbfae"
  commit id: "6ad4bb9"
  commit id: "9bf770c"
```

Sources: `git log --first-parent --format='%h %ad %s' origin/main`, `gh pr list -R bookchaowalit-backend/platform-identity --state all`

```mermaid
flowchart LR
  A["Change on branch or direct to main"] --> B["Check workflow on push or PR"]
  B -->|green| C["main"]
  C --> D["Operator: git archive main, railway up"]
  D --> E["Verify /health, /readyz, JWKS, discovery"]
  E --> F["Record in docs/PLATFORM-SERVICES-CUTOVER.md"]
  V["Variable change: railway variables --skip-deploys"] --> D
```

Sources: `.github/workflows/check.yml`, `docs/PLATFORM-SERVICES-CUTOVER.md`, `PRODUCT.md`

หมายเหตุ: ไม่มี tag หรือ GitHub release; version ใน `pyproject.toml` คงที่ `0.1.0`; ไม่มีอะไรบังคับว่า deploy ต้องมาจาก commit ที่ CI เขียว (deploy เป็นมือ)

## Open questions

- ต้องการ CD (ติดตั้ง Railway GitHub app ใน org `bookchaowalit-backend` หรือ deploy job) หรือคง deploy มือ
