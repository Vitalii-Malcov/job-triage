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
| 05 | 2.1 processed email identity | ACCEPTED — task feff194, merge 1cf7900 |
| 06 | 2.2 XING dedup collisions | ACCEPTED — task 22d230a, merge 818b319 |
| 07 | 2.3 collector concurrency | ACCEPTED — task d74a1de, merge 349108d |
| 08 | 2.4 event-loop blocking | ACCEPTED — task 5e7aa2d, merge c54164d |
| 09 | Arch §3 repository transactions | ACCEPTED — task 6e4c0a7, merge 9604cb6 |
| 10 | Arch §3 large modules | ACCEPTED (see below; SHAs in git log) |
| 11–19 | see task list | PENDING |

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

## TASK 06 — XING dedup collisions (2.2, MEDIUM)

- Confirmed: yes (key = company/title/location; the existing-row update overwrote score/skills/description, also for operator-touched rows).
- Constraint analysis: the digest e-mail has no stable reference id (tracking links are per recipient and must never be requested) and the fingerprint cannot change without duplicating every historical production row, so the audit's alternative "flag likely collisions" was implemented. A first design (derived second fingerprint that splits postings) was rejected after Codex plan review (races, Telegram identity, protected blank base).
- GATE B: Codex plan review, two rounds (PLAN_CHANGES x2 -> flag-and-freeze design with row lock, persisted-values result, alert suppression).
- Behaviour (XING only, `app/db/repositories.py::_update_existing`): the row is reloaded `FOR UPDATE`; when stored and incoming descriptions both have content and differ (NFKC/casefold/whitespace) OR the legacy `JobRecord.status != "NEW"`, only `last_seen_at` is refreshed. `score_and_persist` then reports the persisted assessment (no posting-type reconciliation), the legacy Telegram alert is skipped for flagged observations, `run_xing` returns `possible_collisions` and Telegram `/run` shows it when non-zero. `upsert_job` keeps its tuple signature; `upsert_job_observed` returns `UpsertOutcome`. No schema change, no historical row touched.
- Residual: two postings with identical company/title/location AND identical salary/employment text are still indistinguishable; the second posting of a flagged pair stays invisible (only log/summary). A genuine salary change freezes instead of updating. Flags are per observation, not stored. Freeze scope is `JobRecord.status` only (Telegram Save/Skip and Stage 15 do not change it). Operator-status race window is bounded (not a conditional UPDATE).
- Tests: `tests/test_xing_collision_protection.py`, endpoint test (count + no alert), Telegram `/run` test, PG tests (`test_upsert_job_postgres_concurrency.py`: existing blank row + 2 competing descriptions; new-row race) — 9 passed on postgres:16.
- Full pytest: 7350 passed, 144 skipped, 0 failed. Ruff clean.
- Codex: plan review x2 + 1 code round: `VERDICT: ACCEPT` (P3: show collisions in Telegram `/run` — implemented after the verdict, covered by a new test and the full run).

## TASK 07 — Collector concurrency (2.3, MEDIUM)

- Confirmed: yes (`POST /collectors/*/run` and Telegram `/run` called `run_bundesagentur`/`run_xing` with no lock; the lease protected automation cycles only).
- GATE B: Codex plan review (PLAN_CHANGES: account-key identity, lease-loss scope, cancellation vs the IMAP worker thread, cleanup contract, history rendering, 409 presentation) — adopted.
- Design (no schema change): `app/services/collector_exclusion.py::run_collector_exclusively` — manual runs claim the SAME per-account RUNNING slot as automation, for every distinct ACTUAL key of {normalized `GMAIL_USERNAME` (kept even when empty), `AUTOMATION_SCHEDULER_ACCOUNT_KEY` as configured}, sorted; config preflight before any slot; heartbeat per run; heartbeats stopped and verified before success is recorded (lost/stuck heartbeat => FAILED + `CollectorRunLeaseLostError`); runner exception => sanitized FAILED; cancellation => no finalization, lease extended past the IMAP deadline on a separate session (the worker thread may still use the request session). REST => 409 (in progress / lease lost), Telegram replies. `run_automation_cycle` unchanged. Manual rows are labelled (`manual:<collector>` results, `manual_collector:` lease holder) and excluded from the Telegram digest in SQL before LIMIT (`list_runs(include_manual=False)`).
- IMAP integrity: once the lease is lost `run_xing` leaves the message unacknowledged and the scan watermark untouched (persisted jobs stay — existing documented idempotent design).
- Covered entry points: REST collector endpoints, Telegram `/run`, scheduler and REST automation. Not covered: operator CLIs calling lower-level helpers (`stage22_refresh_ba` has its own file lock).
- Residual: manual runs now appear in `/automation/runs`; scheduler vs REST automation with different spellings of the key are still separate slots among themselves (manual runs hold both); after a lost lease already-started persistence/notifications of the current job may finish; a cancelled manual run keeps the slot RUNNING for ~6 min (fail closed).
- Tests: `tests/test_collector_exclusion.py` (33), lease-guard ack/watermark tests, digest tests, PG module `tests/integration/test_collector_exclusion_postgres.py` (2 passed on postgres:16; added to the ci.yml zero-skip list).
- Full pytest: 7385 passed, 146 skipped, 0 failed. Ruff clean.
- Codex: plan review + 4 code rounds (R1 CHANGES_REQUESTED x5 incl. P1 scheduler key spelling; R2 CHANGES_REQUESTED x3 incl. empty REST key; R3 `BLOCKED` only for missing full-suite evidence; R4 ACCEPT with evidence).

## TASK 08 — Async event-loop blocking (2.4, MEDIUM)

- Confirmed: yes (`async def` endpoints interleaving awaits with synchronous SQLAlchemy work on the web loop, shared with the Telegram poller).
- GATE B: Codex plan review, 2 rounds (PLAN_CHANGES: `score_job` has an `await`; offload the WHOLE exclusion operation; cancellation must drain the worker before the session is closed; bounded dedicated executor; stronger tests) — adopted.
- Design: `app/services/offload.py::run_off_loop` runs `await factory()` on a private event loop in a dedicated bounded executor (8 threads); a cancelled caller keeps waiting (absorbing repeated cancels) until the worker ends, retrieves a late failure, then re-raises — the request-scoped Session is never closed under a running worker. Applied to the collector exclusion operation (REST + Telegram `/run`), POST `/automation/runs`, `/gmail/sync`, `/jobs/{id}/research`, `/jobs/{id}/bewerbung-draft` and `score_job`. Service code unchanged.
- Not done (documented in `docs/DEPLOYMENT.md`): the Telegram poller stays in the web process (topology change); Telegram commands with their own synchronous DB work (`/research`, `/review`, ...) still run on the poller loop; `--workers 1` remains required.
- Residual: a cancelled/disconnected request waits for its worker (can be long for a collector run); the 8-thread pool queues further long runs.
- Tests: `tests/test_offload.py` (11, incl. ASGI liveness: `/health` < 0.6 s while collector/automation/score/Gmail workers block 1.2 s), exclusion tests switched to thread-safe events.
- Full pytest: 7396 passed, 146 skipped, 0 failed. Ruff clean. No schema change.
- Codex: plan review x2 + 1 code round: `VERDICT: ACCEPT`.

## TASK 09 — Repository transactions (Architecture §3)

- Confirmed (scoped): the audit counts ~15 commits in `app/db/repositories.py`. Most are intentional durable/CAS boundaries (job write + reference tokens in one commit, status CAS, score CAS, processed-message ack before watermark advance); `get_or_create_default_profile` has no application caller (legacy; left as is, documented). Two real multi-step gaps were demonstrated by Codex and reproduced: (1) `score_and_persist` committed the upsert and the posting-type reconciliation separately (a failure in between left `SELBSTAENDIGKEIT / APPLY` durable); (2) the company-research update committed content/version before claiming the identity alias (alias failure left an updated record with no alias).
- GATE B: Codex investigation/scope review (SCOPE_CHANGES: narrow to these two, explicit exclusions) — adopted.
- Changes: keyword-only `commit: bool = True` on `upsert_job_observed`, `update_job_score_if_posting_type_unchanged`, `_finalize_job_write`, `_update_existing` (defaults unchanged); `score_and_persist` = deferred upsert + deferred reconciliation CAS + ONE commit, rollback on any failure (a reconciliation failure now leaves the job unwritten, and its source message unacknowledged for reprocessing); deferred mode requires a session without pending changes (fingerprint-collision recovery still rolls back, discarding only this call's own writes — no savepoints); the research update claims its alias in the same transaction (`INSERT .. ON CONFLICT DO NOTHING`); `_allowed_employment_types` is a pure read (no create-and-commit of the singleton profile in the middle of the unit); the BA enrichment path no longer stages `existing.description` (redundant with the upsert).
- Not changed (documented exclusions): other 22 repositories' commits, whole-run transactions, race-recovery code, isolation level, models, status CAS, message acknowledgement.
- Residual: the row lock taken by the deferred upsert is held across the (short) reconciliation loop; the first-write contract of deferred mode is enforced only for pending ORM state.
- Tests: `tests/test_repository_transactions.py` (15), PG tests (2 new, 11 passed on postgres:16), adjusted fakes/assertions in 4 existing test files.
- Full pytest: 7410 passed, 148 skipped, 1 failed (stale monkeypatch target, fixed; file 18 passed). Ruff clean. No schema change.
- Codex: scope review + 3 code rounds (R1 CHANGES_REQUESTED x2 P1: BA staging vs guard, profile create-commit mid-unit; R2 `BLOCKED` only for missing full-suite evidence; R3 ACCEPT).

## TASK 10 — Large modules (Architecture §3)

- Confirmed: `app/db/models.py` 3,647 lines / 47 mapped classes; `app/api/routes.py` 2,155 lines / one router.
- Change (pure move, no behaviour/ORM/API change): `app/db/models/` package (13 domain modules, largest 604 lines; `__init__` re-exports every public name plus the historical `Base`; importing the package still registers all tables for Alembic) and `app/api/routers/` (12 modules, largest 408 lines; `app/api/routes.py` is a 38-line aggregator that keeps `router` as a flat list of routes and re-exports `send_response_draft_endpoint`). Handler/class bodies are byte-identical; the only text change in handlers is `get_settings()` -> `common.get_settings()` (one patch point: `app.api.routers.common.get_settings`). Logger name `app.api.routes` kept.
- Proof of no behaviour change: a fingerprint of the OLD vs NEW tree is identical — SQLite + PostgreSQL DDL of all 47 tables and trigger hooks, mapper columns/relationships/listeners, the complete OpenAPI document, 66 flattened routes with dependencies/response models/status codes. Codex re-derived it independently (85 model statements and 61 router functions preserved). No schema change, no migration. Only incidental module attributes (`sqlalchemy.DDL`, `datetime.UTC`) are no longer reachable as `app.db.models.X`; nothing imported them.
- **Operational effect (act on it at deploy):** `fit_cache.compute_rules_hash` hashes the import closure of the rule modules by name+content; the closure contained the single `app.db.models` file and now contains the package modules, so the rules-hash value changes. Persisted Stage 21 vacancy fit assessments carrying the old hash become stale-by-design (never shown as current) until the operator re-runs the existing fill script; nothing is deleted. The same applies to any future edit of a closure module.
- Tests: `tests/test_module_structure.py` (10), 26 test files retargeted from `app.api.routes.X` to the owning router module, `tests/test_stage21_vacancy_cache.py` closure expectation updated. Migrations suite (117) green.
- Full pytest: 7420 passed, 148 skipped, 2 failed — one wrong route-count constant of mine (fixed), one wall-clock timing test (`test_follow_up_send_service::TestLeaseRenewalHeartbeat`) that passes in isolation and in 3 reruns (flaky under full-suite load, not touched by this change). Ruff clean.
- Residual: docs/comments still mention `app/db/models.py` / `app/api/routes.py` as historical paths (TASK 18); CLAUDE.md/AGENTS.md mention `app/db/models.py` (not edited here — CLAUDE.md has uncommitted operator changes).
- Codex: 2 code rounds: R1 CHANGES_REQUESTED (stale source-closure test), R2 `VERDICT: ACCEPT`.
