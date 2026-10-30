# Daily operations (v1, local Windows runtime)

The single page for running JobTriage day to day on the operator's own
machine. Deeper design docs are linked where relevant; when this page and an
older stage document disagree about *how to operate*, this page wins.

> **Nothing in this system submits an application or writes to an employer
> by itself.** Every real submission is done by the operator by hand (portal,
> e-mail, job board) and only *recorded* here afterwards.

## 1. Start the project

From a fresh PowerShell in the project folder (on the current laptop `E:\Projects\job-triage`):

```powershell
$env:DATABASE_URL = "sqlite:///./runtime/job_triage_runtime.db"
$env:DATABASE_ROLE = "production"
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

## 1a. Operator UI in the browser (Stage 20, read-only)

One command, from the project folder in PowerShell (the API key is read from
`.env` or `$env:API_KEY` as usual; it is never written into a script):

```powershell
.\.venv\Scripts\python.exe scripts\run_operator_ui.py `
    --database runtime/job_triage_runtime.db `
    --snapshot "$env:USERPROFILE\JobTriage-Backups"
```

Then open <http://127.0.0.1:8000/ui/> and log in with `API_KEY`. Pages:
dashboard, vacancies (search, city, source, fit tier, applied, work mode),
vacancy detail (fit tier, criteria, evidence, duplicates), Bewerbungen with
history, operator inbox (mail waiting for your decision), follow-ups.
**Ctrl+C** stops the server.

What the launcher does (`scripts/run_operator_ui.py`):

- binds to `127.0.0.1` only, sets `UI_ENABLED=true`, and for its own server
  process turns **off** the Telegram bot, Alembic auto-upgrade, the
  scheduler and every send / draft / operator-control flag -- nothing in
  `.env` is changed;
- refuses a database path that does not exist (SQLite would create an empty
  one), a database failing `PRAGMA quick_check`, and a missing `API_KEY`;
- `--snapshot DIR` copies the database first with SQLite's backup API;
- refuses to start if the configuration would still point the server at
  another database (`POSTGRES_HOST` is forced empty for that reason);
- prints a read-only fingerprint (integrity, rows per table, SHA-256 of the
  logical content, which includes committed changes still in the WAL) before
  start and after stop and ends with `UNCHANGED` (exit 0) or
  `DATABASE CHANGED` (exit 5, with the tables that changed); exit 3 means
  the server itself failed (e.g. the port was in use). A change means
  another process (scheduler, tracker CLI) wrote meanwhile -- the UI pages
  open the database read-only (`PRAGMA query_only`) and cannot write.

Notes:

- If the normal web process (section 1) already runs on port 8000, add
  `--port 8001` and open <http://127.0.0.1:8001/ui/>.
- The session cookie is `Secure` by default. Chrome and Edge accept it on
  `http://127.0.0.1` (checked with Chrome 156). If another browser keeps
  returning to the login page after a correct key, set
  `$env:UI_COOKIE_SECURE = "false"` for that shell only -- never for a UI
  reachable from another machine.
- Never start the UI with `--host 0.0.0.0` or behind a public port; there is
  no such option in the launcher on purpose. Remote access needs TLS in front
  (`docs/STAGE20_UI_ARCHITECTURE.md`).
- The UI decides nothing: confirming inbox mail, status changes and
  approvals stay with `stage15_application_tracker.py` and Telegram operator
  control.

### 1b. Vacancy assessment cache (Stage 21)

Once filled, the dashboard's fit-tier counts (14-day window and all stored
vacancies) and the fit-tier / applied / mode filters read
`job_fit_assessments` instead of assessing at most 200 vacancies per
request. Only up-to-date rows count; the dashboard shows how many are
`stale` or `not assessed`. Before the first fill (or on a database without
the table) the UI behaves exactly as in Stage 20.

```powershell
# dry run: counts only, opens the database read-only
.\.venv\Scripts\python.exe scripts\stage21_assess_vacancies.py --database runtime/job_triage_runtime.db
# write (production needs a backup snapshot first; ~17 ms per vacancy)
.\.venv\Scripts\python.exe scripts\stage21_assess_vacancies.py --database runtime/job_triage_runtime.db `
  --apply --snapshot "$env:USERPROFILE\JobTriage-Backups"
```

Re-run it after a collection run or a profile/search-configuration change;
it only assesses vacancies whose cached row is missing or stale, and Ctrl+C
keeps every finished batch of 50. `--only-stale` refreshes existing stale
rows without assessing new vacancies. The script needs the database at the
current Alembic head (`alembic upgrade head`, with approval and a backup for
production).
The full production order (preflight, backup, code + migration together,
backfill, smoke tests, rollback) is `docs/STAGE21_VACANCY_CACHE_PLAN.md` §9.
Until the cache is filled the dashboard says so explicitly.

## 2. Which database is production

`runtime\job_triage_runtime.db` in the project folder (SQLite, Alembic head
`b6c1e9a4d2f7`). Everything else in the repository root (`job_search.db`,
`stage11_pilot.db`, `stage14b_*.db`, `runtime/*pre-*.db`,
`runtime/stage16_pilot_scratch.db`) is historical, a pilot or a backup. The
tracker CLIs refuse the frozen `stage14b_canonical_v12.db` by path and hash.

**Explicit selection (Stage 17A).** The web process and the scheduler
classify `DATABASE_URL` at startup (`app/core/database_target.py`) and
refuse to start on a mismatch with `DATABASE_ROLE`:

| `DATABASE_ROLE` | Accepted databases |
|---|---|
| `production` | `runtime/job_triage_runtime.db`, PostgreSQL |
| `pilot` | pilot files only (`stage*_*.db` in the project root or `runtime/`) |
| `test` / `development` | any other SQLite file, in-memory, PostgreSQL |
| unset | production runtime, other SQLite, PostgreSQL -- **never a pilot** |

A backup (`*.pre-*`, `job-triage-backups\`) is refused for every role. So a
shell that forgot the `DATABASE_URL` override no longer starts silently on
`.env`'s pilot database (`stage11_pilot.db`, kept for the Stage 14A/13B
pilot tools, which take an explicit `--database-url`); it stops with a
message naming the database kind. `.env` itself is not rewritten. The test
suite (`tests/conftest.py`) always runs on a temporary database with
`DATABASE_ROLE=test`.

**CLI tools too (night audit 2026-10-27).** Every `scripts/stage15_*`,
`stage16_*` and `stage17_*` tool takes its database explicitly
(`--database` / `--database-url`) and applies the same roles: a write
(`--apply` / `--live`) to a backup is refused, a write to a pilot needs
`DATABASE_ROLE=pilot`, and under `DATABASE_ROLE=test` or `development` the
production runtime database is refused even for reading. Read-only commands
may still inspect a backup.

Safe production start (PowerShell, one shell, nothing written to `.env`):

```powershell
$env:DATABASE_URL  = "sqlite:///./runtime/job_triage_runtime.db"
$env:DATABASE_ROLE = "production"
# CLI: always name the database explicitly; dry run first, --apply second
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database runtime/job_triage_runtime.db list
```

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
| Bundesagentur für Arbeit (Jobsuche API) | `POST /api/v1/collectors/bundesagentur/run` | Keywords from `BUNDESAGENTUR_SEARCH_KEYWORDS`. With a stored candidate search configuration (section 5a) the place and radius come from it: the home postal code + city with a 150 km straight-line radius (a superset of everything within 100 km by car) plus a nationwide home-office search (Stage 22 P1: postings with the BA flag `homeofficemoeglich`; `arbeitszeit=ho` silently returned nothing), once per configured search term. Every BA row also keeps the BA's workplace coordinates, region, home-office flags and `quereinstiegGeeignet`. Without one, `BUNDESAGENTUR_SEARCH_LOCATION/RADIUS_KM` as before. Re-runs update existing rows by fingerprint (source, company, title, URL) -- the same BA posting seen under two URLs is still two rows (the backfill in 4a reports them). |
| XING job-alert e-mails | `POST /api/v1/collectors/xing/run` | Reads the `XING_MAILBOX_*` inbox read-only, only sender `jobs@mail.xing.com`. Tracking links are stored, **never opened**. |

```powershell
$h = @{ "X-API-Key" = "<API_KEY>" }
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/api/v1/collectors/bundesagentur/run
Invoke-RestMethod -Method Post -Headers $h http://127.0.0.1:8000/api/v1/collectors/xing/run
```

**Not supported (no collector exists):** LinkedIn, Indeed, StepStone,
Jobriver, company career pages. Vacancies found there by hand can still be
applied to and *tracked* (section 8).

### 4a. BA reference and liveness (Stage 22 P0-1 / P0-2)

Every Bundesagentur row keeps the BA's own `referenznummer` in
`jobs.source_reference` (also when the posting links to the employer's own
site, and for references with `_`, `.` or `/`). Rows collected before
Stage 22 are filled once from their BA URL (rows with an employer URL are
filled by the next collection run):

```powershell
# dry run: counts, plus references stored on more than one row (reported, not merged)
.\.venv\Scripts\python.exe scripts\backfill_source_reference.py --database runtime/job_triage_runtime.db
# write (production needs a verified backup snapshot; never overwrites a stored reference)
.\.venv\Scripts\python.exe scripts\backfill_source_reference.py --database runtime/job_triage_runtime.db `
  --apply --snapshot "$env:USERPROFILE\JobTriage-Backups"
```

Is a posting still online? Read-only, one request per second, nothing is
written to the database:

```powershell
.\.venv\Scripts\python.exe scripts\ba_liveness.py --database runtime/job_triage_runtime.db --job-id 144 --plan
.\.venv\Scripts\python.exe scripts\ba_liveness.py --database runtime/job_triage_runtime.db --limit 50 `
  --output "$env:USERPROFILE\JobTriage-Backups\liveness.jsonl"
```

States: `ACTIVE`, `CLOSED` (404 for a full, valid BA reference only),
`INVALID_REFERENCE` (no or truncated reference -- never requested),
`SOURCE_ERROR` (429/5xx/timeout/network/rejected key -- says nothing about
the posting; a re-run with the same `--output` retries only these),
`UNKNOWN`. A CLOSED result is a hint to check by hand, not a status change.

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

## 5a. Candidate search configuration and fit criteria (Stage 17)

The search constraints live in the runtime database, not in anyone's
memory: home place and commute origin, the **car-route** commute limit,
relocation regions, the remote-work rule, salary range, earliest start date,
target seniority, search terms, primary/secondary roles, languages, skills
and the confirmed contact data.

```powershell
$db = "runtime/job_triage_runtime.db"
.\.venv\Scripts\python.exe scripts\stage17_candidate_config.py --database $db show
.\.venv\Scripts\python.exe scripts\stage17_candidate_config.py --database $db history
# change it: edit the operator file (outside the repo), dry run, then apply
.\.venv\Scripts\python.exe scripts\stage17_candidate_config.py --database $db apply `
  --config $env:LOCALAPPDATA\job-triage-config\candidate_config_2026-10-27.json `
  --actor operator --reason "..."            # add --expected-version N --apply
```

Every write is versioned and recorded in the append-only
`candidate_profile_revisions`; a value from a less authoritative source
(an old CV) never replaces a confirmed one; skills/languages are never
removed. Details: [STAGE_17_CANDIDATE_SEARCH_CONFIG.md](STAGE_17_CANDIDATE_SEARCH_CONFIG.md).

Per vacancy (read-only, independent criteria, no overall score):

```powershell
.\.venv\Scripts\python.exe scripts\stage17_job_fit.py --database $db assess <job_id>
# store a car route you measured, or let OSRM compute it from coordinates
.\.venv\Scripts\python.exe scripts\stage17_job_fit.py --database $db record-commute <job_id> `
  --car-km 85 --destination "Darmstadt, ..." --region Hessen --source-note "Google Maps 27.10." --apply
.\.venv\Scripts\python.exe scripts\stage17_job_fit.py --database $db check-commute <job_id> `
  --dest-lat 49.87 --dest-lon 8.65 --destination "Darmstadt, ..." --apply
```

Location is `PASS`/`FAIL` only from a stored car route; a known workplace
without one is `DISTANCE_UNVERIFIED`, an unknown one `UNKNOWN`. The decision
is `BLOCKED`, `NEEDS_VERIFICATION` or `CANDIDATE` -- triage, never an
instruction to apply.

**Fit tier (Stage 19)** -- the line `FIT TIER:` (and `fit_tier` /
`tier_reasons` in `--json`), on top of the unchanged decision, with the
reasons and the posting's evidence:

| Tier | Meaning | Next step |
|---|---|---|
| `NOT_SUITABLE` | a blocker, an explicitly stated mandatory German level 2+ above your *confirmed* level, or a senior/architect title with an unmet experience requirement | skip it |
| `NEEDS_OPERATOR` | something to check first: distance not measured, possible duplicate, skills not evaluated, inferred or unconfirmed language gap, years asked without a senior title | check the listed point, then re-run `assess` |
| `PARTIAL_MATCH` | fits, but weaker: partial skills, other role, salary below the expectation, preferred German level, mixed seniority | decide yourself |
| `GOOD_MATCH` | strong skills, fitting role, language and route, entry level | prepare the package (section 7) |

The tier never blocks anything and is never an instruction to apply.

## 6. Before applying: duplicate check (mandatory)

One vacancy often exists several times (employer BA post, XING copy, agency
mirror). Always run, for the job row you intend to apply to:

```powershell
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database runtime/job_triage_runtime.db check-job <job_id>
```

| Verdict | Exit | Meaning |
|---|---|---|
| `ALREADY_APPLIED` | 3 | STRONG: same job row, BA reference, URL, fingerprint, or same employer + role. Do not apply. |
| `POSSIBLE_DUPLICATE` | 0 | PROBABLE: identical full title at another company (typical anonymized agency mirror), an agency posting in the same city whose title shares the distinctive words of an applied role, or (Stage 22) `SIMILAR_DESCRIPTION`: the task/requirement text matches the job of an earlier application although title and reference differ (renamed / re-published vacancy, e.g. the ALD "Backend Developer" re-posted by N.O.C, Weber and FERCHAU). Check by hand; advisory only, never a block. |
| `NO_DUPLICATE_FOUND` | 0 | No deterministic signal. INFO lines list other roles at the same employer (a different role is a legitimate application). |

The second line, `CLASSIFICATION:`, grades how sure the match is:
`EXACT_DUPLICATE` (job row, reference, URL or fingerprint identical),
`PROBABLE_DUPLICATE` (same employer and role, no identifier proof -- still
blocked, verify), `POSSIBLE_DUPLICATE`, `NOT_DUPLICATE`, or
`INSUFFICIENT_EVIDENCE` (the row has no usable company/title, e.g. a broken
alert parse). Employer names are compared with umlauts, "e.G."/"eG", dotted
abbreviations and "Standort ..." folded.

**Enforced (Stage 17C):** creating a Bewerbung draft or a review package
for a vacancy whose STRONG duplicate was already SENT (SUBMITTED or later)
is refused (HTTP 409 / `AlreadyAppliedError`) on every path -- API,
automation shortlist and Telegram. An application still being prepared
(PACKAGE_READY/APPROVED) is not refused.

Limitation: since Stage 22 a mirror that rewrites the title or hides the
employer is found when its task/requirement text matches the job of an
earlier application (`SIMILAR_DESCRIPTION`, advisory). It stays undetectable
when either side has no usable description (XING rows, applications without a
job row, very short postings) or when the text itself was rewritten beyond the
shared distinctive terms — the operator's judgement remains the last guard.

## 7. Preparing a Bewerbung — where human approval is required

1. CV draft `POST /api/v1/jobs/{id}/cv-draft`, cover letter
   `POST /api/v1/jobs/{id}/bewerbung-draft` (only confirmed profile facts).
2. Review package `POST /api/v1/jobs/{id}/review-package` →
   **human review/approve** `POST /api/v1/review-packages/{id}/approve`
   (or Telegram APPROVE, Stage 16 — a local decision, never a submission).
3. Final documents (DOCX/PDF) are produced locally for the human -- the code
   produces the structured CV/Bewerbung drafts (every statement bound to a
   confirmed profile fact), not the PDF itself; package
   folders live outside the repository
   (`%LOCALAPPDATA%\job-triage-packages\`). Never reuse another
   application's folder.
4. **Pre-submit check (mandatory, §7a):** `scripts/presubmit_check.py` on the
   package and the exact upload copies, after uploading and before the final
   click. `READY` (exit 0) may go on to submission with the operator's
   approval; `NEEDS_OPERATOR` (exit 3) only after the operator has seen and
   approved every WARN; `BLOCKED` (exit 4) and a refused run (exit 2) never
   go out. This applies to a browser agent filling the form exactly
   as to a human (`CLAUDE.md`, "Browser submission gate").
5. **The operator submits by hand.** No code path submits to a portal.

Human approval points: review-package approval, Telegram APPROVE/SKIP
(decision only), response-draft decision + separate send, follow-up decision
+ separate send, and every real submission.

### 7a. Pre-submit check (mandatory before the final click)

Every package uses the same file names (`..._Lebenslauf.pdf`), so a file
from another vacancy can end up in a form (it happened on StepStone). Check
the package and the exact copy you are about to upload:

```powershell
.\.venv\Scripts\python.exe scripts\presubmit_check.py --database runtime/job_triage_runtime.db `
  --package "$env:LOCALAPPDATA\job-triage-packages\<day>_LiveHunt\ApplicationPrep\Final\<pkg>" `
  --company "<employer>" --role "<exact title>" `
  --attach cv=.playwright-mcp/upload/<pkg>/<file>.pdf [--attach letter=...] [--no-letter] `
  [--missing-field "Zeugnisnote"]
```

| Exit | Outcome | Meaning |
|---|---|---|
| 0 | `READY` | PDFs complete and within page limits; the PDFs' own text (pypdf) has the profile's contacts, the letter names company and role, no over-claimed German; the upload copy is byte-identical to this package's original. Still not an instruction to submit. |
| 3 | `NEEDS_OPERATOR` | something could not be verified automatically (PDF text unreadable, no `--attach`, PDF identical to another package's, profile version not recorded, no letter field). |
| 4 | `BLOCKED` | wrong package's file, changed copy, wrong contact data, over-claim, stale profile version, or a required portal field the candidate has no confirmed value for. |
| 2 | refused | the check could not run (missing package, bad `--attach`, database refused). Treat like `BLOCKED`. |

Right after rendering a package (and looking at the PDFs), bind each PDF to
its source text once: same command with `--bind` (writes `_manifest.json`
into the package: SHA-256 of PDF and source + profile version; the
documents are not touched); a PDF or source changed after binding is
`BLOCKED`. PDFs from the Stage 20 export (`document_format="pdf"`) come with
their sources in `_quelle_html/`; `--bind` binds such a PDF only if its text
is exactly the exported text, otherwise it prints `NOT BOUND: cv -- ...` and
the package stays `NEEDS_OPERATOR`.

Read-only otherwise; contact values are compared, never printed.

**Portal lessons (27.10.2026).**
- A portal form may require more than the posting (one employer portal: "Zeugnisnote"
  and "Letzter Notenauszug" for a DevOps role). Never invent a value: pass
  the field as `--missing-field` (BLOCKED) and ask the candidate.
- CHECK-IN bank application portal: "Vorschau" ends the form session. Do not
  re-submit the browser's "Confirm Form Resubmission"; resume with
  "Bewerbung fortsetzen" and the access data the portal issued (keep it out
  of reports), re-check the fields and uploads, then "Abschicken".
- StepStone keeps a per-application copy of the CV; "Lebenslauf ersetzen"
  swaps the main CV without deleting the sent copies.
- Upload copies live only in `.playwright-mcp\upload` (never committed);
  delete them after the submission once their SHA-256 matched the original.

## 8. Record a submission (after sending it by hand)

```powershell
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database runtime/job_triage_runtime.db `
  record-submission --company "Firma GmbH" --role "Python Developer (m/w/d)" `
  --source bundesagentur --reference 12345-ABC-S --job-id 123 --channel EMPLOYER_PORTAL `
  --submitted-at 2026-10-21T09:15:00Z --employer-domain firma.de `
  --actor operator --evidence "Portal confirmation 'Vielen Dank'; CV+Anschreiben PDFs"
```

Put the §7a pre-submit outcome (`presubmit READY`, or the operator's
approval of each WARN) into `--evidence`.

With `--job-id` and no `--location`, the job's location is stored (needed by
the same-city agency-mirror duplicate check).

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
.\.venv\Scripts\python.exe scripts\stage15_application_tracker.py --database runtime/job_triage_runtime.db process-gmail --since 2026-10-15
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

A message that reads like a lifecycle event but may not or could not be
applied carries `operator_review=NEEDS_OPERATOR_REVIEW`: a rejection/offer to
confirm, or a receipt/rejection/invitation whose correlation is LOW or
AMBIGUOUS (generic text, two applications at one employer). Such LOW or
AMBIGUOUS mail changes nothing and records nothing; read it and record it
by hand (`transition <id> RECEIVED|REJECTED ... --apply`). A reply from the
same ATS address as an earlier correlated mail (e.g.
`<company>+applicantreply-<token>@...`) is shown as evidence (SENDER) and
names the likely application, but a correlation that needs it never writes
anything: it is reported as `SENDER_ONLY_CORRELATION_NEEDS_OPERATOR`. Full details: [APPLICATION_TRACKER_RUNBOOK.md](APPLICATION_TRACKER_RUNBOOK.md).

The text output ends with **`NEEDS YOUR DECISION (n)`**: every flagged mail
with its application and the next allowed step (the `transition` command
to dry-run, or "read the mail" when it names no vacancy). Stage 19: a mail
that names no employer or role (e.g. only "Wir laden Sie gerne zu einem
Vorstellungsgespräch ein.") but comes from a sender that already wrote about
one of your applications is no longer dropped: it is classified, flagged
(`UNMATCHED_FROM_KNOWN_SENDER`) and lists `review_candidates`; nothing is
written.

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
.\.venv\Scripts\python.exe -c "import sqlite3,sys;s=sqlite3.connect('file:runtime/job_triage_runtime.db?mode=ro',uri=True);d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close();print(d)" "$env:USERPROFILE\JobTriage-Backups\job_triage_runtime.$ts.db"
```

Verify a backup: `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check`
empty, `SELECT * FROM alembic_version` = head.

Restore: stop uvicorn (and any CLI), keep the current file as
`job_triage_runtime.broken-<ts>.db`, copy the backup to
`runtime\job_triage_runtime.db` in the project folder, run the verification above,
start again. Backups and DB files are never committed (`*.db` is ignored).

## 12. Tests

```powershell
.\.venv\Scripts\ruff.exe check app scripts tests alembic
.\.venv\Scripts\ruff.exe format --check app scripts tests alembic
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp $env:LOCALAPPDATA\Temp\pt
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
