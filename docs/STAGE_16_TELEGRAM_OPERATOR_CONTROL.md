# Stage 16 — Telegram Operator Control

Telegram becomes the operator's control surface for Stage 15 applications:

```
vacancy discovered → scored/matched → review package prepared
  → Telegram operator card → DETAILS / APPROVE / SKIP / LATER
```

**Stage 16 records operator decisions. It never submits an application.**
Telegram APPROVE means *"the operator approves this exact review package for
the NEXT controlled step"* — not *"send it now"*. A future submission stage
keeps its own explicit send gate.

Operator procedures: [TELEGRAM_OPERATOR_RUNBOOK.md](TELEGRAM_OPERATOR_RUNBOOK.md).

## 1. Components

| Concern | Module |
|---|---|
| Vocabulary (decisions, delivery states, actors) | `app/models/telegram_operator.py` |
| Tables (cards, decisions, notification ledger) | `app/db/models.py`, migration `e3a9c5d7f2b4` |
| Persistence primitives | `app/db/telegram_operator_repository.py` |
| Rendering (pure: card, details, callback data, notifications) | `app/services/telegram_operator_render.py` |
| Transport (configured operator chat only, bounded retry) | `app/services/telegram_operator_transport.py` |
| Workflow (authorization, readiness, decisions, override, card send) | `app/services/telegram_operator_control.py` |
| Lifecycle notifications + dedup | `app/services/telegram_operator_notifications.py` |
| Bot callback handler (existing polling bot) | `app/services/telegram_bot.py::on_operator_callback` |
| Operator CLI (dry-run first) | `scripts/stage16_telegram_operator.py` |

Rendering, transport and workflow are separate: rendering and workflow are
unit-tested with no network; the bot handler only relays outcomes.

No second bot runtime and no webhook: callbacks are handled by the existing
embedded polling bot (`start_bot`), registered for `^s16:` callback data.

## 2. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | existing settings, reused |
| `TELEGRAM_OPERATOR_CONTROL_ENABLED` | `false` | gates card sending, callbacks and notifications; independent of every other Telegram flag; listed in the startup safety line |
| `TELEGRAM_OPERATOR_USER_ID` | empty | optional operator Telegram user id (see trust model) |

Nothing else is enabled by Stage 16. The Stage 9A–9D flags, the daily digest
and `OUTBOUND_SENDING_ENABLED` are untouched.

## 3. Trust model

Every callback passes two gates before anything is read:

1. the existing `@require_authorized` chat gate: the chat must be
   `TELEGRAM_CHAT_ID` — any other chat gets **no answer at all** (silence,
   as for every other bot command; a fixed log line without chat id/payload);
2. `telegram_operator_control.authorize`:
   - with `TELEGRAM_OPERATOR_USER_ID` set: the pressing user must be that user;
   - without it: only a **private** chat whose id equals the sender's user id
     is accepted. In a group, any member could press the buttons, so group
     callbacks are refused unless the operator user id is configured.

An unauthorized callback causes no read of application data, no write and no
reply. Callback data is never trusted: it carries only an opaque random token
(`s16:<a|s|d|l>:<16 chars>`, 22 bytes, limit 64), resolved server-side to a
card, after which the application and its review package are **re-read from
the database** and every check is re-evaluated.

Telegram identities are stored only as SHA-256 references (`operator_ref`,
`chat_ref`, `callback_ref`), never raw.

## 4. Decision model

`application_operator_decisions` (append-only: ORM guards + database
triggers refuse UPDATE/DELETE; same shape as Stage 15's events):

| Column | Purpose |
|---|---|
| `application_id` | the Stage 15 application (NOT NULL — see below) |
| `card_id` | the card pressed (NULL for a CLI override) |
| `review_package_id`, `target_key` | the exact package decided on (`app:<id>:pkg:<id>`) |
| `vacancy_reference` | `source:source_reference` |
| `decision` | `APPROVE` / `SKIP` / `LATER` (CHECK) |
| `supersedes_decision_id` | set only by an explicit override (reason required by CHECK) |
| `idempotency_key` | UNIQUE, deterministic (section 6) |
| `callback_ref` | UNIQUE SHA-256 of the Telegram callback-query id |
| `actor` | `TELEGRAM_OPERATOR` or `CLI_OPERATOR` |
| `operator_ref`, `chat_ref`, `telegram_message_id` | provenance |
| `lifecycle_correlation_key` | the Stage 15 event an APPROVE produced |
| `reason`, `metadata_json`, `created_at` | audit |

Cards are issued for Stage 15 `ApplicationRecord`s only; a package that has no
application yet is first registered with the Stage 15 tracker. DETAILS is an
interaction, never a decision row.

### Semantics

| Button | Effect | Lifecycle |
|---|---|---|
| DETAILS | renders the detail view into the operator chat | none |
| APPROVE | decision row + `PACKAGE_READY → APPROVED` via `ApplicationTracker.transition_application` (origin `OPERATOR`, actor `TELEGRAM_OPERATOR`) **in one transaction** | APPROVED only |
| SKIP | decision row | none — SKIP is not `REJECTED` (employer) and not `WITHDRAWN` |
| LATER | decision row, non-final, queryable | none |

APPROVE never sets SUBMITTED, never calls SMTP, Gmail (send or draft), a
portal, LinkedIn, XING, Indeed, StepStone or the Bundesagentur. The decision
row is flushed into the tracker's unit of work: the tracker commits both, or
any failure (lost CAS, invalid transition) rolls both back.

Decisions apply only before submission (`DISCOVERED`, `MATCHED`,
`PACKAGE_READY`); afterwards every button except DETAILS answers
`NOT_DECIDABLE`.

## 5. Package readiness (`PACKAGE_NOT_READY`)

APPROVE is offered — and accepted — only when **all** hold (re-checked on the
press):

- application status is `PACKAGE_READY` and it names a review package;
- the Stage 6E review exists, is not `REJECTED`, has a current revision;
- the review's job matches the application's job;
- the candidate profile version and the job snapshot fingerprint still equal
  the package's pins (profile/job changed → `PROFILE_CHANGED`/`JOB_CHANGED`);
- the package has no unacknowledged manual edits (`MANUAL_EDITS_UNACKNOWLEDGED`
  — Telegram never acknowledges USER_EDIT content implicitly).

Otherwise the card shows `Package: PACKAGE_NOT_READY (reasons)`, no APPROVE
button, and a forged APPROVE callback is refused with the same reasons.

**Concurrent review changes (Codex S16 P1).** The readiness read before
the write is never trusted on its own. Inside the APPROVE transaction (the
callback and the CLI override SKIP → APPROVE), after the decision row is
flushed and before the lifecycle write commits, the review row is re-read
with `SELECT ... FOR UPDATE` and readiness is re-assessed; the press is
refused (`PACKAGE_NOT_READY`, e.g. `REVIEW_PACKAGE_REJECTED` or
`REVIEW_PACKAGE_CHANGED` for a different status/version/revision) and the
whole transaction rolled back — no decision, no `APPROVED` event. On
PostgreSQL a concurrent Stage 6E reject/revision either commits first (and is
seen) or waits for the approval's commit; on SQLite the flushed INSERT
already holds the database write lock. Proven by
`tests/integration/test_telegram_operator_control_postgres.py`.

**One notion of approval (review lifecycle invariant).** A successful APPROVE
also moves a `PENDING_REVIEW` package to Stage 6E `APPROVED` through the
existing `decide_review` CAS (pinning the shown revision as
`approved_revision_id`) in the **same** transaction as the decision row and
the application `PACKAGE_READY → APPROVED` event — all commit together or
none does. `APPROVED` is terminal in Stage 6E, so a rejection that waited on
the approval resumes into a CAS loss (`409 not pending`), never into
"review `REJECTED` + application `APPROVED`". Independently of Telegram,
`decide_review(REJECTED)` refuses (`ReviewApplicationApprovedError`, API 409)
while any application linked to that package is `APPROVED`; undo the
approval first (override APPROVE → SKIP). Lock order on every path: review
row, then application row (the guard reads applications `FOR NO KEY UPDATE`,
compatible with the KEY SHARE lock the decision INSERT takes via its FK).
An APPROVE → SKIP override returns the application to `PACKAGE_READY` but
leaves the review `APPROVED` (terminal).

**Usable approval = Stage 6E APPROVED AND not withdrawn by the operator
(Codex S16 P1).** The review's `APPROVED` status is history; the CURRENT
authorization is the effective Stage 16 decision. When the effective final
decision of a target bound to the package is `SKIP` (an APPROVE → SKIP
override, or a SKIP pressed before a Stage 6E approval),
`get_latest_approved_review_for_job` -- the single read behind
`GET /jobs/{id}/approved-package` (→ 404, as for "no approved package") and
send readiness (→ `NO_APPROVED_REVIEW`) -- withholds it, with no fallback to
an older approved review. A later SKIP → APPROVE override makes it usable
again. A review with no Stage 16 decision (legacy/direct Stage 6E approval)
is unaffected, and LATER is not SKIP. The effective decision has ONE
definition, `telegram_operator_repository.effective_final_decision`: the
APPROVE/SKIP row no other row supersedes (`supersedes_decision_id`); the
callback/override workflow (`decision_state`) uses the same helper. No
review status, decision or event row is rewritten.

Stage 9D (Gmail draft) applies the same rule at its final locked boundary:
`revalidate_approved_link_locked` refuses an APPROVED review whose effective
Stage 16 decision is SKIP with the existing `NOT_APPROVED` outcome, so an old
"Gmail-Entwurf erstellen" button cannot proceed -- also when the SKIP was
recorded before the Stage 9C approval. Stage 16 SKIP writes (callback and
APPROVE -> SKIP override) take the review row lock before inserting the
decision, so on PostgreSQL a concurrent SKIP either committed before that
revalidation or waits for it.

**SKIP vs. an armed Gmail draft (Codex S16 P1).** The cross-stage
synchronization point is the existing Stage 9D ledger state: a row in
`CREATING` with the network fence set (`append_started_at`, committed by the
arm step before the provider is called). While such an attempt exists for the
package's review, SKIP (callback and APPROVE → SKIP override) refuses and
records nothing (`STATE_CHANGED` / `GMAIL_DRAFT_IN_FLIGHT`); once the attempt
resolved -- CREATED, FAILED or UNCERTAIN -- SKIP proceeds as before. UNCERTAIN
does not block: it is sticky and never re-appends, so a SKIP after it neither
erases it nor allows a retry. An armed attempt left behind by a crashed
process blocks SKIP until Stage 9D classifies it UNCERTAIN (any later
create/recheck press past its frozen deadline). The SKIP check runs after the
review row lock and again after the decision row is flushed; Stage 9D's arm
re-checks the operator decision after its fence write. Lock order: review
row → (Stage 9D) ledger row; (Stage 16) decision INSERT → application row.
Stage 9D never locks applications; Stage 16 only reads the ledger. Proven on
SQLite (`tests/test_stage16_stage9d_arm_race.py`) and PostgreSQL
(`tests/integration/test_stage16_stage9d_arm_race_postgres.py`).

## 6. Idempotency and conflicts

| Situation | Result |
|---|---|
| same callback delivered twice (Telegram retry) | `callback_ref` found → `DUPLICATE_CALLBACK`, nothing written |
| APPROVE pressed twice | `ALREADY_APPROVED`, one decision, one event, no card re-edit |
| SKIP pressed twice | `ALREADY_SKIPPED` |
| LATER pressed twice | `ALREADY_LATER` |
| APPROVE then SKIP (or SKIP then APPROVE, or a final then LATER) | `CONFLICT_REQUIRES_OVERRIDE` — never silently replaced |
| two presses racing past the read checks | `UNIQUE(idempotency_key)` lets one commit; the other re-reads and answers ALREADY_* / CONFLICT |

`idempotency_key` = `<target>:final:<generation>` (APPROVE/SKIP),
`<target>:later:<generation>` (LATER), `<target>:override:<n>` (override);
`generation` counts overrides. The APPROVE lifecycle event's correlation key
`s16:approve:<target>:g<generation>` is deterministic too, so the Stage 15
event cannot be written twice.

**Explicit override** (CLI only, mandatory `--reason`): a new decision row with
`supersedes_decision_id`:

- SKIP → APPROVE: package must be ready; `PACKAGE_READY → APPROVED`;
- APPROVE → SKIP: only while the application is `APPROVED`; recorded as a
  Stage 15 operator status override `APPROVED → PACKAGE_READY`.

A submitted application is never touched by an override.

## 7. Card and details

Card (plain text, ≤ 3000 chars; least important sections dropped first):

```
💼 Acme Vacuum GmbH — Backend Developer (m/w/d)
📍 Hanau

Match: 67%
Required: 67%
Preferred: 100%
Experience: 33%

Matched:
Python · PostgreSQL · Git · Docker

Missing:
CI/CD · German C1 · Kubernetes

Language:
German C1 (missing)

Channel: Employer portal
Package: CV READY · ANSCHREIBEN READY
Status: PACKAGE_READY

Source: bundesagentur
Ref: 20385-0000001-S
Fresh: first seen 2 days ago

Telegram approval never sends an application.

[✅ APPROVE] [⏭ SKIP]
[📄 DETAILS] [🕒 LATER]
```

After a final decision the card is edited in place (`Decision: ✅ APPROVED` /
`⏭ SKIPPED`) and only DETAILS stays active; after LATER it shows `🕒 LATER`
and keeps its buttons. Duplicates do not re-edit. An edit failure is cosmetic
(the decision is already committed).

DETAILS (≤ 3800 chars): source/reference, job URL, matcher breakdown,
missing/partial/matched requirements, language and experience requirements,
package readiness (review id/status/version/revision, CV/Anschreiben draft
ids), review risks, employer research summary, lifecycle history. Salary and
work mode are not collected by any source and are therefore omitted, never
invented.

### PII boundary

View models carry vacancy, matcher, package and lifecycle facts only. CV and
Bewerbung **text**, the candidate's name and contact data are never loaded
into a view; no files are uploaded. External free text (requirement wording,
employer research) is scrubbed of e-mail addresses and phone numbers.

## 8. Card delivery

One card per target (`UNIQUE(target_key)`): states `SENDING → SENT | FAILED |
UNCERTAIN`. A provably FAILED send is retried on the same row (≤ 5 attempts
in total; the transport itself retries a FAILED attempt ≤ 3 times with
backoff). UNCERTAIN is never retried (Telegram may have delivered it); a
SENDING claim older than 5 minutes becomes UNCERTAIN. Only SENT/UNCERTAIN
cards accept presses. Delivery bookkeeping never changes the application.

## 9. Lifecycle notifications

Notified (one message each):

- status change / operator override to `RECEIVED`, `UNDER_REVIEW`,
  `INTERVIEW`, `TECHNICAL_INTERVIEW`, `OFFER`, `REJECTED`;
- an inbound e-mail that Stage 15 recorded **without** a status change because
  a REJECTION/OFFER needs operator confirmation — rendered as
  `⚠️ … Possible rejection detected … Status unchanged … Operator confirmation
  required`. Nothing is applied; the operator confirms with the Stage 15
  tracker. Confirmation buttons are **Stage 17 backlog**.

```
📩 ALD Vacuum Technologies GmbH
Backend Developer (m/w/div.)

Status changed:
SUBMITTED → RECEIVED

Source:
Gmail receipt confirmation

No action required.
```

Dedup: `telegram_lifecycle_notifications` has `UNIQUE(application_event_id)`;
the row is claimed (SENDING) before the send, so a re-run or a concurrent
sweep sends nothing. FAILED → retried on the same row (bounded); UNCERTAIN →
never retried. Notification retry is bookkeeping — it never replays business
state. The sweep is operator-driven (`notifications` CLI, default window 7
days so historic events are not notified on first enable).

## 10. Logging

Structured events: `telegram_operator_card_rendered`,
`telegram_operator_message_sent`, `telegram_callback_received`,
`telegram_callback_refused` (with a reason), `telegram_callback_details`,
`telegram_decision_recorded`, `telegram_duplicate_callback`,
`telegram_notification_sent`, `telegram_notification_duplicate`,
`telegram_transport_failed`. Never logged: bot token, chat/user ids of
unauthorized senders, callback payloads, message text, CV content.

## 11. No-employer-outbound invariant

Telegram-to-operator is the only outbound capability Stage 16 introduces.
`tests/test_stage16_operator_notifications.py` proves it on the **import
graph** (including function-level imports): the transitive closure of the
Stage 16 services and CLI contains no SMTP, outbound-mail, Gmail draft,
response/follow-up send, automation or Stage 9B/9C module, the services do
not even reach IMAP, and `httpx` is reached only through
`app.services.telegram` (`api.telegram.org`). A runtime test additionally
patches every employer-facing sender to fail and presses APPROVE.

## 12. Database

One additive migration `e3a9c5d7f2b4` (revises `c8e4a2f6d1b9`; Stage 15's
`b5d2e8f4a1c7`/`c8e4a2f6d1b9` are not rewritten). SQLite + PostgreSQL. Foreign
keys `ON DELETE RESTRICT` to `applications`, `application_events` and
`telegram_operator_cards`. Downgrade drops the three tables (discarding
decisions) — only on a disposable database or after export.

The Stage 15 PostgreSQL test fixture now truncates with `CASCADE`, because the
Stage 16 tables reference `applications`/`application_events`.

## 13. Known backlog (not fixed in Stage 16)

- **Stage 15 accepted P2:** a postposed technical-interview negation such as
  "A technical interview will not take place." can still classify as
  `TECHNICAL_INTERVIEW_INVITATION`.
- **Stage 17:** Telegram confirmation buttons for REJECTION/OFFER e-mails
  (`[Confirm rejection] [Confirm offer] [Details]`); today the notification
  is informational and confirmation goes through the Stage 15 CLI.
- **Stage 17 (Codex S16 P2):** the lifecycle-notification scan takes the
  oldest `limit × scan factor` candidate events in its time window and
  filters already-notified ones afterwards, so once that many notified events
  sit in the window, newer events are overlooked; it needs pagination/keyset over
  un-notified events.
- **Stage 17 (Codex S16 P2):** the Stage 6E reject guard
  (`decide_review(REJECTED)`) refuses only while a linked application is
  exactly `APPROVED`; later statuses (`SUBMITTED`, …) are not covered yet.
- **Test hardening (Codex S16 P3):** the PostgreSQL lock-wait tests infer
  "B is blocked" from thread timing (`join(1.5)` + `is_alive()`); they should
  observe the wait directly (e.g. `pg_locks` / `pg_stat_activity`).
- Card dispatch and the notification sweep are operator-driven CLI commands;
  wiring them into the scheduler is a later decision.
- Callback tokens do not expire; every press re-validates the current state
  instead (stale package → `STALE_CARD`, moved status → `NOT_DECIDABLE`).
