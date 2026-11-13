# SPEC-006 checker reviews

Checker: fresh Claude reviewer subagent (solo-reviewer), no maker context, same reviewer across rounds.

| Round | Spec version | Verdict | Findings | Addressed in |
|---|---|---|---|---|
| R1 | 0.2 | revise | 3 P1 (scope not proof; SECURITY DEFINER under forced RLS; installation semantics), 6 P2, 2 P3 | 0.3 |
| R2 | 0.3 | revise | 2 P1 (initiator never reaches the PDP; migrator cannot create a BYPASSRLS role), 2 P2, 2 P3 | 0.4 |
| R3 | 0.4 | **pass** | 3 P2 (suspended-owner exchange vs AC-15; install escalation; ALTER FUNCTION OWNER needs role membership), 1 P3 | 0.5 |

Full findings are in the maker session's transcript (2026-11-12); each fix is listed in the spec change log.
