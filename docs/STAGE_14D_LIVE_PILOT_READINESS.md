# Stage 14D — Live-pilot readiness (system blockers)

Status: implemented on `feat/stage-14d-live-pilot-readiness` (from the frozen Stage 14B matcher
`499aaa5`, matcher `v12` unchanged). Nothing in Stage 14D sends, uploads, submits or contacts
anyone; `OUTBOUND_SENDING_ENABLED` stays false.

Stage 14C's canonical dry run found five product/system blockers. This stage fixes exactly those.
The operator blockers (portal verification, location conflict, freshness, fit, human approval)
are deliberately **not** resolved by code and stay BLOCKED / REVIEW.

| # | Blocker (14C) | Fix |
|---|---|---|
| 1 | Runtime `DATABASE_URL` still pointed at `stage11_pilot.db` | Explicit canonical runtime (`app/core/canonical_runtime.py`, `scripts/stage14d_canonical_runtime.py`) |
| 2 | No CV/attachment file | Local DOCX export + attachment plan (`app/agents/cv_document.py`, `app/services/document_export.py`) |
| 3 | Contact data not modeled | `ApplicationIdentity` contact layer, separate from the Candidate Profile (`app/models/application_identity.py`) |
| 4 | Research artifact bound to pilot job id 27; CLI traceback on canonical job 1 | Stable `VacancyIdentity` binding + typed, clean CLI failures |
| 5 | Letter cited Bulgarian B2, omitted German B1 | Deterministic language relevance (`app/agents/language_relevance.py`), Bewerbung generator `v4` |

## 1. Canonical runtime

The normal `.env` is never rewritten. The canonical database is selected per process through
environment overrides (which pydantic-settings ranks above `.env`):

- `DATABASE_URL=sqlite:///<absolute canonical path>`
- `POSTGRES_HOST=""` (the PostgreSQL parts builder can never replace the target)
- every flag in `startup_diagnostics.SAFETY_FLAGS` = `false` (outbound sending, Alembic
  auto-upgrade, Telegram feed/Bewerbung/approval/Gmail draft, automation, digest, auto research)

```
python scripts/stage14d_canonical_runtime.py preflight \
  --database D:/AI_agent/stage14b_canonical_v12.db \
  --expect-sha256 874d2cab6fc78efe3574dd1fb123c06e60662da48c32c240227313778f41e35d
```

READ-ONLY (SQLite `mode=ro` URI + `PRAGMA query_only`; SHA-256 compared before/after). Reports
DATABASE TARGET, OUTBOUND STATE, ALEMBIC STATE, MATCHER VERSION and MIGRATION HEAD. Exit 0 PASS,
1 FAIL, 2 refused (missing file, relative/UNC/mapped-drive path, bad digest syntax).

For `preflight`, `--expect-sha256` is optional (diagnostic only: without it the report shows the
file's digest but cannot prove it is the approved one).

```
python scripts/stage14d_canonical_runtime.py serve \
  --database D:/AI_agent/stage14b_canonical_v12.db \
  --expect-sha256 874d2cab6fc78efe3574dd1fb123c06e60662da48c32c240227313778f41e35d
```

**A database path alone is NOT authorization to serve.** Another database at the same migration
head (the audit `stage14b_working.db`, the pilot `stage11_pilot.db`) passes every other check, so
`serve` REQUIRES `--expect-sha256` with the independently approved canonical digest (never derived
from the selected file) and refuses without it (exit 2). Only after path validation, digest match,
migration-head check and safety-flag check does it start the web app on `127.0.0.1` with those
overrides; any mismatch exits nonzero without starting the server. A running app writes to its
database as normal operation (the digest then changes, and a later `serve` needs a newly approved
digest); use `preflight` or a scratch copy whenever the canonical file must stay byte-identical.

## 2. Application contact data

`ApplicationIdentity` is **not** part of the Candidate Profile (profile semantics, the transfer
digest and the matcher are untouched). Fields: `full_name`, `email`, `phone`, optional `street`,
`postal_code`, `city`, `country`, plus `confirmed_by: "OPERATOR"` and `confirmed_at` — only a human
statement makes it usable. Values are validated (e-mail shape, phone digits, no control
characters, no blank strings); `repr` is masked and errors name fields only.

Keep it in a LOCAL, untracked file (`*.local.json` is git-ignored), for example
`application_identity.local.json`:

```json
{
  "full_name": "<exactly the trusted first + last name of the profile>",
  "email": "<application e-mail>",
  "phone": "<phone>",
  "city": "<only if you want it on the CV>",
  "confirmed_by": "OPERATOR",
  "confirmed_at": "<ISO timestamp>"
}
```

Nothing is guessed: no file ⇒ `CONTACT_DATA_MISSING`. E-mail and phone are required; the name must
equal the profile's trusted first + last name (`CONTACT_NAME_MISMATCH`,
`CONTACT_PROFILE_NAME_UNTRUSTED`). The send-readiness gate has a new required check
`APPLICATION_CONTACT_DATA_COMPLETE`; the CLI takes `--identity-json`.

## 3. CV / letter files and the attachment plan

- `build_cv_document` (pure) renders the pinned CV draft as a German CV: headings Profil,
  Kenntnisse, Berufserfahrung, Projekte, Ausbildung, Zertifikate, Sprachen in the draft's own
  `section_order`. Only draft facts (already trust-filtered): missing dates stay missing ("aktuell"
  for a current job without a start date), unfinished education is "nicht abgeschlossen", UNKNOWN
  language level is "Niveau nicht angegeben", free text is copied verbatim (not translated).
  Without complete contact data there are no contact lines and the document reports the problem.
- `render_docx` writes a minimal WordprocessingML package with the standard library only —
  byte-deterministic (fixed member order and timestamps). **PDF is a follow-up**: the project has
  no PDF library and adding one was out of scope.
- Filenames: `Lebenslauf_<Vorname>_<Nachname>_<Firma>_<Referenz>.docx` and
  `Anschreiben_...docx`; umlauts transliterated, everything else outside `[A-Za-z0-9]` collapsed.
- `write_document` refuses an existing file (`DocumentExistsError`); only an explicitly flagged
  dry-run directory inside the system temp directory may be rewritten.
- `build_attachment_plan` lists the files (role, name, media type, size, SHA-256) and the blockers:
  document warnings (e.g. `CONTACT_DATA_MISSING`), `APPROVAL_PENDING` unless the package is
  APPROVED, then any failing send-readiness checks. `delivery` is always `NOT_SENT`; the plan is a
  hand-off description for a human, never executed.

## 4. Stable vacancy identity for research artifacts

A numeric job id is database-local (pilot job 27 = canonical job 1). `VacancyIdentity`
(`source` + `source_reference` and/or `fingerprint`) binds long-lived research evidence; every
given field must match (`source_reference` against `extract_reference_tokens(title, url)`, the
same derivation as `job_reference_tokens`). `ApplicationDestination.job_id` is now an optional
legacy hint, used only when no `vacancy` is given; `LocationEvidence` gained the same optional
`vacancy`. The committed E362/B artifacts in `docs/research/` now carry
`{"source": "bundesagentur", "source_reference": "20079-GR6IFL6L-S", "fingerprint": "72d52b51…"}`
and resolve on canonical job 1 without any copy or edit.

## 5. Readiness CLI failures

Expected failures no longer raise a traceback:

| Situation | Output | Exit |
|---|---|---|
| destination / location evidence names another vacancy | `BLOCKED DESTINATION_VACANCY_MISMATCH` / `BLOCKED LOCATION_EVIDENCE_VACANCY_MISMATCH` | 1 |
| no candidate profile | `BLOCKED NO_CANDIDATE_PROFILE` | 1 |
| unreadable / invalid JSON input | `refused: INPUT_INVALID: <file/fields>` (never values) | 2 |
| readiness not met | normal `READY_FOR_SEND_PACKAGE = NO` report | 1 |

Unexpected programmer errors still raise.

## 6. Language relevance in the Bewerbung (generator v4)

A language is cited only when the vacancy gives a concrete reason, in this order: (1) the pinned
match evaluated it against a language requirement, (2) the ad names it ("Deutschkenntnisse",
"English", "deutschsprachig"; not the country "Deutschland"), (3) the ad is written in it
(function-word count, at least 15 and three times the other language). A verified but unrelated
language is not inserted; UNKNOWN level is never claimed. E362/B (a German ad naming no language):
German B1 is cited, Bulgarian B2 is not. `BEWERBUNG_GENERATOR_VERSION` `v3 → v4`, so older
packages are not current (package currency, by design).

## Still blocked for a controlled live pilot (operator work, not code)

1. Employer portal: `UNVERIFIED` until an operator confirms it in a browser (no submission).
2. Location: stored Frankfurt am Main vs employer Hannover/Münster — operator review.
3. Freshness: `last_seen` 2026-09-15 — needs a live check.
4. Fit: candidate match 31/100 — operator go/no-go.
5. Approval: package stays `PENDING_REVIEW` until a human approves.
6. Contact data: an operator-confirmed `ApplicationIdentity` file must be supplied.
