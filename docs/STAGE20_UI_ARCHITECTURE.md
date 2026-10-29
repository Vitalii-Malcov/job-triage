# Stage 20 — Operator UI architecture

Status: foundation (branch `stage20/operator-dashboard-foundation`, not merged).

## Problem

JobTriage's backend is complete enough for daily use, but every daily question
("what is new and worth applying to?", "what did employers answer?", "what is
due today?") needs a CLI invocation and knowledge of its flags. Stage 20 adds a
browser view of the same data.

## Decision

**Server-rendered HTML inside the existing FastAPI app** (`app/ui/`), Jinja2
templates with autoescaping, one static stylesheet, **no JavaScript**.

Rejected alternatives:

- *React/Node SPA* — a second toolchain, a build step, a JSON API to design
  and secure, for pages that are tables and cards. No interaction here needs
  client-side state.
- *Hand-built HTML strings with `html.escape`* — every interpolation is a
  place to forget escaping. Jinja2 autoescape makes escaping the default and
  the unescaped path (`|safe`) greppable; the templates never use it.

New dependency: `jinja2` (BSD-3, Pallets, pure Python plus `markupsafe`;
already Starlette's documented templating engine). Nothing else is added:
form bodies (only the login form) are parsed with `urllib.parse`, so
`python-multipart` is not needed.

## Boundaries (what the UI may and may not do)

1. **Read-only.** Every page reads through `app/ui/readonly.py`: a separate
   engine whose connections are read-only below SQLAlchemy's transaction
   handling, so a rollback cannot undo it (Codex plan review, P1): SQLite runs
   `PRAGMA query_only = ON` on the raw DBAPI connection at connect time;
   PostgreSQL gets the libpq startup option
   `-c default_transaction_read_only=on`, which every transaction on that
   connection inherits. On top of that, the session refuses any flush with
   pending ORM changes and is rolled back at the end of every request. A write
   attempt anywhere in the call graph (ORM or Core DML) raises instead of
   committing; tests check this again after a rollback. There are no UI routes that change jobs, applications, events,
   the candidate profile or configuration.
2. **No outbound actions.** No e-mail, Gmail draft, Telegram message, portal
   or network request is made by any UI route. The operator inbox runs the
   Stage 15 response processor with `apply=False` over **already stored**
   messages (the same path as `stage15_application_tracker.py process-gmail
   --source stored`); IMAP is never contacted.
3. **No parallel business logic.** Fit tier, decision and criteria come from
   `app.services.job_fit_assessment.assess_job`; duplicates from
   `app.services.application_guard`; lifecycle vocabulary from
   `app.domain.application_lifecycle`; follow-up dates from the tracker's
   persisted `next_followup_at` (Stage 15 rule: 14 days after the last
   employer contact / submission while waiting). The UI only groups and
   displays.
4. **Honest unknowns.** A metric that cannot be computed is shown as
   `UNKNOWN` (with the reason) or `NOT_CONFIGURED`, never as `0`.
5. **Off by default.** `UI_ENABLED=false` (default) makes every `/ui` route a
   404. Independent of every other feature flag.

## Authentication

The API uses an `X-API-Key` header, which a browser does not send. The UI
adds a login form that takes the same `API_KEY`:

- `POST /ui/login` compares the submitted key with `API_KEY` in constant time
  (rate-limited like the API) and sets a session cookie.
- The cookie is **stateless**: `<expiry>.<HMAC-SHA256(API_KEY, "jobtriage-ui/1|" + expiry)>`.
  It contains no key material, expires after `UI_SESSION_HOURS` (default 12),
  and every cookie becomes invalid when `API_KEY` is rotated. No server-side
  session table (no new state).
- Cookie flags: `HttpOnly`, `SameSite=Strict`, `Path=/ui`, `Secure` unless
  `UI_COOKIE_SECURE=false` (needed only for plain-HTTP access from a
  non-localhost host).
- `POST /ui/login` and `POST /ui/logout` reject a cross-origin `Origin`
  header (login-CSRF): the Origin's host must equal the `Host` header. The
  scheme is not compared, because behind the TLS-terminating proxy
  (`docs/DEPLOYMENT.md`) the browser sends `https` while Uvicorn sees `http`.
  If the proxy rewrites `Host`, set `UI_PUBLIC_ORIGIN` to the exact browser
  origin. All other routes are `GET` and side-effect free.
- The login body is read with a 4 KiB limit (declared length and stream),
  and a cookie that is not exactly `<digits>.<64 hex>` is treated as "not
  logged in".
- An empty `API_KEY` refuses every login (same fail-closed rule as the API).

## Response headers

Every UI response carries `Content-Security-Policy: default-src 'none';
style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none';
base-uri 'none'` (no scripts at all), `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin` (not `no-referrer`: under that policy
browsers send `Origin: null` on the login form POST, which the Origin check
refuses; found in the real-browser check) and `Cache-Control: no-store`.

External vacancy links are rendered as links only for `http`/`https` URLs,
with `rel="noopener noreferrer nofollow"`; anything else is shown as text.
The UI never fetches those URLs (CLAUDE.md collector rule — XING tracking
redirects).

## Scale

`assess_job` costs ~17 ms per vacancy (measured on a synthetic database), so
assessing all ~2 900 stored vacancies per request (~50 s) is not acceptable.

- **Vacancy list:** SQL filters (search text, city, source, recommendation)
  and keyset pagination (`id` descending, `before_id` cursor, 25 per page)
  run in the database; only the rows of the page are assessed.
- **Computed filters** (fit tier, already applied / not applied,
  remote / hybrid) need the assessment, so they scan SQL-ordered rows with a
  **scan budget** (200 assessments per request). The page states how many
  rows were scanned and offers a "continue scanning" cursor; it never claims
  the result is complete when the budget ran out.
- **Dashboard fit-tier counts** cover vacancies first seen in the last 14
  days. If that window holds more than the budget, the counts are `UNKNOWN`
  with the reason.

## Pages

| Route | Content | Source |
|---|---|---|
| `/ui/` | dashboard counts | `app/ui/queries.py::dashboard_summary` |
| `/ui/vacancies` | filtered, paginated table | `list_vacancies` |
| `/ui/vacancies/{id}` | description, criteria, evidence, duplicates, route | `vacancy_detail` |
| `/ui/applications` | applications (read-only) | `ApplicationTracker.list_applications` |
| `/ui/applications/{id}` | application + event history | `ApplicationTracker.get_application_detail` |
| `/ui/inbox` | operator review queue | `ApplicationResponseProcessor.process(apply=False)` |
| `/ui/follow-ups` | follow-up center | `ApplicationRecord.next_followup_at` + lifecycle |

The inbox keeps four things apart per message: the **classification** (rule +
confidence), the **correlation** to an application (status, confidence,
signals), the **proposed action** (what the processor would do) and the
**confirmed action** (whether an event for this message is already recorded).
Nothing on the page changes a status; decisions stay with the CLI / Telegram
operator control.

Follow-up buckets are computed in the operator's local time zone
(`TELEGRAM_DAILY_DIGEST_TIMEZONE`, default `Europe/Berlin`): a follow-up stored
as `2026-10-25T23:30Z` is due on 26 October in Berlin.

## Running locally

One command (details, cookie note and the before/after database check:
`docs/DAILY_OPERATIONS.md` section 1a):

```
.\.venv\Scripts\python.exe scripts\run_operator_ui.py --database <sqlite file>
# open http://127.0.0.1:8000/ui/
```

The launcher binds to `127.0.0.1` only and turns the Telegram bot, migrations
and every sending flag off for its server process. Exposing the UI beyond
localhost needs TLS in front of it (and keeps `UI_COOKIE_SECURE=true`).

## Not in this stage

- Any write action from the browser (status changes, approvals, commute
  checks). Those need a separate confirmation design (CSRF tokens, explicit
  confirm step, audit actor) and stay on CLI/Telegram for now.
- A persisted fit-tier cache.
- Any UI route that renders or writes documents (see below: PDF export is a
  local service, not a browser action).

## PDF export (Stage 20, block B)

`app/services/pdf_document.py` renders the same confirmed-fact blocks as the
DOCX export (`build_cv_document`: trusted CV draft facts + confirmed
`ApplicationIdentity`; `letter_blocks`: the reviewed cover letter) as A4 PDF.
Selected with `export_package_documents(..., document_format="pdf")`; DOCX
remains the default.

- Standard PDF fonts Helvetica / Helvetica-Bold (not embedded) with
  WinAnsiEncoding; exact line wrapping from the Adobe Core 14 AFM widths
  (`app/services/pdf_font_metrics.py`, notice retained). No new dependency.
- A character outside cp1252 is **refused** (`UnrenderableTextError`), never
  replaced or transliterated; nothing is written in that case.
- Every PDF is re-read with pypdf (the extraction `presubmit_check.py` uses)
  and refused (`PdfVerificationError`) unless its text equals the blocks'
  text ignoring whitespace.
- Deterministic bytes (no dates, no document ID, no compression), so the
  `_manifest.json` SHA-256 binding stays stable.
- The cover letter now starts with a sender block (name, address, phone,
  e-mail) from the confirmed identity, only when the CV's contact checks
  pass -- the pre-submit check requires the contact data in the letter.
- Nothing is sent or uploaded; `presubmit_check.py` and the operator's
  look at every PDF stay mandatory. A generated package passes every
  content check of `validate_package` (READY in the tests, which pass no
  profile version).
- Binding (`presubmit_check.py --bind`): the PDF export also writes
  `_quelle_html/_cv.html` / `_letter.html` -- the exact text of the blocks,
  escaped, never over an existing file -- naming the SHA-256 of that text
  (whitespace removed, the comparison `render_pdf` verifies). `--bind`
  binds such a PDF only when its extracted text has exactly that hash; a
  wrong, edited or unreadable PDF is listed as `NOT BOUND` and stays
  `*_NOT_BOUND` -> `NEEDS_OPERATOR`. Unbound packages are `NEEDS_OPERATOR`;
  `--bind` stays the explicit step after the operator looked at the PDFs,
  and after it the profile-version, PDF-changed and source-changed checks
  of Stage 19 apply unchanged (Stage 19 HTML sources without the marker
  bind as before). Checked with the real CLI on a synthetic package:
  `NEEDS_OPERATOR` (exit 3) before, `READY` (exit 0) after `--bind`.
- Visual check (Stage 20 release): PDFium-rendered pages of a synthetic CV,
  letter, 3-page CV and a long-line stress document stay inside the 62 pt
  margins, with umlauts, `ß`, `€` and German quotes rendered correctly. The
  letter has no recipient address or date block (not in the confirmed data).
