# API Retry Semantics — JobTriage

For every write endpoint: what happens if a client retries after a timeout, connection reset, or 500/502/503 — where the client genuinely does not know whether the first attempt succeeded. This is a different question from "the request failed" (client can safely retry) — this document is specifically about the "**client doesn't know**" case.

`HARD-008` (`docs/ADVERSARIAL_HARDENING_REPORT.md`) is the reference example of a genuinely ambiguous, irreversible side effect (an email send that may or may not have gone out) — it is **not** re-fixed here, only cited as the calibration point for what "unsafe" looks like.

## Legend

- **Retry-safe:** repeating the identical request after an uncertain outcome never produces a worse result than not retrying.
- **DB-enforced idempotency:** a real `UniqueConstraint` or CAS check, not just "the code happens not to duplicate things today."
- **Dup row / dup external effect:** what a retry-after-uncertain-outcome could produce in the worst case.

## Read-only endpoints (all GET routes)

Trivially retry-safe — no state is ever mutated. Not tabulated individually.

## Write endpoints

| Endpoint | Retry-safe | DB-enforced idempotency | Dup row? | Dup external effect? |
|---|---|---|---|---|
| `POST /jobs/score` | **YES** | `uq_jobs_fingerprint` (`app/db/models.py`) via `upsert_job` | NO | NO — Telegram notify gated by `created` flag, never re-sent on an update |
| `PATCH /jobs/{id}/status` | **YES for data, but the retry itself may 409** | Atomic CAS `UPDATE ... WHERE status IN allowed_sources` | NO | N/A | If attempt 1 actually committed and the client never saw the response, an identical retry sees `current == target` — not a valid transition — `409 InvalidStatusTransitionError`. No duplication risk either way; the client must `GET` to distinguish "already applied" from "genuinely invalid." |
| `PATCH /candidate-profile` | **YES (CAS)** | `expected_profile_version` optimistic-concurrency CAS, single `UPDATE ... WHERE id=1 AND profile_version=:expected` | NO | N/A | Same shape as job-status: a retry after a real commit sees a version mismatch → `409`, never double-applies. |
| `POST /jobs/{id}/research` | **Mostly** | Cache-identity read; a `FAILED` row is persisted for diagnostics but re-running is a normal refresh, not a create | Possibly minor (extra diagnostic `FAILED` rows) | **YES** — real outbound call to the research provider each invocation unless served from cache; a retry after a timeout re-triggers the external call. In v1, the only shipped provider makes zero network requests, so this is currently theoretical. |
| `POST /jobs/{id}/match` | **YES** (unless `force_recompute=true`) | `uq_candidate_job_matches_cache_identity` on `(job_id, candidate_profile_version, job_snapshot_fingerprint, algorithm_version)` | NO | NO — pure computation |
| `POST /jobs/{id}/cv-draft` | **YES** (unless `force_recompute=true`) | `uq_candidate_cv_drafts_cache_identity` on `(match_id, cv_adapter_version)` | NO | NO — deterministic, zero I/O |
| `POST /jobs/{id}/bewerbung-draft` | **NO** | **None** — `bewerbung_drafts` has no cache-identity constraint at all; every successful call inserts a new row by design | **YES — always inserts** | **YES** — calls the bewerbung provider each time; a retried request after a false-negative timeout produces both a duplicate DB row and a duplicate provider invocation |
| `POST /jobs/{id}/review-package` | **NO** | **None** — no `UniqueConstraint` on `(cv_draft_id, bewerbung_draft_id)`; unconditional insert | **YES — always inserts** a new `PENDING_REVIEW` row | NO — zero I/O beyond the database |
| `PATCH /review-packages/{id}` | **YES (CAS)** | `expected_review_version` CAS via `_resolve_cas_conflict` | NO (a new revision per successful patch is intended, not a bug; a stale retry is rejected by the CAS before any revision is inserted) | NO |
| `POST /review-packages/{id}/approve` | **YES (CAS)** | Same version CAS + `ReviewNotPendingError` (only `PENDING_REVIEW → APPROVED`) | NO | NO — approval never sends anything |
| `POST /review-packages/{id}/reject` | **YES (CAS)** | Same version CAS + `ReviewNotPendingError` | NO | NO |
| `POST /collectors/bundesagentur/run` | **YES for DB effects; wasteful but harmless otherwise** | Per-job `uq_jobs_fingerprint` upsert; Telegram notify gated by `created` | NO | **Partial** — a retry after attempt 1 actually completed triggers a full second upstream API fetch, but no duplicate rows/notifications (fingerprint + `created` gating) |
| `POST /collectors/xing/run` | Same shape as Bundesagentur | Message-ID tracking + fingerprint upsert | NO | Same as above — duplicate IMAP fetch, no duplicate persisted jobs |
| `POST /automation/runs` | **Mostly YES** | Partial unique index `uq_automation_runs_one_running_per_account` (`status='RUNNING'`) → `409 AutomationRunAlreadyInProgressError` while genuinely running; stale leases reconciled via CAS | NO duplicate `automation_runs` row while genuinely running | If the retry lands *after* run 1 already finished, the index no longer blocks it and a full second cycle (redundant collector fetches) executes — same "wasted external call, no duplicate persisted data" shape as the collectors |
| `POST /gmail/sync` | **YES** | `uq_gmail_messages_account_provider_identity` on `(account_key, mailbox, uid_validity, uid)` | NO | NO — read-only IMAP, never mutates the mailbox |
| `POST /gmail/messages/{id}/analyze` | **YES** | `uq_gmail_message_analyses_identity` on `(gmail_message_id, analysis_version, input_fingerprint, context_fingerprint)` — "re-analyzing returns the existing revision" | NO | NO — deterministic, no LLM/external call |
| `POST /gmail/messages/{id}/response-draft` | **YES** | `uq_response_drafts_identity` on `(gmail_message_id, analysis_id, candidate_profile_version, generator_version)` | NO | NO — deterministic, no LLM/external call |
| `POST /response-drafts/{id}/decision` | **YES** | `uq_response_draft_approvals_response_draft` — one approval row per draft, `409 ResponseDraftAlreadyDecidedError` on a second call | NO | NO — decision never sends anything |
| `POST /response-drafts/{id}/send` | **See HARD-008 — the reference example of "client doesn't know."** | `uq_response_draft_sends_response_draft` + `claim_send_attempt`/`begin_transmission` CAS. Most crash points resolve correctly (claim-before-send fails closed; a genuinely ambiguous provider outcome reaches a terminal `UNCERTAIN` state, never auto-retried). **The one confirmed gap (HARD-008, unfixed):** if the SMTP send genuinely succeeds but the local commit that would record `SENT` fails (crash/DB blip at that exact instant), the row is left `PENDING` forever with **no recovery path** — unlike the follow-up send sibling below. | N/A (send row unique) | **Documented gap, not fixed.** A caller retrying a `PENDING`-stuck draft gets `409 ResponseDraftSendInProgressError` forever, not a resolution — this is the "client cannot tell whether it succeeded, and neither can the server's own state" case in its purest form in this codebase. |
| `POST /follow-ups/evaluate` / `POST /jobs/{id}/follow-up/evaluate` | **YES** | `uq_follow_up_proposals_anchor_fingerprint` on `(account_key, anchor_gmail_message_id, input_fingerprint)` — idempotent for unchanged inputs | NO | NO — pure local scan |
| `POST /follow-ups/{id}/decision` | **YES** | `uq_follow_up_approvals_follow_up_proposal` — `409 FollowUpAlreadyDecidedError` on a second call | NO | NO — never sends anything |
| `POST /follow-ups/{id}/send` | **Better than response-draft-send, but still an "UNCERTAIN" design, not a "never ambiguous" one.** | `uq_follow_up_sends_follow_up_proposal` + a `send_attempted` CAS flag `begin_transmission` sets BEFORE dispatch — this is the mechanism `response_draft_send.py` lacks. A crash after SMTP success but before the local commit is recognized on the NEXT request (`send_attempted=True` with no terminal state yet) and degrades to a terminal `UNCERTAIN` rather than staying permanently `PENDING`. | N/A | Ambiguous outcomes still exist (SMTP itself is not proof-of-delivery) and are still never auto-retried — same "requires a human to reconcile `UNCERTAIN`" design as response-draft-send, just with a working recovery path for the specific stranded-PENDING crash HARD-008 documents for its sibling. |

## Summary

- **Genuinely unsafe to retry (silent duplicate row + duplicate side effect on an uncertain-outcome retry):** `POST /jobs/{id}/bewerbung-draft`, `POST /jobs/{id}/review-package`. Neither has an external irreversible effect worse than "a human has to notice and discard an extra draft/PENDING review" — annoying, not dangerous, and not fixed here (adding a cache-identity constraint would be a real, if narrow, DB-schema-adjacent change reserved for a reviewed follow-up, not a "safe fix" under this branch's own policy since it touches idempotency semantics).
- **The one endpoint with a confirmed, unrecoverable "client doesn't know and neither does the server" gap:** `POST /response-drafts/{id}/send` (HARD-008) — already documented, already deliberately deferred to Codex/Astra, not revisited here.
- **Everything else** is either trivially idempotent (cache-identity/CAS-backed) or, in the collector/automation-run case, merely wasteful (a redundant external fetch) on retry-after-uncertainty, never duplicative of persisted state or user-visible effects.
