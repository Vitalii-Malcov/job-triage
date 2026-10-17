# Application Tracker Runbook (Stage 15)

All commands are local. None of them sends mail, creates drafts, posts to a
portal or notifies anyone. Use `--json` for machine-readable output.

`<db>` is a SQLite file **already at the Alembic head** (`--database`), or a
PostgreSQL URL (`--database-url`). The tool refuses the protected Stage 14B
canonical database, Stage 13D paths and `ai_agent-db-1`, and any database
not at head (upgrade explicitly; there is no auto-upgrade).

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

## Gmail dry run (default)

    python scripts/stage15_application_tracker.py --database <db> process-gmail --since 2026-10-17

Reads the configured mailbox read-only (IMAP `readonly` + `BODY.PEEK[]`;
nothing is marked read). Prints, per message: correlation status/grade/
signals, classification + rule, `previous_status → new_status`, `action`
(TRANSITION / RECORD_ONLY / IGNORE), `duplicate`, `reason`. **Writes nothing.**
`--source stored` uses the already-synced `gmail_messages` table instead (no
network).

## Apply inbound processing

    python scripts/stage15_application_tracker.py --database <db> process-gmail --since 2026-10-17 --apply

Each message is one transaction: event + status projection, keyed by the
message. Re-running is safe.

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
