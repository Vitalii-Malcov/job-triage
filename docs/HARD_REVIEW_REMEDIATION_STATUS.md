# Hard Code Review remediation — status log

Source: `docs/job-triage — Hard Code Review (main @ 78a947b).md` (Artur Savitskiy, 2026-11-03).
Base: `main` @ `78a947b6e4df312d7bf3f5cac7069141d8d90762`. Integration branch: `fix/hard-review-integration` (local only; no push, no merge to `main`).

Environment facts (2026-11-04/05): Python 3.13, pytest 9.2, ruff 0.17.3, alembic 1.20.1, Codex CLI 0.166.0
(`codex exec -s read-only`). Throwaway PostgreSQL 16 container `jt-test-pg` on `localhost:55432`
(test-only, user/db `scheduler_test`). Codex's read-only sandbox cannot run pytest; test runs are done by
Claude and reported to Codex. `runtime/job_triage_runtime.db` is never opened; `tests/conftest.py` forces a temp SQLite DB with `DATABASE_ROLE=test`.

| Task | Finding | State |
| --- | --- | --- |
| 01 | 1.1 API key compare / failed-auth rate limit | IN_PROGRESS |
| 02–19 | see task list | PENDING |

## TASK 01 — API authentication (1.1, HIGH)

- Confirmed: yes. `x_api_key != expected` and 401 before the limiter on every route.
- Process note: implementation preceded the Codex *plan* review (GATE B); plan and code were reviewed together
  in the first Codex round. Later tasks run GATE B first.
- Changes: `app/security/auth.py` (constant-time `hmac.compare_digest` on UTF-8 bytes; shared failed-auth budget),
  `app/security/rate_limit.py` (atomic `reserve`/`refund`, 10 failures / 300 s per client identity, IPv6 per /64,
  4096-identity cap that never evicts locked clients, throttled sweep), `app/ui/auth.py` (presented UI cookies are
  charged — forged-cookie key-guess oracle), `app/ui/routes.py` (login charged after body read).
- Behaviour: a locked-out client gets 429 on every route even with the right key; successes are refunded.
  Accepted trade-offs: operator behind a shared NAT/proxy identity is locked out with an attacker for ≤5 min;
  if 4096 networks are simultaneously locked, new clients get 429 (fail closed).
- Tests: `tests/test_auth_rate_limit_ordering.py` rewritten (previously documented the gap), `tests/test_security.py`,
  `tests/conftest.py` (autouse budget reset). Targeted: 163 passed (auth/security/UI).
- Full pytest, pre-fix-round-1 code: 7282 passed, 139 skipped, 0 failed (55m50s). Post-fix rerun: see below.
- Ruff check / format: clean.
- Codex: 5 invocations, 4 review rounds. R1 CHANGES_REQUESTED (UI cookie oracle, non-atomic, unbounded state),
  R2 CHANGES_REQUESTED (cap eviction reset lockouts, refund wrong entry), R3 CHANGES_REQUESTED (cap-exhaustion DoS →
  documented trade-off + /64), R4 `BLOCKED` solely because Codex cannot run pytest in its sandbox (no code findings),
  R5 `VERDICT: ACCEPT` after being told pytest is run by the implementer.
- PostgreSQL / migrations: not applicable (no schema change).
