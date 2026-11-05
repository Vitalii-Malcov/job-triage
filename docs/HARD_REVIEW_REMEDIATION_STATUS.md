# Hard Code Review remediation — status log

Source: `docs/job-triage — Hard Code Review (main @ 78a947b).md` (Artur Savitskiy, 2026-11-03).
Base: `main` @ `78a947b6e4df312d7bf3f5cac7069141d8d90762`. Integration branch: `fix/hard-review-integration` (local only; no push, no merge to `main`).

Environment facts (2026-11-04/05): Python 3.13, pytest 9.2, ruff 0.17.3, alembic 1.20.1, Codex CLI 0.166.0
(`codex exec -s read-only`). Throwaway PostgreSQL 16 container `jt-test-pg` on `localhost:55432`
(test-only, user/db `scheduler_test`). Codex's read-only sandbox cannot run pytest; test runs are done by
Claude and reported to Codex. `runtime/job_triage_runtime.db` is never opened; `tests/conftest.py` forces a temp SQLite DB with `DATABASE_ROLE=test`.

| Task | Finding | State |
| --- | --- | --- |
| 01 | 1.1 API key compare / failed-auth rate limit | ACCEPTED — task 5c4646d, merge bab160d |
| 02 | 1.2 secrets in repr / UI signing key | ACCEPTED — task d6f1494, merge 73f6bba |
| 03 | 1.3 response-draft recipient pinning | ACCEPTED — task 32336e3, merge 57d58a5 |
| 04 | 1.4 UI session revocation | ACCEPTED — task b4ec414, merge 10c69ad |
| 05 | 2.1 processed email identity | ACCEPTED (see below; SHAs in git log) |
| 06–19 | see task list | PENDING |

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
- Full pytest, pre-fix-round-1 code: 7282 passed, 139 skipped, 0 failed (55m50s). Post-fix rerun: 7290 passed, 139 skipped, 0 failed (37m27s).
- Ruff check / format: clean.
- Codex: 5 invocations, 4 review rounds. R1 CHANGES_REQUESTED (UI cookie oracle, non-atomic, unbounded state),
  R2 CHANGES_REQUESTED (cap eviction reset lockouts, refund wrong entry), R3 CHANGES_REQUESTED (cap-exhaustion DoS →
  documented trade-off + /64), R4 `BLOCKED` solely because Codex cannot run pytest in its sandbox (no code findings),
  R5 `VERDICT: ACCEPT` after being told pytest is run by the implementer.
- PostgreSQL / migrations: not applicable (no schema change).

## TASK 02 — Secrets and configuration (1.2, HIGH)

- Confirmed: yes (`api_key`, `telegram_bot_token`, `bundesagentur_api_key` were plain `str`, shown by `repr(Settings)`).
- GATE B: Codex plan review done first (PLAN_CHANGES: use SecretStr, bind signing key to API key for fail-closed + rotation revocation, test semantics) — plan revised accordingly.
- Changes: the three fields + new optional `ui_session_secret` are `SecretStr`; `reveal()` helper (`app/core/config.py`) unwraps at all
  consumers in `app/` and `scripts/`; `app/ui/auth.py::signing_key` = HMAC-SHA256(`UI_SESSION_SECRET` or API key, context + current API key).
  Raw API key is no longer the cookie key; existing cookies are invalidated once (re-login). `.env.example` documents `UI_SESSION_SECRET`.
  No real keys changed.
- Honest limits: a stolen API key still logs in through the login form; without `UI_SESSION_SECRET` the derived key adds domain separation only.
- Tests: `tests/test_config.py` (masking, generic scan, errors), `tests/test_stage20_ui.py` (modes/rotation/fail-closed/roundtrip),
  two assertions in `tests/test_stage20_run_operator_ui.py` unwrap SecretStr.
- Full pytest: 7295 passed, 1 failed (assertion on SecretStr; fixed, file 11 passed), 139 skipped. Ruff check/format clean. No schema change.
- Codex: plan review + 2 code rounds. R1 CHANGES_REQUESTED (second stale assertion that I had missed), R2 ACCEPT.

## TASK 03 — Response-draft recipient pinning (1.3, MEDIUM)

- Confirmed: yes (recipient read from `GmailMessageRecord.from_address` at send time; approval pinned only subject/body).
- GATE B: Codex plan review first (PLAN_CHANGES: strict single-mailbox parsing, fresh read, SENT/UNCERTAIN precedence, preview/expected_recipient, migration round trip, missing 422 handler) — all adopted.
- Schema: `response_draft_approvals.pinned_recipient` (migration `f1a8c3d5e7b9`, head bumped in tests + `ci.yml`). Existing approvals keep `''` and are never sendable; nothing is back-filled.
- Behaviour: APPROVED pins the validated recipient (`app/services/response_draft_recipient.py`); optional `expected_recipient` guard (409); send uses only the pinned value after a fresh re-derivation matches (409 otherwise, nothing claimed); already SENT/UNCERTAIN/attempted-PENDING keep their outcome before the recipient check; `reply_recipient` preview on `/state` and `/pending`.
- Residual risk: the stored `From` is still unauthenticated (no SPF/DKIM) — pinning fixes what the operator authorized, not who the sender is. Legacy approved-but-unsent drafts are blocked until a genuinely new revision exists (regeneration with unchanged identity returns the same revision).
- Tests: 9 new endpoint tests, service tests rewritten, populated SQLite migration round trip. PostgreSQL (throwaway `postgres:16`, localhost:55432): alembic upgrade/downgrade/upgrade OK, 11 PG integration tests passed.
- Full pytest: 7312 passed, 139 skipped, 0 failed. Ruff clean.
- Codex: plan review + 2 code rounds (R1 CHANGES_REQUESTED: stale existing test, weak stale-session test; R2 ACCEPT).

## TASK 04 — UI session revocation (1.4, LOW)

- Confirmed: yes (stateless cookie; logout only cleared the browser copy).
- GATE B: Codex plan review first (PLAN_CHANGES; option A required: in-memory bounded registry, no DB write because the Stage 20 UI is read-only).
- Changes: token `expiry.sid.hmac` (random 128-bit session id per login); signing key bound to a random per-process nonce (restart invalidates all cookies); `SessionRevocations` (thread-safe, cap 10 000, never evicts unexpired entries, full => logout answers 503 and the cookie is kept); logout only records correctly signed, unexpired cookies (identical 303 otherwise); signed-but-revoked cookies are refunded from the TASK 01 guess budget and then denied; launchers pinned to `--workers 1` (`scripts/run_operator_ui.py`, `scripts/stage14d_canonical_runtime.py`, `docs/DAILY_OPERATIONS.md`).
- Residual: per-process state, so one web process/replica is required (documented in `docs/STAGE20_UI_ARCHITECTURE.md`); "log out everywhere" = rotate `UI_SESSION_SECRET`/`API_KEY` and restart. Every restart logs the browser out.
- Tests: 11 new in `tests/test_stage20_ui.py` (replay after logout, second session, idempotence, forged logout, CSRF, budget, capacity, expiry recovery, threads, restart) + launcher tests.
- Full pytest: 7324 passed, 139 skipped, 0 failed. Ruff clean. No schema change.
- Codex: plan review + 3 code rounds (R1 CHANGES_REQUESTED launcher workers, R2 CHANGES_REQUESTED docs command, R3 ACCEPT).

## TASK 05 — Processed email identity (2.1, MEDIUM latent)

- Confirmed: yes (global unique `message_id` vs `(source, message_id)` lookups; check-then-insert).
- GATE B: Codex plan review first (PLAN_CHANGES: no generic fallback, SQLite/PostgreSQL `ON CONFLICT` only, downgrade refuses before DDL, PG cleanup + CI list, stronger concurrency evidence) — adopted.
- Schema: migration `a2b7d4f9c1e3` — drops the unique index, keeps a non-unique `message_id` index, adds `uq_processed_email_messages_source_message_id`. Downgrade refuses (nothing changed) while two sources share a Message-ID.
- Code: `mark_message_processed` is one `INSERT .. ON CONFLICT (source, message_id) DO NOTHING` + commit (no commit when the marker exists; the commit also persists other pending session work — documented contract).
- Tests: `tests/test_processed_email_identity.py` (6, incl. 8-thread SQLite and populated migration round trip), `tests/integration/test_processed_email_identity_postgres.py` (3, incl. forced overlap with an uncommitted competing insert; added to the ci.yml zero-skip list), head literals bumped.
- PostgreSQL (throwaway postgres:16): upgrade, populated downgrade refusal, downgrade/re-upgrade with correct indexes/constraints, 3 PG tests passed.
- Full pytest: 7330 passed, 142 skipped (3 new PG tests skip without `TEST_POSTGRES_URL`), 0 failed. Ruff clean.
- Codex: plan review + 3 code rounds (R1 CHANGES_REQUESTED holder not released on failure; R2 `BLOCKED` only for lack of full-suite evidence; R3 ACCEPT with evidence supplied).
