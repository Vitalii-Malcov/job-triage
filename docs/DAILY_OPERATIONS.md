# Daily operations (v1, local Windows runtime)

The single page for running JobTriage day to day on the operator's own
machine. Deeper design docs are linked where relevant; when this page and an
older stage document disagree about *how to operate*, this page wins.

> **Nothing in this system submits an application or writes to an employer
> by itself.** Every real submission is done by the operator by hand (portal,
> e-mail, job board) and only *recorded* here afterwards.

## 1. Start the project

From a fresh PowerShell in `D:\AI_agent`:

```powershell
$env:DATABASE_URL = "sqlite:///D:/AI_agent/runtime/job_triage_runtime.db"
$env:ALEMBIC_AUTO_UPGRADE = "false"
$env:OUTBOUND_SENDING_ENABLED = "false"
$env:TELEGRAM_GMAIL_DRAFT_ENABLED = "false"
$env:TELEGRAM_OPERATOR_CONTROL_ENABLED = "true"
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

Checks: `GET http://127.0.0.1:8000/api/v1/health` → `{"status":"ok"}`,
`GET /api/v1/ready` → `{"status":"ready"}` (503 if the database is not at the
Alembic head — there is no auto-upgrade). The first log line
`startup_safety_state ...` lists every safety flag; confirm
`outbound_sending_enabled=false telegram_gmail_draft_enabled=false
alembic_auto_upgrade=false`. Every other endpoint needs the header
`X-API-Key: <API_KEY from .env>`.

The web process also starts the Telegram bot when `TELEGRAM_BOT_TOKEN` is
set (operator chat only). To start without the bot, set
`$env:TELEGRAM_BOT_TOKEN = ""` for that session.

## 2. Which database is production

`D:\AI_agent\runtime\job_triage_runtime.db` (SQLite, Alembic head
`e3a9c5d7f2b4`). Everything else in the repository root (`job_search.db`,
`stage11_pilot.db`, `stage14b_*.db`, `runtime/*pre-*.db`,
`runtime/stage16_pilot_scratch.db`) is historical, a pilot or a backup. The
tracker CLIs refuse the frozen `stage14b_canonical_v12.db` by path and hash.

## 3. Flags that keep it safe

| Flag | Safe value | Effect |
|---|---|---|
| `OUTBOUND_SENDING_ENABLED` | `false` (default) | Both send endpoints return 503 before any state is written; the SMTP provider refuses independently. |
| `TELEGRAM_GMAIL_DRAFT_ENABLED` | `false` (default) | No Gmail draft is ever created. |
| `ALEMBIC_AUTO_UPGRADE` | `false` | Schema changes only by an explicit `alembic upgrade head` on a backed-up DB. |
| `AUTOMATION_SCHEDULER_ENABLED` | `false` unless deliberately used | No background cycles. |
| `TELEGRAM_OPERATOR_CONTROL_ENABLED` | `true` is fine | APPROVE/SKIP are local decisions; no employer contact. |

**Stop all outbound immediately:** set `OUTBOUND_SENDING_ENABLED=false` (and
`TELEGRAM_GMAIL_DRAFT_ENABLED=false`) in `.env` *and* the shell, restart
uvicorn, check the `startup_safety_state` line. See also
[RUNBOOK.md → Emergency disabling of outbound send](RUNBOOK.md).

## 4. Collect jobs — supported sources today

| Source | How | Notes |
|---|---|---|
| Bundesagentur für Arbeit (Jobsuche API) | `POST /api/v1/collectors/bundesagentur/run` | Query from `BUNDESAGENTUR_SEARCH_KEYWORDS/LOCATION/RADIUS_KM`. Re-runs update existing rows by fingerprint (no duplicates). |
| XING job-alert e-mails | `POST /api/v1/collectors/xing/run` | Reads the `XING_MAILBOX_*` inbox read-only, only sender `jobs@mail.xing.com`. Tracking links are stored, **never opened**. |

```powershell
$h = @{ "X-API-Key" = "<API_KEY>" }
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/api/v1/collectors/bundesagentur/run
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/api/v1/collectors/xing/run
```

**Not supported (no collector exists):** LinkedIn, Indeed, StepStone,
Jobriver, company career pages. Vacancies found there by hand can still be
applied to and *tracked* (section 8).

## 5. Matching and shortlist

- Candidate profile (authoritative: runtime profile v4) —
  `GET /api/v1/candidate-profile`. Only facts the operator confirmed; the
  matcher never adds skills (e.g. Selenium is not in v4 and is reported
  MISSING when a posting requires it). German is B1 (B2 course ongoing — never
  claimed as a held level).
- Match one job: `POST /api/v1/jobs/{job_id}/match`, read it back with
  `GET /api/v1/jobs/{job_id}/match`. The matcher (v12) is a
  requirements/technology filter, **not a final ranking**; sparse postings
  (XING alerts) naturally get similar scores. Known conservative limitation:
  "Studium oder vergleichbare Qualifikation / Quereinsteiger willkommen" is
  still reported as an unmet education requirement — read the posting.
- Inspect: `GET /api/v1/jobs?limit=200`, `GET /api/v1/jobs/{id}`; Telegram
  digest/vacancy feed if enabled.

## 6. Before applying: duplicate check (mandatory)

One vacancy often exists several times (employer BA post, XING copy, agency
mirror). Always run, for the job row you intend to apply to:

```powershell
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database D:/AI_agent/runtime/job_triage_runtime.db check-job <job_id>
```

| Verdict | Exit | Meaning |
|---|---|---|
| `ALREADY_APPLIED` | 3 | STRONG: same job row, BA reference, URL, fingerprint, or same employer + role. Do not apply. |
| `POSSIBLE_DUPLICATE` | 0 | PROBABLE: identical full title at another company (typical anonymized agency mirror). Check by hand. |
| `NO_DUPLICATE_FOUND` | 0 | No deterministic signal. INFO lines list other roles at the same employer (a different role is a legitimate application). |

Limitation: a mirror that *rewrites* the title (e.g. "Backend Developer (all
genders) 25185" at a staffing agency for the ALD vacancy) is not detectable
deterministically — the operator's judgement remains the last guard.

## 7. Preparing a Bewerbung — where human approval is required

1. CV draft `POST /api/v1/jobs/{id}/cv-draft`, cover letter
   `POST /api/v1/jobs/{id}/bewerbung-draft` (only confirmed profile facts).
2. Review package `POST /api/v1/jobs/{id}/review-package` →
   **human review/approve** `POST /api/v1/review-packages/{id}/approve`
   (or Telegram APPROVE, Stage 16 — a local decision, never a submission).
3. Final documents (DOCX/PDF) are produced locally for the human; package
   folders live outside the repository
   (`C:\Users\BOSS\AppData\Local\job-triage-packages\`). Never reuse another
   application's folder.
4. **The operator submits by hand.** No code path submits to a portal.

Human approval points: review-package approval, Telegram APPROVE/SKIP
(decision only), response-draft decision + separate send, follow-up decision
+ separate send, and every real submission.

## 8. Record a submission (after sending it by hand)

```powershell
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database D:/AI_agent/runtime/job_triage_runtime.db `
  record-submission --company "Firma GmbH" --role "Python Developer (m/w/d)" `
  --source bundesagentur --reference 12345-ABC-S --job-id 123 --channel EMPLOYER_PORTAL `
  --submitted-at 2026-10-21T09:15:00Z --employer-domain firma.de `
  --actor operator --evidence "Portal confirmation 'Vielen Dank'; CV+Anschreiben PDFs"
```

Dry run by default; add `--apply` to write. `--source/--reference`
identify the vacancy (BA reference, StepStone id, ...) and make it
idempotent (`ALREADY_RECORDED`); both are stored exactly as typed, so an empty
or whitespace-padded value is refused (exit 2). `--submitted-at` must have an
offset and must not be later than now (exit 2). With `--job-id`, company,
role, source and BA reference must match that job (exit 4 otherwise; omit
`--job-id` for a submission without a collected job, e.g. StepStone). A
STRONG/PROBABLE duplicate refuses with exit 3 until you name each
verified-different application id with `--confirm-distinct-from <id>`. Pass `--employer-domain` whenever you know the
employer's mail domain — it is the strongest signal for matching replies.

## 9. Employer receipts and replies

```powershell
# dry run (reads mail, writes nothing)
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database D:/AI_agent/runtime/job_triage_runtime.db process-gmail --since 2026-10-15
# apply
... process-gmail --since 2026-10-15 --apply
```

**Monitored mailboxes** (`--mailbox all`, the default; read-only IMAP, nothing
marked read):

| Label | Credentials | Used for |
|---|---|---|
| `primary` | `GMAIL_*` | main application mailbox |
| `secondary` | `XING_MAILBOX_*` | also used to send applications (e-mail, StepStone account) and receives XING alerts |

stderr shows `MAILBOX: <label> fetched=.. skipped=.. pages=.. error=..
complete=..`. A mailbox with more than 500 messages in the window is read in
pages of 500 (up to 10 per run), so later replies are always reached. Exit 1
(`INCOMPLETE`) means at least one mailbox was not read completely — fetch
error, deadline, page limit, or messages left unread (`skipped>0`); what was
read is still processed. Re-run later or narrow `--since` (everything is
idempotent).
XING platform mail is ignored (`JOB_PLATFORM_NOTIFICATION`); the same
Message-ID in both mailboxes is processed once.

What e-mail may do: RECEIVED / UNDER_REVIEW / INTERVIEW / TECHNICAL_INTERVIEW
at MEDIUM+ correlation. Rejections and offers are only *reported*
(`operator_confirmation_required`) — confirm them yourself after reading the
mail:

```powershell
... transition <id> REJECTED --actor operator --reason "Absage vom 2026-11-02, im Postfach geprüft" --apply
```

A receipt that the processor leaves LOW/AMBIGUOUS (generic text, shared ATS
sender such as join.com, two applications at one employer) changes nothing;
record it by hand with `transition <id> RECEIVED ... --apply` once you have
read it. Full details: [APPLICATION_TRACKER_RUNBOOK.md](APPLICATION_TRACKER_RUNBOOK.md).

Status overview: `... list`, `... show <id>`, `... history <id>`.

## 10. Follow-ups

Each SUBMITTED application gets `next_followup_at` (+14 days), visible in
`list`/`show`. Nothing follows up automatically. The Stage 7E follow-up
draft path (`/follow-ups/...`) needs an explicit decision **and** a separate
send request, and the send is refused while `OUTBOUND_SENDING_ENABLED=false`.
In v1 follow-ups are written and sent by the operator by hand.

## 11. Backup and restore of the runtime DB

Backup (safe while the app runs; uses SQLite's online backup API):

```powershell
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
.\.venv\Scripts\python.exe -c "import sqlite3,sys;s=sqlite3.connect('file:D:/AI_agent/runtime/job_triage_runtime.db?mode=ro',uri=True);d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close();print(d)" "C:\Users\BOSS\AppData\Local\job-triage-backups\job_triage_runtime.$ts.db"
```

Verify a backup: `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check`
empty, `SELECT * FROM alembic_version` = head.

Restore: stop uvicorn (and any CLI), keep the current file as
`job_triage_runtime.broken-<ts>.db`, copy the backup to
`D:\AI_agent\runtime\job_triage_runtime.db`, run the verification above,
start again. Backups and DB files are never committed (`*.db` is ignored).

## 12. Tests

```powershell
.\.venv\Scripts\ruff.exe check app scripts tests alembic
.\.venv\Scripts\ruff.exe format --check app scripts tests alembic
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\Users\BOSS\AppData\Local\Temp\pt
```

The full suite takes ~35-40 minutes sequentially; on a memory-tight machine
run it in batches of files. Tests never touch the runtime DB or the network.

## 13. Intentionally NOT automated

- Submitting an application anywhere (portal forms, job boards, e-mail).
- Sending any e-mail while `OUTBOUND_SENDING_ENABLED=false`; with it true,
  only an approved draft via an explicit send request.
- Opening XING (or any) tracking links from e-mails.
- Setting REJECTED / OFFER / WITHDRAWN from e-mail.
- Collecting from LinkedIn, Indeed, StepStone or career pages.
- Inventing or upgrading candidate facts (skills, language levels).
