# Application Tracker Runbook (Stage 15)

All commands are local. None of them sends mail, creates drafts, posts to a
portal or notifies anyone. Use `--json` for machine-readable output.

`<db>` is a SQLite file **already at the Alembic head** (`--database`), or a
PostgreSQL URL (`--database-url`). The tool refuses the protected Stage 14B
canonical database, Stage 13D paths and `ai_agent-db-1`, and any database
not at head (upgrade explicitly; there is no auto-upgrade).

**Production runtime database (v1):**
`D:\AI_agent\runtime\job_triage_runtime.db` — use it as `<db>` for daily
work; day-to-day procedures are in [DAILY_OPERATIONS.md](DAILY_OPERATIONS.md).
The section below describes how the original Stage 15 tracker database was
bootstrapped and stays as history.

## Never use the canonical database

`D:\AI_agent\stage14b_canonical_v12.db` is immutable. Do not upgrade it and
do not pass it to these tools (they refuse it by path identity and by hash).
Create the runtime database from a verified byte copy instead:

    sha256sum D:/AI_agent/stage14b_canonical_v12.db      # must be 874d2cab…e35d
    cp D:/AI_agent/stage14b_canonical_v12.db D:/AI_agent/runtime/application_tracker.db
    DATABASE_URL=sqlite:///D:/AI_agent/runtime/application_tracker.db python -m alembic upgrade head
    sha256sum D:/AI_agent/stage14b_canonical_v12.db      # still 874d2cab…e35d

Use `--database D:/AI_agent/runtime/application_tracker.db` everywhere below.

## List applications

    python scripts/stage15_application_tracker.py --database <db> list
    python scripts/stage15_application_tracker.py --database <db> list --status RECEIVED --status INTERVIEW

Pages are explicit (default 200, max 500, id order). A full page prints
`MORE: page is full; continue with --after-id <id>` on stderr — run again
with that `--after-id`. Gmail processing is not affected by paging: it
always evaluates every submitted application.

## Inspect one application and its history

    python scripts/stage15_application_tracker.py --database <db> show 1
    python scripts/stage15_application_tracker.py --database <db> history 1

History columns: `seq`, `type` (CREATED / STATUS_CHANGED / STATUS_OVERRIDDEN /
INBOUND_MESSAGE), `old` → `new`, `origin`, `actor`, `occurred_at` (event
time) vs `recorded_at` (write time), `reason`, `summary`, `correlation`
(start of the idempotency key).

## Before applying: duplicate check

    python scripts/stage15_application_tracker.py --database <db> check-job <job_id>

Read-only. `ALREADY_APPLIED` (exit 3): a STRONG signal (same job row, BA
reference, posting URL, vacancy fingerprint, or same employer + role).
`POSSIBLE_DUPLICATE`: identical full title at another company (agency
mirror), or — Stage 22 — `SIMILAR_DESCRIPTION`: the task/requirement text
shares enough distinctive terms with the job of an earlier application
(renamed or re-published vacancy; the matched terms are printed below the
finding) — check by hand. `INFO` lines name other roles at the same employer,
which are NOT duplicates; a near-identical text at the same employer (a
renamed posting) is raised to `POSSIBLE_DUPLICATE`. Applications without a
job row, and vacancies without a description, get no text comparison.

`record-submission` prints a finding whose only signal is
`SIMILAR_DESCRIPTION` as `ADVISORY` and does **not** refuse because of it;
every other STRONG/PROBABLE finding still needs `--confirm-distinct-from`.

## Record a manual submission

    python scripts/stage15_application_tracker.py --database <db> record-submission \
        --company "Firma GmbH" --role "Python Developer (m/w/d)" --source bundesagentur \
        --reference 12345-ABC-S --job-id 123 --channel EMPLOYER_PORTAL \
        --submitted-at 2026-10-21T09:15:00Z --employer-domain firma.de \
        --actor operator --evidence "Portal confirmation page; CV + Anschreiben PDFs" [--apply]

Records ONE submission the operator already made by hand, as SUBMITTED
(origin OPERATOR). Dry run without `--apply`. Idempotent on
`--source` + `--reference` (`ALREADY_RECORDED`, nothing written); the
event key is `submission-v2:` + SHA-256 of the JSON array
`[source, reference]` (records created before this keep their
`email-/portal-submission:` keys and are found by source + reference). A
STRONG or PROBABLE duplicate refuses (exit 3) until each verified-different
application id is passed with `--confirm-distinct-from <id>`. With
`--job-id`, a company/role/source/BA-reference mismatch with that job refuses
(exit 4). Empty or whitespace-padded `--source`/`--reference`, and naive,
invalid or future (later than now) `--submitted-at` values are refused
(exit 2). All checks run identically with and without `--apply`.

## Gmail dry run (default)

    python scripts/stage15_application_tracker.py --database <db> process-gmail --since 2026-10-17

Reads every configured applicant mailbox read-only (IMAP `readonly` +
`BODY.PEEK[]`; nothing is marked read): `--mailbox all` (default) =
`primary` (`GMAIL_*`) + `secondary` (`XING_MAILBOX_*`, the account that also
receives XING alerts and from which applications have been sent);
`--mailbox primary|secondary` restricts it. Per mailbox, stderr prints
`MAILBOX: <label> fetched=N skipped=N pages=N deadline_exceeded=..
error=<class> complete=..`. More than 500 messages in the window are read in
successive pages of 500 within the run (at most 10 pages), never the same
oldest 500 again. Exit 1 (`INCOMPLETE`) means a mailbox was not read
completely: fetch error, deadline, page limit or `skipped>0` (re-run later or
narrow `--since`). XING platform
mail (`mail.xing.com`) is ignored as `JOB_PLATFORM_NOTIFICATION`, and the same
Message-ID in both mailboxes is processed once. Prints, per message: correlation status/grade/
signals, classification + rule, `previous_status → new_status`, `action`
(TRANSITION / RECORD_ONLY / IGNORE), `duplicate`, `reason`. **Writes nothing.**
`--source stored` uses the already-synced `gmail_messages` table instead (no
network; primary account only).

## Apply inbound processing

    python scripts/stage15_application_tracker.py --database <db> process-gmail --since 2026-10-17 --apply

Each message is one transaction: event + status projection, keyed by the
message. Re-running is safe.

## Confirm a rejection or offer (operator only)

E-mail never sets REJECTED or OFFER. Such a message appears with
`operator_confirmation_required=True`, `proposed_status=REJECTED|OFFER` and
`reason=TERMINAL_EMAIL_REQUIRES_OPERATOR_CONFIRMATION`; the application keeps
its status and gets `followup_required=True`. Read the actual mail in Gmail,
then confirm it yourself:

    python scripts/stage15_application_tracker.py --database <db> transition 1 REJECTED --actor <you> --reason "Absage per E-Mail vom 2026-10-30, im Postfach geprüft" --occurred-at 2026-10-30T09:12:00+01:00 --apply

(`--occurred-at`, Stage 22: the real time of the event, e.g. the mail's Date; UTC offset required, not in the future, not before the current status. Default: now.)

Terminal statuses (OFFER, REJECTED, WITHDRAWN) always need `--actor` and a
non-empty `--reason` and are recorded with origin OPERATOR.

## Manually correct a state

Within the transition table (e.g. a phone invitation):

    python scripts/stage15_application_tracker.py --database <db> transition 1 INTERVIEW --actor <you> --reason "Phone call 2026-10-20" --apply

Outside the table (e.g. leave REJECTED after a misattributed mail):

    python scripts/stage15_application_tracker.py --database <db> override 1 RECEIVED --actor <you> --reason "Rejection was for another role" --apply

Without `--apply` both only print what would happen. An override never
deletes the wrong event; the history shows both.

## Inspect duplicate handling

- A re-processed message shows `duplicate=True`, `reason=ALREADY_PROCESSED`,
  `event_recorded=False`.
- `history <id>`: each Gmail-caused event has a distinct `correlation`
  (`gmail:…`). Two events with the same key are impossible
  (`UNIQUE(correlation_key)`).
- `AMBIGUOUS` (two applications equally likely, e.g. same employer and no
  role in the mail) writes nothing — resolve it with `transition`.

## Recover from a failed transaction

A failure inside a tracker call rolls back the event AND the status change
together; nothing partial remains. `CONCURRENT_CHANGE_RETRY` means another
writer changed the application first: simply re-run `process-gmail` (the
already-applied message is a duplicate, the other is re-evaluated against
the new status). If a database error persists, check `alembic current`
equals the head and that the database is writable.

## Verify that no outbound action occurred

- `history <id>`: origins are only PORTAL/IMPORT (bootstrap), GMAIL
  (inbound mail) and OPERATOR (your commands).
- `OUTBOUND_SENDING_ENABLED=false` in the environment; the Stage 15 code has
  no SMTP/HTTP/Telegram/draft imports (enforced by
  `tests/test_stage15_response_tracking.py::test_stage15_modules_import_no_outbound_capability`).
- Gmail: no new Sent or Drafts items; IMAP access is read-only.
- Tables written by Stage 15: `applications`, `application_events` only.

## First-time setup for the ALD pilot record

    python scripts/stage15_import_ald_application.py --database <db>                       # dry run
    python scripts/stage15_import_ald_application.py --database <db> --receipt-source imap --apply

Creates ALD Vacuum Technologies GmbH / Backend Developer (m/w/div.) /
20385-2347902-S at SUBMITTED (2026-10-17T14:02:50Z) and records RECEIVED
from the real receipt mail. Idempotent.
