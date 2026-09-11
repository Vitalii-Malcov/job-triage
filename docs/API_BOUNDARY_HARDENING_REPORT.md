# API Boundary Hardening Report — `hardening/api-boundaries-r1`

**Base:** `f9181a194a9a2e2e5a36e779c707a983e1b85c2e`
**Branch:** `hardening/api-boundaries-r1`
**Scope:** public API surface, validation boundaries, state machines, resource
bounds, and operator-facing behavior — surfaces *not* deeply exercised by the
prior `hardening/adversarial-r1` pass (HARD-001..016, AUD-001..020). This
report does not repeat those findings unless new evidence materially changes
them.

Finding IDs in this document use the prefix `BOUND-###`.

---

## Summary table

| ID | Area | Severity | Status | Codex? | Astra? |
|---|---|---|---|---|---|
| BOUND-001 | Pagination `offset` int overflow → uncaught 500 | High | **Fixed** | No | No |
| BOUND-002 | Invalid API key never consumes rate-limit budget | Info (confirmed safe) | Documented | No | No |
| BOUND-003 | No HTTP body size limit on any endpoint | Medium | Documented (leave unbounded) | Yes | No |
| BOUND-004 | List cardinality (skills, patches) — no quadratic blowup found | Info | Documented | No | No |
| BOUND-005 | `jobs.last_seen_at` has no index; `list_jobs` default-order query does a seq scan + sort at 1k rows | Low/Medium | Documented, analysis only | Yes | No |
| BOUND-006 | `expected_review_version`/`edit_note`/`decision_note` unbounded, unlike sibling fields | Low | **Fixed** | No | No |
| BOUND-007 | Stale comment in `response_draft_repository.py` claiming to mirror Gmail-analysis limits (20/100 vs actual 50/200) | Trivial | **Fixed** | No | No |
| BOUND-008 | (subsumed into BOUND-005) | — | — | — | — |
| BOUND-009 | Auth-before-rate-limit ordering (HARD-015 deep dive) — current design (Architecture A) confirmed non-exploitable | Info | Documented, no change | No | No |
| BOUND-010 | DST transitions (spring-forward/fall-back) — daily-digest gate confirmed correct under live-computed transition instants | Info | Documented (upgrades prior downgraded claim to verified) | No | No |
| BOUND-011 | `follow_up_send.py` heartbeat renewal error logged with `exc_info=True` (3rd instance of HARD-002/003 pattern) | Medium (privacy) | **Fixed** | No | No |
| BOUND-011b | `bundesagentur.py` non-JSON response snippet leaked into client-visible `HTTPException` detail | Medium (privacy) | **Fixed** | No | No |
| BOUND-012 | `ResponseDraftNotApprovableError` maps to 422 with two different detail strings depending on route | Low | Documented, deferred | Yes | No |
| BOUND-013 | `automation_follow_up_cycle_enabled=True` without any Gmail sync silently never becomes eligible (no distinguishing status) | Low | Documented, deferred (legitimate config in some setups) | Yes | No |
| BOUND-014 | `automation_scheduler_poll_seconds > automation_scheduler_interval_seconds` produces a confusing effective-cadence mismatch | Low | Documented, deferred | Yes | No |
| BOUND-015 | Malformed-email adversarial corpus (15 cases) — no crashes, no trust escalation | Info | Documented | No | No |
| BOUND-016 | Mutation testing of 5 critical invariants — no weak/tautological tests found | Info | Documented | No | No |

---

## BOUND-001 — Pagination `offset` integer overflow → uncaught 500 (Fixed)

**Priority:** High
**Evidence:** `tests/test_api_boundary_hardening.py::TestPaginationAbuse`

Every `offset: int = Query(default=0, ge=0)` parameter across 7 list
endpoints in `app/api/routes.py` accepted arbitrarily large integers. Passing
`offset=2**63` (or `2**31`) reached SQLAlchemy/SQLite and raised
`OverflowError: Python int too large to convert to SQLite INTEGER`, which
propagated as an **uncaught 500**, not a validated 422.

**Reproduction:** `TestClient` request with `?offset=9223372036854775808` (and
`2**31`) against e.g. `GET /api/v1/jobs` — observed a raw traceback / 500
before the fix.

**Impact:** any authenticated caller (or, combined with BOUND-002, any
network caller regardless of key validity in principle — though offset is
only reachable post-auth here) can trigger an unhandled server exception via
a single crafted query string. Not a security boundary bypass, but a
reliability/observability issue (noisy 500s, potential worker-level stack
traces in logs).

**Fix:** added `MAX_OFFSET = 2**31 - 1` (int32-max convention — comfortably
below both SQLite's signed-64-bit integer affinity limit and Postgres
`bigint`, and far larger than any realistic real page count) and applied
`le=MAX_OFFSET` to all 7 `offset` `Query(...)` declarations via one
`replace_all` edit.

**Regression tests:** `test_offset_negative_rejected`,
`test_offset_at_max_offset_accepted`,
`test_offset_over_max_offset_rejected` (parametrized over `2**31`, `2**63`,
`2**63-1`) in `tests/test_api_boundary_hardening.py`. Verified failing (raw
500/OverflowError) before the fix, passing (422) after.

**Codex:** No — narrow, deterministic, no schema/outbound/approval change,
matches Safe Fix Policy exactly.
**Astra:** No.

---

## BOUND-002 — Invalid API key never consumes rate-limit budget (confirmed safe, documented)

**Priority:** Info
**Evidence:** `tests/test_auth_rate_limit_ordering.py`

This is the HARD-015 deep dive requested in Section 5. Current architecture
is **Architecture A: auth runs before the rate limiter** (`require_api_key`
dependency executes, and on failure raises `401` before the rate-limit
dependency's counter is touched).

Tested all three architectures analytically and confirmed A empirically:

- **A (current, auth-before-limit):** an attacker sending unlimited
  wrong-key requests gets unlimited `401`s and never trips `429` — but also
  never consumes the *legitimate* operator's budget, because the limiter key
  is per-source and only counted on the code path *after* auth succeeds in
  this implementation. Confirmed via
  `test_unlimited_invalid_key_attempts_never_429` (10 wrong-key requests, all
  401) and
  `test_invalid_and_valid_key_attempts_from_same_host_do_not_share_a_budget`
  (wrong-key spam from one host does not erode that same host's valid-key
  budget).
- **B (limit-before-auth):** would mean an attacker CAN lock out the
  legitimate operator by exhausting the shared per-source budget with
  wrong-key spam before the operator's real requests get a turn — strictly
  worse for a single-operator tool where the attacker and operator may share
  a source (e.g. behind the same NAT/proxy).
  **C (separate limiters for auth-failed vs. authed traffic)** would fix
  unlimited-401-spam-as-a-vector for wasting server CPU, but that is a
  distinct, lower-severity concern (CPU cost of running `hmac.compare_digest`
  ~unlimited times) not a lockout vector.

**Decision:** per Section 5's instruction, this is NOT a lockout risk under
current behavior, so no change is implemented. Documented as reference
evidence; Architecture C (separate limiter for unauthenticated traffic) is
left for Codex to weigh if CPU-exhaustion-by-auth-spam is judged worth
addressing — it's a distinct concern from lockout.

**Codex:** No (nothing to review — no code change). **Astra:** No.

---

## BOUND-003 — No HTTP body size limit on any endpoint (documented, leave unbounded)

**Priority:** Medium
**Evidence:** `tests/test_api_boundary_hardening.py::TestBodySizeBehavior`

Tested payloads of 100KB / 1MB / 5MB / 10MB against `POST` endpoints
accepting free-text bodies (job creation notes, candidate-profile patch
fields) via in-process `TestClient` (no real sockets, no real bandwidth
consumed). In every case:

- Pydantic validation completed in well-bounded time (sub-second even at
  10MB in-process).
- The full payload was persisted verbatim to the DB (no silent truncation).
- No duplicate logging of the oversized body was observed (log lines
  reference IDs, not raw body content — consistent with prior HARD-002/003
  findings).

**Classification: LEAVE UNBOUNDED at the application layer.** Per Section 2's
explicit instruction not to add arbitrary bounds merely because a field is
currently unlimited, and because:
- No specific field has an established sibling precedent for a max size at
  this scale (unlike `note`/`decision_note`, which had a clean 2000-char
  sibling precedent — see BOUND-006).
- Request body size is conventionally an **infra-level** concern (reverse
  proxy / ASGI server `client_max_body_size` equivalent, e.g. uvicorn/nginx
  config) rather than a per-field Pydantic bound, and this project has no
  existing per-field precedent at the multi-KB/MB scale to extend.

**Recommendation for Codex:** confirm whether the production deployment
(reverse proxy / uvicorn config) has an infra-level body size cap. If not,
that is the correct place to add one — not a new per-endpoint Pydantic
`max_length`.

**Codex:** Yes (infra-level decision, outside this wave's safe-fix scope).
**Astra:** No.

---

## BOUND-004 — List cardinality stress (skills, patches) — no quadratic blowup

**Priority:** Info
**Evidence:** `tests/test_api_boundary_hardening.py::TestListCardinality`

Tested 100/1000/5000-item lists (skills arrays, candidate-profile patch
skills) via `TestClient`. Response time scaled linearly with input size (no
quadratic behavior observed), and no unbounded DB amplification (each
patch/create is a single bounded write, not N separate inserts triggered by
list length). No fix needed; documented as evidence the boundary is safe at
these cardinalities.

**Codex:** No. **Astra:** No.

---

## BOUND-005 — `jobs.last_seen_at` has no index (analysis only)

**Priority:** Low/Medium
**Evidence:** disposable local PostgreSQL 16 container, 1000-row `jobs`
table, `EXPLAIN ANALYZE` on `list_jobs(limit=50, offset=0)` (default-order
listing).

`JobRecord` (`app/db/models.py`) indexes `fingerprint` (unique) and `status`
but **not** `last_seen_at`, which is the default list-ordering column. At
1000 rows this produces a `Seq Scan on jobs` followed by a top-N
heapsort/quicksort `Sort` node, completing in 0.65–1.3ms — not currently a
practical problem at this scale, but the access pattern (seq scan + sort on
every default-order page load) will degrade linearly as the table grows,
and the retention projection (`docs/DATA_RETENTION_CAPACITY.md`) shows
`jobs` is one of the tables that keeps growing indefinitely (no natural
prune point) under normal usage.

Other capacity-simulation queries were confirmed **safe**, for contrast:
- `list_unprocessed_messages_for_automation` (5000 messages): uses
  `ix_gmail_messages_account_key_automation_processed_at`, ~1.2–1.7ms.
- `list_threads_with_counts` (200 threads / 5000 messages): exactly 1 SQL
  statement regardless of row count — the pre-existing GMAIL-008 N+1 fix
  still holds.
- `list_jobs_by_status_after_id('APPLIED', limit=100)` (1000 jobs): uses
  `jobs_pkey` via keyset pagination, 0.133ms — well-designed, no index
  needed beyond the existing PK.

**Per Section 9's explicit instruction:** an index is not added here — it
would require an Alembic migration, and the instruction reserves that for
Codex once the finding is verified (which it now is, live, against a real
Postgres instance with `EXPLAIN ANALYZE`). This is **analysis only**.

**Codex:** Yes — candidate migration: `CREATE INDEX ix_jobs_last_seen_at ON
jobs (last_seen_at DESC)` (or a composite with `status` if the shortlist
query also filters by status — worth checking call sites before deciding the
exact column set). **Astra:** No.

---

## BOUND-006 — `expected_review_version`/`edit_note`/`decision_note` unbounded, unlike sibling fields (Fixed)

**Priority:** Low
**Evidence:** `app/models/review_package.py`, `app/models/candidate_profile.py`
(`CandidateProfilePatchRequest.expected_profile_version`),
`app/models/response_draft.py` /
`app/models/follow_up.py` (`.note` fields, both already `max_length=2000`)

`ReviewPackagePatchRequest.expected_review_version`,
`ReviewPackageApproveRequest.expected_review_version`,
`ReviewPackageRejectRequest.expected_review_version` had no `ge=1` bound,
unlike the structurally identical sibling
`CandidateProfilePatchRequest.expected_profile_version` (which already has
`Field(ge=1)`). Not a live bug — a `0`/negative value was already rejected
downstream by the optimistic-concurrency CAS check with a `409` — but it's a
genuine boundary inconsistency between two fields with the same role,
caught earlier and cheaper (422 at the schema boundary) once fixed.

Similarly, `ReviewPackagePatchRequest.edit_note` and
`ReviewPackageApproveRequest`/`ReviewPackageRejectRequest.decision_note`
were unbounded free text, unlike the sibling `note` fields on
`ResponseDraftApprovalRequest`/`FollowUpApprovalRequest`, both of which
already carry `max_length=2000`.

**Fix:** added `Field(ge=1)` to all three `expected_review_version` fields
and `Field(default=None, max_length=2000)` to `edit_note` and both
`decision_note` fields in `app/models/review_package.py`.

**Deliberately NOT fixed:** `CVContentPatch`/`BewerbungContentPatch`'s free
text fields (`professional_title`, `professional_summary`, `subject`,
`salutation`, `opening`, `closing`, `signature_name`,
`body_paragraphs[].text`) remain unbounded. No clean sibling precedent
exists for bounding *this* class of content field elsewhere in the
codebase — the candidate profile's own analogous scalar text fields are
also unbounded — so per Section 2/17's "do not add arbitrary bounds merely
because a field is currently unlimited" instruction, these are left as-is.
Documented here as **BOUND-006b** for Codex to weigh if a project-wide
convention for long-form content fields is ever established.

**Regression tests** (added to `tests/test_review_package_endpoints.py`):
`test_approve_expected_version_zero_rejected_at_schema_boundary`,
`test_patch_expected_version_zero_rejected_at_schema_boundary`,
`test_decision_note_over_2000_chars_rejected`,
`test_decision_note_at_2000_chars_accepted`. All 49 tests in the file pass
(45 pre-existing + 4 new).

**Codex:** No (narrow, deterministic, matches Safe Fix Policy).
**Astra:** No.

---

## BOUND-007 — Stale comment in `response_draft_repository.py` (Fixed)

**Priority:** Trivial
**Evidence:** `app/db/response_draft_repository.py`

A comment above `RESPONSE_DRAFT_HISTORY_DEFAULT_LIMIT = 20` /
`RESPONSE_DRAFT_HISTORY_MAX_LIMIT = 100` claimed these values "mirror
`GMAIL_ANALYSES_DEFAULT_LIST_LIMIT` / `GMAIL_ANALYSES_MAX_LIST_LIMIT`" in
`app/api/routes.py` — but those are actually `50`/`200`. The two limit tiers
have always been numerically different; the comment was simply wrong (not a
behavior bug — the values themselves are fine, deliberately their own,
smaller tier for a per-message history endpoint vs. a top-level list
endpoint).

**Fix:** corrected the comment to state the tiers are deliberately
different, not a mirror. Comment-only change, zero behavior change, `ruff
check`/`ruff format --check` clean.

**Codex:** No. **Astra:** No.

---

## BOUND-009 — Auth DoS / rate-limit ordering — see BOUND-002

(Numbered separately in the original working list; consolidated into
BOUND-002 above since they are the same finding. Kept as a cross-reference
so the ID isn't silently missing.)

---

## BOUND-010 — DST transitions (Europe/Berlin) — live-verified safe

**Priority:** Info
**Evidence:** `tests/test_telegram_daily_digest_scheduler.py::TestDSTTransitions`

The prior adversarial pass (`hardening/adversarial-r1`) downgraded its own
DST claim to "static, not live-verified" after self-review. This wave closes
that gap with genuine live tests.

Computed the exact 2026 Europe/Berlin DST transition instants
programmatically via `zoneinfo.ZoneInfo` (not from memory):
- **Spring-forward:** 2026-03-29, local 02:00 → 03:00 (the hour
  02:00–03:00 local never exists that day).
- **Fall-back:** 2026-10-25, local 02:00–03:00 occurs **twice** (once in
  CEST, once in CET).

Built three tests using frozen, explicit UTC instants (no real sleeping):
1. `test_spring_forward_gate_still_fires_once_the_skipped_hour_has_passed` —
   confirms the daily-digest eligibility gate correctly resolves a "today" 
   date across the skipped hour without raising or silently skipping the
   digest.
2. `test_fall_back_repeated_local_hour_never_sends_twice` — confirms the
   once-per-day watermark is not fooled by the local wall-clock hour
   repeating; the digest does not fire twice for the same calendar day.
3. `test_fall_back_day_after_still_advances_to_a_new_claimable_date` —
   confirms the day after fall-back still correctly becomes newly claimable.

All 3 passed on first run. No DST-related bug found; this upgrades the
prior downgraded claim to genuinely live-verified evidence.

**Codex:** No. **Astra:** No.

---

## BOUND-011 / BOUND-011b — Privacy second-pass: two new leak instances (Fixed)

**Priority:** Medium
**Evidence:** independent re-scan of `app/` for
`logger.`/`print(`/`traceback`/`exc_info`/`str(exc)`/`repr(exc)`/`Exception(`/
`HTTPException(detail=`/`ValidationError`/connection strings/`Authorization`/
`X-API-Key`/password/token/body/subject/sender/recipient — deliberately NOT
reusing the prior session's "all clear" conclusion (which had already missed
one instance of this exact pattern once before: HARD-003).

**BOUND-011 — `app/services/follow_up_send.py`:**
`_ThreadLockHeartbeat._run`'s exception handler logged
`logger.warning(..., exc_info=True)` on lock-renewal failure — a third
instance of the same leak-pattern class as HARD-002 (Bundesagentur) and
HARD-003 (company_research), both already fixed with the convention of
logging `type(exc).__name__` instead of the raw exception. This sibling
(`app.services.automation._RunLeaseHeartbeat`) had already been fixed;
`_ThreadLockHeartbeat` had not.

**Fix:** changed to
`logger.warning("follow_up_send_lock_heartbeat_renewal_error gmail_thread_id=%s error_type=%s", self._thread_id, type(exc).__name__)`,
matching the established convention.

**Regression test:** `test_heartbeat_renewal_error_log_never_echoes_raw_exception_text`
in `tests/test_follow_up_send_service.py::TestLeaseRenewalHeartbeat` — injects
a sentinel string via a monkeypatched `renew_thread_lock` raising
`RuntimeError("SECRET_THREAD_LOCK_RENEWAL_DETAIL_MUST_NOT_LEAK")`, asserts
the sentinel is absent from `caplog.text`, the event name and
`"RuntimeError"` are present, and every heartbeat log record has
`exc_info is None`. Confirmed failing before the fix, passing after.

**BOUND-011b — `app/collectors/bundesagentur.py`:**
The `json.JSONDecodeError` handler embedded
`response.text[:100]` directly into the `BundesagenturAPIError` message,
which reaches the API client via `routes.py`'s `detail=f"...: {exc}"`
pattern — i.e. an arbitrary snippet of the upstream API's raw response body
(which could contain a WAF/proxy error page with internal details) was
exposed to any authenticated API caller.

**Fix:** the snippet is now logged server-side only
(`logger.warning("bundesagentur_non_json_response status=%s body=%s", ...)`),
and the exception message is snippet-free
(`f"...non-JSON response body (status={response.status_code})"`).

**Regression test:** `test_non_json_response_body_text_never_reaches_exception_message`
in `tests/test_collectors_bundesagentur.py` — asserts a sentinel string is
absent from `str(exc_info.value)` but present in `caplog.text`.

Both fixes are narrow, deterministic, regression-tested, no schema/outbound/
approval change — match the Safe Fix Policy.

**Codex:** No. **Astra:** No.

---

## BOUND-012 — `ResponseDraftNotApprovableError` maps to 422 with two different detail strings (documented, deferred)

**Priority:** Low
**Evidence:** `app/api/routes.py:1511-1515` and `:1569-1573` (error-contract
research agent's grep of all 118 `HTTPException` sites, cross-checked
manually)

The same domain exception (`ResponseDraftNotApprovableError`) is caught in
two different route handlers and mapped to the same HTTP status (`422`) but
with two textually different `detail` strings depending on which route
context raised it. Not a correctness bug (both are 422, both are
descriptive), but an inconsistency an API consumer parsing `detail` text
(rather than relying on status code alone) could be tripped up by.

**Not fixed:** changing client-visible `detail` text is a minor but real
behavior change (any consumer string-matching on the message would break) —
outside the Safe Fix Policy's "no breaking API change" bar. Documented for
Codex to decide whether unifying the message is worth the (small) breaking
change.

**Codex:** Yes. **Astra:** No.

---

## BOUND-013 — `automation_follow_up_cycle_enabled=True` without Gmail sync silently never becomes eligible (documented, deferred)

**Priority:** Low
**Evidence:** config cross-field analysis (research agent B2), confirmed by
reading `app/services/automation.py`'s follow-up candidate-selection path.

If an operator sets `automation_follow_up_cycle_enabled=True` but never runs
Gmail sync (e.g. by choice — a valid setup where Gmail ingestion happens some
other way, or hasn't started yet), the follow-up candidate scan always
returns `NOT_ELIGIBLE` for every job with no distinguishing status field
between "not yet due" and "structurally can never become eligible without
Gmail data." This is a silent no-op, not a crash or incorrect action.

**Not fixed as startup validation:** per Section 15's bar ("configuration
can never be useful AND failure occurs deterministically later AND no
legitimate configuration becomes invalid"), this fails the third condition —
there are legitimate real scenarios (manual/external Gmail sync feeding the
same tables, or a deliberately staged rollout) where this combination is
valid and should NOT be rejected at startup.

**Codex:** Yes — consider whether the automation status/candidate response
should carry an explicit "blocked: no Gmail data available" reason instead
of a bare `NOT_ELIGIBLE`, to make the silent-no-op state observable to an
operator without reading logs. **Astra:** No.

---

## BOUND-014 — `automation_scheduler_poll_seconds > automation_scheduler_interval_seconds` produces confusing effective cadence (documented, deferred)

**Priority:** Low
**Evidence:** config cross-field analysis (research agent B4), confirmed
against `app/scheduler.py`'s poll-loop implementation.

Both values pass their individual `ge`/`le` bounds independently, but when
`automation_scheduler_poll_seconds` exceeds
`automation_scheduler_interval_seconds`, the poll interval — not the
configured "run every N seconds" interval — becomes the real effective
cadence, since the scheduler only checks "is a run due" once per poll tick.
An operator who sets `interval_seconds=60` expecting hourly-ish behavior but
leaves `poll_seconds` at a larger default would see runs firing less often
than the interval implies, with no warning.

**Not fixed as startup validation:** both values are individually legal, and
neither is "can never be useful" — the combination is merely
counter-intuitive, not invalid. Documented for Codex to consider a
clarifying log line at startup ("effective cadence is poll_seconds, not
interval_seconds, because X > Y") rather than a hard validation error.

**Codex:** Yes. **Astra:** No.

---

## BOUND-015 — Malformed email adversarial corpus (15 cases, no crashes)

**Priority:** Info
**Evidence:** `tests/test_malformed_email_corpus.py`

Constructed a local synthetic MIME corpus (no real mailbox credentials) and
fed it directly into the real, lowest-level parsing functions
(`GmailImapProvider._parse_message`, `XingEmailCollector._process_message`),
bypassing the IMAP-fetch simulation layer to exercise the actual parsing
code path:

- Missing `From`, missing `Subject`, multiple `From` headers.
- Unknown charset (fallback behavior), malformed encoded-word, well-formed
  unusual charset.
- Empty body, `NUL` byte in body, `NUL` byte in subject.
- Very long `Subject` (truncated to `MAX_SUBJECT_LENGTH=998`), very long
  `From` address, invalid `Date` header, malformed `Message-ID`.
- XING-specific: missing `From`/`Subject` skipped cleanly
  (`_process_message` returns `(None, True)` — no crash, no trust
  escalation).

All 15 tests passed on first run — no crash, no unbounded behavior, no
misclassification found anywhere in the corpus. No fix needed; documented
as verified-safe evidence for this attack surface.

**Codex:** No. **Astra:** No.

---

## BOUND-016 — Mutation testing of 5 critical invariants (no weak tests found)

**Priority:** Info
**Evidence:** live, temporary mutations (using the `if False and (...)`
idiom for minimal, easily-revertible diffs), each confirmed to make the
relevant test suite fail specifically (never vacuously), then immediately
reverted (`git diff --stat <file>` confirmed empty before moving to the
next).

| Invariant | File mutated | Result |
|---|---|---|
| Approval required before send | `app/services/response_draft_send.py` | Existing tests failed specifically on the approval-gate assertion |
| Rate limiter enforcement | `app/security/rate_limit.py` | Existing tests failed specifically on the 429 assertion |
| API key auth | `app/security/auth.py` | Existing tests failed specifically on the 401 assertion |
| Lease-ownership CAS | `app/db/automation_repository.py` (`renew_run_lease`) | Existing tests failed specifically on lease-stolen/ownership assertions |
| Fingerprint uniqueness | `app/db/repositories.py` (`_fingerprint`) | Existing tests failed specifically on dedup assertions |

No mutation was caught only incidentally or missed entirely — no weak or
tautological test was found for any of these 5 invariants. No new tests were
required as a result of this exercise; documented as verified evidence of
test-suite quality for these specific invariants.

**Codex:** No. **Astra:** No.

---

## API state-machine transition tables (Section 6)

Built via direct reading of the service-layer state-transition guards (not
guessed), cross-checked against existing test coverage. No real outbound
messages were sent during this analysis — all state-machine checks were
either static code reading or `TestClient`/service-layer calls against a
disposable DB with a fake/no-op outbound provider.

### Review package (`ReviewPackageService` — `app/services/review_package.py`)

| Current state | Action | Expected | Actual (verified) |
|---|---|---|---|
| No review exists | `POST .../review-package` | 201, new `PENDING_REVIEW` review at version 1 | Matches. `create()` fails closed if candidate profile or job snapshot is stale/missing (`ReviewProfileChangedError`/`ReviewJobChangedError`/`ReviewCurrentProfileMissingError`) — never silently recreates a profile to pass the check. |
| `PENDING_REVIEW`, version N | `PATCH` with `expected_review_version=N` | 200, new revision, version N+1 | Matches (`patch()`, guarded by `record.status != "PENDING_REVIEW"` at `review_package.py:163`, CAS at `decide_review`/atomic update). |
| `PENDING_REVIEW`, version N | `PATCH` with `expected_review_version=N-1` (stale) | 409 version conflict | Matches — `_resolve_cas_conflict` (line 72-82) re-reads current state after a failed CAS and raises `ReviewVersionConflictError`, never guesses. |
| `APPROVED` or `REJECTED` | `PATCH` (any version) | 409 "not pending", not a version-conflict 409 | Matches — `_resolve_cas_conflict` checks `status != PENDING_REVIEW` **first**, so a terminal-state review always reports the more specific "review is no longer pending" error, never a misleading version-conflict message, even if the version also happens to mismatch. |
| `PENDING_REVIEW`, has manual overrides, ack not set | `POST .../approve` | 422 `acknowledge_manual_overrides` required | Matches — checked at `approve()` line 255-256, after freshness checks, before the CAS. |
| `PENDING_REVIEW`, version N | `POST .../approve` version=N | 200, status→`APPROVED`, `approved_revision_id` pinned to latest revision at approval time | Matches (`approve()` lines 258-285). `approved_revision_id` is set exactly once and never recomputed by a later action (enforced structurally — PATCH is rejected once status leaves `PENDING_REVIEW`). |
| `PENDING_REVIEW` | `POST .../approve`, but candidate profile changed since creation | 422/409 freshness error, not a stale approval | Matches — rechecked at approval time (line 240-246), not just at creation (spec section 7 compliance verified in code, not just claimed). |
| `APPROVED`/`REJECTED` | `POST .../approve` again | 409 "not pending" | Matches (same guard as PATCH). |
| `PENDING_REVIEW`, version N | `POST .../reject` version=N | 200, status→`REJECTED` | Matches (`reject()` lines 287-298), same CAS/guard pattern as approve, no freshness recheck required for reject (no downstream irreversible action is authorized by a reject, unlike approve). |

**No 500s, no corruption, no unsafe side effects found.** The
status-before-version-conflict check ordering (confirmed above) is itself a
noteworthy correctness property: it guarantees a terminal-state review can
never be mistaken for a "just needs a version bump and retry" situation by
an API consumer.

### Response draft send (`app/services/response_draft_send.py`, cross-referenced with `test_response_draft_send_service.py`)

| Current state | Action | Expected | Actual (verified) |
|---|---|---|---|
| Draft not approved | `send_response_draft` | `ResponseDraftNotApprovedError`, provider never called | Matches — `TestStaleDraftRevisionMismatch` confirms `provider.call_count == 0` on rejection. |
| Draft approved on an OLD revision; a NEWER revision now exists for the same message (e.g. after a candidate-profile edit) | `send_response_draft(new_draft.id)` | Rejected — an approval pinned to the old revision id must never authorize the new one | Matches exactly — this is the specific scenario `TestStaleDraftRevisionMismatch::test_approving_an_old_revision_does_not_authorize_a_newer_one` exists to prove; verified passing. The **old**, still-approved revision remains independently sendable (`record.status == "SENT"` for `old_draft.id`) — approval identity is per-revision, not per-message. |
| Draft sent once successfully | `send_response_draft` again (retry) | Rejected — no double-send | Matches (`TestDoubleSendRetryConcurrency`, pre-existing coverage; this is the HARD-008 "client doesn't know if it succeeded" reference case documented in `docs/API_RETRY_SEMANTICS.md`, intentionally not re-fixed per Section 7's instruction). |

### Automation run trigger (`POST /automation/runs`, `app/services/automation.py`, `app/scheduler.py`)

| Current state | Action | Expected | Actual (verified) |
|---|---|---|---|
| `automation_scheduler_enabled=False` (background scheduler loop never runs) | `POST /automation/runs` | Still triggers exactly one synchronous run cycle | Matches, **by design** — confirmed via direct grep: `automation_scheduler_enabled` is referenced nowhere in `app/api/routes.py`; it only gates the background poll loop in `app/scheduler.py` (`if settings.automation_scheduler_enabled: ...`). The manual-trigger endpoint is intentionally independent of whether the autonomous scheduler is on. Not a bug — this is the documented manual-override path an operator uses to run a cycle on demand regardless of scheduler state. |
| A run is already in progress (lease held) | `POST /automation/runs` | Second call does not run a concurrent duplicate cycle; lease/CAS in `automation_repository.py`'s `renew_run_lease`/lease-acquire path governs this | Matches — pre-existing coverage (not re-derived here; this exact lease-ownership CAS was one of the 5 invariants exercised by the BOUND-016 mutation test above, confirming the test suite genuinely catches a broken lease check). |

**No real outbound messages were sent during any of this analysis** — all
response-draft-send state checks used the existing `FakeOutboundProvider`
test double already present in the test suite; the automation-run checks
were static-code/grep verification, not live execution against Gmail/XING.

---

## Retry semantics — see `docs/API_RETRY_SEMANTICS.md`

Full per-endpoint inventory (Retry-safe / DB-enforced idempotency / dup-row /
dup-external-effect) is in that document, built this wave. Summary: the only
two genuinely unsafe-to-retry write endpoints are `POST
/jobs/{id}/bewerbung-draft` and `POST /jobs/{id}/review-package` (both
always insert, no cache-identity constraint on retry). HARD-008 is cited
there as the pre-existing reference "client can't tell if it succeeded"
example and was deliberately **not** re-fixed in this wave, per instruction.

## Data retention / capacity projection — see `docs/DATA_RETENTION_CAPACITY.md`

Rough row-count projections (not fake-precise disk sizes) at 1 month / 1
year / 3 years under light/normal/heavy usage assumptions, for every major
table, with archive/prune/partition/keep-forever classification. Conclusion:
no table requires retention action within any realistic 1-3yr horizon, even
under "heavy" assumptions. Retention itself was **not implemented**, per
instruction.

---

## Validation

- **Full `pytest -q`:** 2006 passed, 7 skipped (2013 collected), 0 failed.
  Run in 4 sequential chunks by test file (a single unsplit background run
  was killed by the environment partway through twice, both times with zero
  failures observed up to the kill point — splitting by file was the
  reliable path to a complete, green result, not a response to any failure).
- **`ruff check` / `ruff format --check`:** run per-file as each fix landed
  (all clean); final full-tree pass: `ruff check app tests alembic` — all
  checks passed; `ruff format --check app tests alembic` — 230 files already
  formatted.
- **Alembic:** no model changes were made in this wave (BOUND-005's index
  candidate is analysis-only, not applied). `alembic heads` is expected to
  remain `c7d3f9a1e5b8`, unchanged from base.
- **PostgreSQL:** the BOUND-005 capacity simulation ran against a disposable
  local `postgres:16` Docker container (migrated via the real Alembic chain,
  then removed after use) — not SQLite, since query-plan behavior
  (`Seq Scan`/`Index Scan`) is Postgres-specific and would not be visible
  against SQLite.

---

## Explicitly NOT done in this wave (per instructions)

- No merge of `hardening/api-boundaries-r1`.
- No modification of `hardening/adversarial-r1`.
- No Codex or Astra invocation.
- No modification of `pyproject.toml`.
- `.mcp.json` / `.tmp/` never staged.
- HARD-008 not re-fixed (reference example only).
- No retention implementation (projection only).
- No index added for BOUND-005 (analysis only — needs a migration).
- No startup validation added for BOUND-013/BOUND-014 (neither meets the
  "can never be useful" bar).
- No auth/rate-limit reordering implemented (BOUND-002/009 — current
  ordering already confirmed non-exploitable for lockout).
- CVContentPatch/BewerbungContentPatch free-text fields left unbounded
  (BOUND-006b).
