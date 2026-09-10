# Refactoring Backlog — JobTriage

Read-only analysis. **Nothing in this document has been implemented.** It records candidates, evidence, and suggested direction for later, individually-reviewed work (explicitly reserved for independent Codex review per project instruction). Source data: AST-based line-count scan + manual reading of each candidate, at HEAD `f88b84a`.

---

## Part 1 — Large-function map

### Top functions by LOC (AST scan, top 15)

| LOC | Location | Function | Responsibility |
|---|---|---|---|
| 259 | `app/services/collector_runner.py:433-691` | `run_xing` | Fetch + score + persist one XING mailbox collector run; shared by route, Telegram `/run xing`, and automation orchestrator; manages scan-watermark progress and mailbox scoping. |
| 213 | `app/services/automation_shortlist.py:159-371` | `prepare_shortlist_drafts` | Stage 8C automation step: preselects candidate job IDs, matches + revalidates them, produces shortlist draft items with per-job failure isolation. |
| 206 | `app/services/collector_runner.py:225-430` | `run_bundesagentur` | Fetch + score + persist one Bundesagentur collector run; same sharing pattern as `run_xing`; handles `touched_jobs` bookkeeping and lease-loss checks. |
| 205 | `app/services/follow_up_send.py:669-873` | `send_follow_up` | Sends an approved follow-up under a thread lock with lease-renewal heartbeat; raises one of 8 distinct domain exceptions. |
| 204 | `app/services/automation_gmail.py:239-442` | `prepare_gmail_response_drafts` | Automation step preparing response drafts from newly-synced Gmail messages. |
| 188 | `app/db/candidate_profile_repository.py:283-470` | `apply_candidate_profile_patch` | Applies a partial-update patch to a candidate profile row with versioning/consistency checks. |
| 168 | `app/providers/email/smtp.py:201-368` | `send` | Low-level SMTP send implementation (the real `smtplib` call). |
| 166 | `app/services/automation_follow_up.py:68-233` | `prepare_follow_up_proposals` | Automation step generating follow-up proposals from matched threads. |
| 157 | `app/services/scheduler.py:227-383` | `run_due_digest_if_claimed` | Claims and runs the daily Telegram digest tick if due. |
| 154 | `app/services/follow_up_eligibility.py:66-219` | `evaluate_follow_up_eligibility` | Determines whether a job/thread is eligible for a follow-up proposal. |
| 153 | `app/providers/email/imap.py:416-568` | `_fetch_sync_body` | Core IMAP fetch/parse loop for Gmail inbox sync. |
| 146 | `app/services/automation.py:509-654` | `run_automation_cycle` | Top-level automation orchestrator composing collector/Gmail/shortlist/follow-up steps. |
| 140 | `app/providers/email/imap.py:692-831` | `_fetch_one` | Fetches/parses a single IMAP message. |
| 133 | `app/collectors/xing_email.py:512-644` | `_fetch_sync_body` | Core IMAP fetch loop for the XING mailbox digest collector. |
| 130 | `app/services/company_research.py:183-312` | `get_or_run` | Company research cache/TTL check + provider invocation + failure-isolated persistence. |

### Individual candidate analysis — the four explicitly named in scope

#### `run_xing` — `app/services/collector_runner.py:433-691` (259 LOC)

- **Actual number of responsibilities:** ~5 — (1) config/credential validation, (2) IMAP fetch orchestration via the collector, (3) per-message scoring, (4) DB persistence (upsert + watermark advance), (5) result-summary assembly for 3 different callers (route, Telegram, automation).
- **Nesting/branch complexity:** moderate-high — several nested try/except around per-message failure isolation (one bad message must not abort the whole run).
- **Transaction boundaries:** per-message upsert, watermark advanced only after successful persistence — already deliberately incremental, not one big transaction.
- **Side effects:** IMAP read, DB writes (jobs, scan watermark), structured log lines.
- **External calls:** IMAP (via `app/collectors/xing_email.py`).
- **Security implications:** owns the trust boundary between raw XING email content and persisted `JobRecord` fields — any extraction change here must preserve the existing trust-marking behavior.
- **Decomposition benefit:** real — the failure-isolation loop, the scoring step, and the result-summary assembly are each independently nameable and testable (`_fetch_xing_messages`, `_score_and_persist_message`, `_summarize_xing_run`), unlike a mechanical `step1`/`step2` split.
- **Risk of refactoring:** medium — this function is on the hot path for 3 different callers (route, Telegram command, automation cycle) and touches the XING trust boundary; a careless extraction could accidentally change what's logged or how a partial failure is reported.
- **Suggested priority:** P2 (maintainability), not urgent.
- **Classification: REQUIRES CODEX** — security-adjacent (trust boundary) + multi-caller shared logic; too risky for a mechanical extraction without independent review.

#### `run_bundesagentur` — `app/services/collector_runner.py:225-430` (206 LOC)

- **Actual number of responsibilities:** ~4 — config validation, paginated API fetch, per-job scoring/persistence, result-summary assembly.
- **Nesting/branch complexity:** moderate — retry/backoff handling for the paginated fetch (per known Bundesagentur collector tech debt, see `docs/TECHNICAL_DEBT.md`).
- **Transaction boundaries / side effects:** same incremental-persistence shape as `run_xing`.
- **External calls:** Bundesagentur REST API (structured, trusted source).
- **Security implications:** lower than `run_xing` — Bundesagentur is the one `TRUSTED_JOB_SOURCES` entry, so less trust-boundary risk, but still shared across 3 callers.
- **Decomposition benefit:** real, same shape as `run_xing` (fetch / score-and-persist / summarize).
- **Risk of refactoring:** medium — same multi-caller sharing risk as `run_xing`, plus the already-known pagination/retry tech debt (see project `CLAUDE.md` "Known tech debt — Bundesagentur collector") that should not be accidentally touched by an unrelated extraction.
- **Suggested priority:** P2.
- **Classification: REQUIRES CODEX.**

#### `prepare_shortlist_drafts` — `app/services/automation_shortlist.py:159-371` (213 LOC)

- **Actual number of responsibilities:** ~4 — candidate preselection (score-DESC/id-ASC bounded), match computation, per-job draft preparation with failure isolation, result aggregation.
- **Nesting/branch complexity:** moderate — bounded-preselection logic plus a per-job try/except loop.
- **Transaction boundaries:** per-job draft persistence, isolated so one job's failure doesn't abort the shortlist.
- **Security implications:** none beyond the existing job-trust rules it already delegates to.
- **Decomposition benefit:** real — preselection and per-job draft preparation are cleanly separable.
- **Risk of refactoring:** medium — the exact preselection ordering (`JobRecord.score-DESC/id-ASC`) and the "exact set this run's own collectors persisted" in-memory attribution are load-bearing invariants (explicitly documented in `.env.example`) that an extraction must not perturb.
- **Suggested priority:** P2.
- **Classification: REQUIRES CODEX** — touches automation invariants explicitly called out as load-bearing.

#### `send_follow_up` — `app/services/follow_up_send.py:669-873` (205 LOC)

- **Actual number of responsibilities:** ~4 — approval/precondition validation (8 distinct domain exceptions), thread-lock acquisition with lease-renewal heartbeat, the actual SMTP send call, CAS-based send-record persistence.
- **Nesting/branch complexity:** high relative to its peers — 8 distinct failure modes each need a precise, distinguishable exception.
- **Transaction boundaries:** send-record CAS claim before the real send, terminal `UNCERTAIN` state on ambiguous provider outcome — the most safety-critical function in this list.
- **Side effects:** real outbound email (the only side effect in the whole codebase that cannot be undone).
- **Security implications:** highest of any candidate here — it is the literal human-approval enforcement point ("no approval = no send").
- **Decomposition benefit:** real in principle (precondition-validation vs. send-execution are separable), but the current single-function shape makes the full approval chain auditable in one place, which has its own value.
- **Risk of refactoring:** **high** — any extraction mistake here risks the project's most important safety invariant (never sending without approval, never double-sending).
- **Suggested priority:** P3 — leave alone unless there is a concrete, reviewed reason to change it.
- **Classification: REQUIRES CODEX, and likely REQUIRES ASTRA** given it is a send-safety-critical function; do not decompose without a dedicated, focused review round (mirroring how FINAL-001/003/004 were handled).

### Other notable candidates (not deep-dived, recorded for completeness)

| Function | LOC | Classification | Note |
|---|---|---|---|
| `prepare_gmail_response_drafts` (`app/services/automation_gmail.py`) | 204 | REQUIRES CODEX | Shares the same automation-invariant risk profile as `prepare_shortlist_drafts`. |
| `apply_candidate_profile_patch` (`app/db/candidate_profile_repository.py`) | 188 | REQUIRES CODEX | Versioning/CAS logic — repository-layer concurrency control, high risk to touch casually. |
| `send` (`app/providers/email/smtp.py`) | 168 | LEAVE ALONE | The actual outbound SMTP call; same safety-critical class as `send_follow_up`. |
| `prepare_follow_up_proposals` (`app/services/automation_follow_up.py`) | 166 | REQUIRES CODEX | Automation invariant risk. |
| `run_due_digest_if_claimed` (`app/services/scheduler.py`) | 157 | SAFE LATER | Digest-claim CAS logic is well-isolated; lower blast radius than send paths. |
| `evaluate_follow_up_eligibility` (`app/services/follow_up_eligibility.py`) | 154 | SAFE LATER | Pure evaluation logic, no side effects — good decomposition candidate if ever prioritized. |
| `_fetch_sync_body` (`app/providers/email/imap.py`, `app/collectors/xing_email.py`) | 153 / 133 | LEAVE ALONE | Tightly coupled to the deadline/socket-lifecycle mechanism documented in `imap_deadline.py` — do not touch without the same rigor applied to FINAL-003. |
| `run_automation_cycle` (`app/services/automation.py`) | 146 | REQUIRES CODEX | Top-level orchestrator — the single riskiest "God function" to decompose incorrectly. |
| `company_research.get_or_run` | 130 | SAFE LATER | Cache/TTL + provider-call pattern, lower risk. |

**General guidance for any future decomposition:** extract named responsibilities (`validate_preconditions()`, `claim_send_lock()`, `summarize_run()`) — never generic `step1()`/`helper_a()` fragments. Sequential, readable code that is already easy to follow top-to-bottom should not be fragmented just to reduce line count.

---

## Part 2 — `app/api/routes.py` analysis

### Current state

- **Total lines:** 1860 (single file, no sub-router split).
- **Route-decorated functions:** 50.
- **`raise HTTPException(...)` call sites:** 118.
- **`except SomeError:` blocks translating a domain exception to `HTTPException`:** 85, covering ~39 distinct exception type names.
- **Centralized exception handling:** none — no `@app.exception_handler` anywhere in `app/`. Every route does its own manual try/except → `HTTPException`.

### Duplication found

At least **12 of the 85 except blocks (≈14%)** are byte-identical duplicates of 4 patterns:

| Exception type | Status | Detail | Repeated at (line numbers) |
|---|---|---|---|
| `ReviewNotFoundError` | 404 | `str(exc)` | 894-895, 940-941, 994-995 |
| `CollectorNotConfiguredError` | 503 | `str(exc)` | 1051-1054, 1074-1077, 1176-1179 |
| `ResponseDraftNotFoundError` | 404 | fixed `"Response draft not found"` | 1497-1500, 1555-1558, 1633-1636 |
| `FollowUpProposalNotFoundError` | 404 | fixed `"Follow-up proposal not found"` | 1744-1747, 1790-1793, 1857-1860 |

A further set of 2×-repeated types (`AmbiguousCompanyIdentityError`, `ReviewCurrentProfileMissingError`, `ReviewProfileChangedError`, `ReviewCurrentJobMissingError`, `ReviewJobChangedError`, `CollectorError`, `ResponseDraftNotApprovableError`) likely follow the same shape but were not individually diffed in this pass.

The remaining ~25 exception types occur exactly once each (e.g. `BewerbungCVDraftNotFoundError`, `ReviewParagraphIndexError`, `AutomationRunAlreadyInProgressError`) — genuinely route-specific, though many mirror each other structurally by design (`Review*Error` / `Bewerbung*Error` / `CVDraft*Error` families follow the same in-code "Stage" convention).

### Status/detail semantics that genuinely differ

Not all exceptions of a "similar shape" map identically — e.g. `AutomationRunAlreadyInProgressError` maps to 409 (conflict, not 404/503), and some `*NotFoundError` variants carry dynamic `str(exc)` detail text while others use a fixed string. Any centralization must preserve this per-type variance exactly, not flatten it to one generic shape.

### Could FastAPI exception handlers reduce duplication safely?

Yes, in principle: a `@app.exception_handler(ReviewNotFoundError)`-style registration (or a single handler keyed by a small `type(exc) -> (status, detail_fn)` lookup table) could replace the ≥12 confirmed duplicate blocks without changing observable behavior, **provided**:

1. The exact status code and detail string/format is preserved per exception type (including the dynamic-vs-fixed-string distinction above).
2. Route-local context currently embedded in some `except` blocks (e.g. blocks that do additional cleanup or logging before raising) is identified and kept route-local rather than centralized.
3. Test coverage for each route's error path (already exists per this project's "tests for every behavior change" rule) is used to prove byte-identical response bodies before/after.

### What could break if centralized

- Any except block that does more than "translate and raise" (e.g. logs additional context, or has a subtly different message than its apparent duplicates) would silently lose that behavior if merged into a generic handler without individual inspection.
- FastAPI exception handlers run outside the route function's local variable scope — any except block that currently closes over route-local state (path params, query params) for its message needs that state threaded through explicitly (e.g. via exception attributes set at raise time in the service layer, not reconstructed in the handler).
- Ordering/precedence: FastAPI resolves exception handlers by exact type match with subclass fallback — if any of the ~39 exception types are subclasses of one another in ways not currently obvious from routes.py alone, a shared handler could change which handler fires for a given exception.

### What should remain route-local

- Request-shape validation errors and any except block with route-specific side behavior (logging extra fields, conditional status codes based on request state) should stay in the route function even after centralizing the pure "type → status/detail" mappings.

### Proposed staged migration strategy (not implemented — for future reviewed work)

1. **Stage 1 (near-zero risk):** build a `_EXCEPTION_STATUS_MAP: dict[type[Exception], tuple[int, str | Callable[[Exception], str]]]` module-level table in `routes.py` covering only the ≥12 confirmed byte-identical duplicates, plus a tiny `_translate(exc) -> HTTPException` helper each of those 12 call sites uses. This changes nothing observable — it's a local deduplication, not a FastAPI-level architectural change. Requires per-route diff review, not a mechanical sed.
2. **Stage 2:** extend the same table to the ~7 likely-duplicated-but-unverified 2×-repeated types, after individually diffing each pair to confirm byte-identical behavior first.
3. **Stage 3 (higher risk, only after Stage 1-2 prove the pattern out):** consider promoting the table to real `@app.exception_handler` registrations in `app/main.py`, which removes the try/except boilerplate from routes.py entirely for the covered types — but only after confirming (via the existing route-level error-path tests) that FastAPI's handler dispatch produces byte-identical response status/body/headers for every covered case, including content-type and any response-model validation interactions.
4. At every stage: preserve status codes, detail text (including the fixed-vs-dynamic distinction), response body shape, and security behavior (e.g. a 401 must still short-circuit before any handler logic that could leak information). No stage should be a single giant commit — one exception-type-family per commit, each independently reviewable and revertable.

This entire section is **explicitly reserved for later, independent Codex review** per project instruction — not implemented in this branch.
