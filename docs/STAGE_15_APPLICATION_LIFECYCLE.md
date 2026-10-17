# Stage 15 — Application Lifecycle & Response Tracking

Stage 15 tracks a real job application from discovery to the employer's
answer, with an immutable, evidence-backed history. Inbound employer mail is
read **read-only**, classified deterministically, correlated conservatively,
and turned into lifecycle events that are idempotent: processing the same
mail any number of times yields one event.

**Nothing in Stage 15 sends anything.** No Gmail send, no Gmail draft, no
SMTP, no Telegram, no portal POST, no recruiter reply. `JobRecord.status` is
never written. `OUTBOUND_SENDING_ENABLED` and `ALEMBIC_AUTO_UPGRADE` stay
`false`.

## 1. Architecture

| Layer | Module | Role |
|---|---|---|
| Vocabulary / DTOs | `app/models/application_lifecycle.py` | Status, event type, origin, channel, response classification enums; `Application`, `ApplicationEvent` |
| State machine | `app/domain/application_lifecycle.py` | Pure transition table, terminal set |
| Response policy | `app/domain/application_response_policy.py` | Pure decision: classification × correlation → transition / record-only / ignore |
| Persistence | `app/db/models.py` (`ApplicationRecord`, `ApplicationEventRecord`), `app/db/application_repository.py` | Tables, CAS update, event insert (no event update/delete path) |
| Service | `app/services/application_tracker.py` | The only writer; one transaction per call |
| Classifier | `app/agents/application_response_classifier.py` | Normalization + refinements on top of the Stage 7B classifier |
| Correlation | `app/services/application_correlation.py` | Message → at most one application |
| Processor | `app/services/application_response_processor.py` | fetch → correlate → classify → decide → (apply) |
| Rendering | `app/services/application_notifications.py` | German notification text only (not wired to any sender) |
| Operator tools | `scripts/stage15_application_tracker.py`, `scripts/stage15_import_ald_application.py` | CLI and one-shot ALD bootstrap |
| API | `GET /api/v1/applications?limit=&after_id=`, `/{id}`, `/{id}/history` | Read-only, keyset-paginated (limit ≤ 200), API key + rate limit |

Why a new entity instead of `JobRecord.status`: an application is
identified by its stable external vacancy identity (Stage 14D
`VacancyIdentity`: source + source reference), not a DB-local job row, and
it needs an append-only audit trail. The coarse Stage 1 `ApplicationStatus`
on `JobRecord` is left exactly as it was.

## 2. Database schema (migration `b5d2e8f4a1c7`, revises `a7c3e9f1b2d4`)

`applications`

- identity: `UNIQUE(source, source_reference)`; `external_job_id`,
  `vacancy_fingerprint`, optional informational `job_id` /
  `candidate_profile_id` / `review_package_id` (not foreign keys — an
  application's history must survive a rebuilt jobs table)
- `company_name`, `role_title`, `location`, `application_channel`
  (CHECK: EMPLOYER_PORTAL/EMAIL/JOB_BOARD/OTHER), `destination_url`,
  `employer_domains_json` (confirmed sender domains)
- `current_status` (CHECK: the 12 statuses), `version` (CAS counter, ≥ 1)
- `submitted_at`, `received_at` (CHECK `received_at ≥ submitted_at`),
  `last_status_changed_at`, `last_employer_contact_at`
- follow-up (informational): `next_followup_at`, `followup_required`
- indexes: `current_status`, `company_name`, `external_job_id`,
  `created_at`, `last_status_changed_at`

`application_events` (append-only)

- `application_id` (FK, ON DELETE RESTRICT), `sequence` (`UNIQUE(application_id, sequence)`)
- `event_type` (CREATED / STATUS_CHANGED / STATUS_OVERRIDDEN / INBOUND_MESSAGE),
  `old_status`, `new_status`, `origin` (SYSTEM/GMAIL/PORTAL/OPERATOR/IMPORT), `actor`, `reason`
- `occurred_at` (when it happened), `recorded_at` (when we wrote it)
- `evidence_hash`, `evidence_summary` (bounded, never a full mail body),
  `correlation_key` (`UNIQUE`), `metadata_json` (classifier rule, signals, hashed Message-ID)
- CHECKs: shape per event type (CREATED has no old status; status events
  name both; INBOUND_MESSAGE never has a new status); STATUS_OVERRIDDEN
  requires a non-empty reason.

The migration is additive. Its downgrade drops both tables (and therefore
lifecycle history); use it only on disposable databases.

## 3. State machine

```
DISCOVERED → MATCHED → PACKAGE_READY → APPROVED → SUBMITTED        (strictly sequential)
SUBMITTED → RECEIVED → UNDER_REVIEW → INTERVIEW → TECHNICAL_INTERVIEW → OFFER
                                    (monotonic; later steps may be skipped)
any non-terminal → REJECTED | WITHDRAWN
```

- Terminal: **OFFER, REJECTED, WITHDRAWN**. OFFER is terminal in Stage 15;
  accepting/declining an offer is a separate lifecycle (Stage 16 backlog).
- Backwards moves are never transitions (a late receipt after an interview
  invitation is out-of-order correspondence, recorded as INBOUND_MESSAGE).
- Leaving a terminal status, or any other correction outside the table, is
  an **operator override**: `actor` and `reason` required, recorded as its
  own STATUS_OVERRIDDEN event with old and new status. History is never
  rewritten.

## 4. Inbound classification

`classify_application_response` reuses the Stage 7B deterministic classifier
(phrase patterns, clause-scoped negation, REJECTION-vs-positive conflict →
OTHER) and adds:

1. normalization: HTML → inert text (`app/utils/html_text.py`, no rendering
   or fetching), quoted reply history removed ("Am … schrieb …:", "On …
   wrote:", "-----Original Message-----", `>` lines), reply/forward subject
   prefixes removed (Re/AW/WG/Fwd/…);
2. TECHNICAL_INTERVIEW_INVITATION (interview + technical/coding marker, or an
   explicit "Einladung zum technischen Interview / Fachgespräch");
3. UNDER_REVIEW_NOTICE only for explicit present-tense review wording and
   only when no Stage 7B lifecycle category matched — a receipt saying
   "in Bearbeitung" stays a receipt, and the future-tense "Sobald wir Ihre
   Unterlagen geprüft haben" is **not** a review notice.

Stage 7B receipt patterns were extended additively with the real ALD
wording: "…deren Eingang wir hiermit bestätigen", "bestätigen … den Eingang
Ihrer Bewerbung", "Eingangsbestätigung Ihrer Bewerbung", subject shape
"Ihre Bewerbung - Eingangsbestätigung".

HTML-only mail: the Stage 7A IMAP provider keeps `body_plain` = text/plain
only (unchanged). Stage 15 added an in-memory `ParsedGmailMessage.body_html_text`
(inert tag stripping, bounded, **never persisted**), used by the live IMAP
path. Messages read from the stored `gmail_messages` table still have no
HTML text and are classified from subject + plain body only.

## 5. Email → status policy

| classification | target | min. correlation |
|---|---|---|
| RECEIPT_CONFIRMATION | RECEIVED | MEDIUM |
| UNDER_REVIEW_NOTICE | UNDER_REVIEW | MEDIUM |
| INTERVIEW_INVITATION | INTERVIEW | MEDIUM |
| TECHNICAL_INTERVIEW_INVITATION | TECHNICAL_INTERVIEW | MEDIUM |
| OFFER | OFFER | **HIGH** (+ classifier HIGH) |
| REJECTION | REJECTED | **HIGH** (+ classifier HIGH) |
| REQUEST_FOR_INFORMATION | — (sets `followup_required`) | MEDIUM |
| OTHER_APPLICATION_MESSAGE / UNKNOWN | — | MEDIUM |

Below MEDIUM nothing is written. MEDIUM+ without a lawful forward
transition is recorded as an INBOUND_MESSAGE event (status unchanged).

## 6. Correlation

Inbound mail is unauthenticated (no SPF/DKIM/DMARC evidence in this
pipeline, same as Stage 7B), so correlation counts independent signals for
each submitted application whose response window (submission − 1 h …
submission + 365 days) contains the message:

- DOMAIN — sender domain is (a subdomain of) a confirmed employer domain
- COMPANY — normalized company name in display name / subject / body
- ROLE — role title without "(m/w/d)"-style suffix in subject / body
- REFERENCE — `source_reference` verbatim, or `external_job_id` only when labelled ("Stellen-ID 962")
- THREAD — replies to a message already correlated to this application (supporting only)

HIGH = DOMAIN + (ROLE or REFERENCE), or REFERENCE + COMPANY.
MEDIUM = DOMAIN + (COMPANY or THREAD), or COMPANY + ROLE.
LOW = any other signal.

**Complete candidate set, bounded memory.** Candidates are read with a
keyset-batched scan over every submitted application (`id > last_id`, 200
rows per query, `ApplicationTracker.iter_correlation_candidates`), so the
201st (or 10 000th) application is evaluated like the first; only matching
candidates are kept. Thread support is an indexed lookup of the referenced
messages' own correlation keys, not a scan of all events. The best grade must be held by exactly one
application with the most signals, otherwise the result is **AMBIGUOUS**
and nothing is written. Own messages, OUTBOUND messages and messages without
a usable timestamp are never correlated. The message time is Gmail's own
arrival time (IMAP INTERNALDATE) when available, the Date header otherwise,
normalized to UTC.

## 7. Idempotency

- Every event carries a deterministic `correlation_key`; `UNIQUE` in the DB.
  - Gmail: `gmail:` + SHA-256(account key, message key); message key is the
    normalized Message-ID, or `imap:<mailbox>:<uidvalidity>:<uid>` without one.
  - Portal submission: `portal-submission:<source>:<reference>`.
- The tracker checks the key first, then inserts the event and CAS-updates
  the application (`id`, `current_status`, `version`) in one transaction. A
  race on the same key ends in a UNIQUE violation that is resolved as a
  duplicate; a lost CAS rolls everything back
  (`ApplicationConcurrencyError`, reported as `CONCURRENT_CHANGE_RETRY`).
- Application creation is idempotent on `(source, source_reference)`.

## 8. Operator workflow and CLI

```
# read
python scripts/stage15_application_tracker.py --database <db> list [--limit N] [--after-id ID]
python scripts/stage15_application_tracker.py --database <db> show 1
python scripts/stage15_application_tracker.py --database <db> history 1
# Gmail (dry run by default; --source stored = already-synced gmail_messages, no network)
python scripts/stage15_application_tracker.py --database <db> process-gmail --since 2026-10-17
python scripts/stage15_application_tracker.py --database <db> process-gmail --since 2026-10-17 --apply
# corrections (dry run unless --apply)
python scripts/stage15_application_tracker.py --database <db> transition 1 INTERVIEW --actor me --reason "phone call" --apply
python scripts/stage15_application_tracker.py --database <db> override 1 RECEIVED --actor me --reason "rejection was for another role" --apply
# one-shot ALD bootstrap
python scripts/stage15_import_ald_application.py --database <db> --receipt-source imap --apply
```

Target guard: the database must already be at the Alembic head (no
auto-upgrade); SQLite paths must be local files; the protected Stage 14B
canonical DB (by SHA-256), Stage 13D paths and `ai_agent-db-1` are refused
(exit 2).

## 9. Safety constraints

- Gmail access: `GmailImapProvider` (`SELECT … readonly`, `BODY.PEEK[]`),
  nothing persisted by the live path; or SELECTs on `gmail_messages`.
- Stage 15 modules import no SMTP/HTTP/Telegram/draft/send code (AST-checked
  by `tests/test_stage15_response_tracking.py`), and a full bootstrap +
  processing run with sockets/SMTP/Telegram/draft functions patched to fail
  makes no such call.
- Logs carry ids, classifications, signals and reasons — never bodies,
  addresses or Message-IDs. Results show a 12-hex message reference only.

## 10. ALD pilot proof (2026-10-17)

- Real receipt fetched read-only: sender domain `ald-vt.de`, display name
  "ALD Vacuum Technologies GmbH", subject "Ihre Bewerbung -
  Eingangsbestätigung", HTML-only, arrival 14:06:17Z, no role or reference
  in the text.
- Classified RECEIPT_CONFIRMATION (rule PHRASE_MATCH:APPLICATION_RECEIVED),
  correlated MEDIUM [DOMAIN, COMPANY].
- On a scratch copy of the canonical DB upgraded to `b5d2e8f4a1c7`
  (canonical file itself unchanged, SHA-256 `874d2cab…e35d`):
  1. `CREATED → SUBMITTED` 2026-10-17T14:02:50Z (origin PORTAL, evidence hash of the portal confirmation)
  2. `SUBMITTED → RECEIVED` 2026-10-17T14:06:17Z (origin GMAIL, keyed by the receipt message)
  - current status RECEIVED, 2 events; bootstrap re-run and two Gmail
    re-runs (dry run and apply) → `ALREADY_PROCESSED`, still 2 events.
- A sanitized copy of the real receipt is the test fixture
  (`tests/stage15_fixtures.py`).

## 11. Known limitations

- No sender authentication: a forged message that matches domain + role could
  record OFFER/REJECTED. Mitigated by the HIGH-only rule for terminal states,
  the manual override path and visible history; a real fix needs
  Authentication-Results evidence (backlog).
- HTML-only messages are fully classifiable on the live IMAP path only; the
  stored `gmail_messages` path sees subject + plain text.
- The production/canonical database is still at `a7c3e9f1b2d4`. Using Stage
  15 there requires an explicit, operator-approved `alembic upgrade head`,
  which changes the canonical DB's SHA-256 (new approval for the Stage 14D
  `serve --expect-sha256` gate). Until then the Stage 14D preflight of the
  old canonical DB reports "database at head: NO".
- `employer_domains` are operator-confirmed per application; an employer
  writing from an undeclared ATS domain correlates via COMPANY + ROLE only.
- Follow-up dates are informational; nothing reminds or sends.

## 12. Stage 16 backlog

- Telegram interactive approval and lifecycle notifications (renderer exists, not wired)
- Portal adapter framework; automatic submission for officially supported portals (with explicit approval)
- UI / dashboard
- Scheduled follow-up reminders
- LinkedIn / XING / other discovery adapters
- Richer employer conversation threading (persisted HTML text, thread view)
- Offer acceptance / decline lifecycle (OFFER is terminal today)
- Sender authentication evidence (Authentication-Results) for terminal transitions
