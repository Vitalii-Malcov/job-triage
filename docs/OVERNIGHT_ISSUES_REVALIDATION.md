# Overnight issues revalidation — main @ 5834fe7 (2026-11-11)

Sources found and read: `issues-index.md` (89-row audit, `Downloads/`), `docs/AUDIT_2026_11_06_REMEDIATION_MATRIX.md`,
the Hard Code Review (`main @ 78a947b`, 8 findings), `docs/STAGE62_ISSUE_INVENTORY.md`, Stage 64/65 reports,
GitHub Issues (none exist), TODO/FIXME in `app/`, `scripts/`, `alembic/` (none). **Not available:** the full audit
report `job-triage-audit.md` and its `findings/*.md` (only the index and the matrix summarise them).

Method: "Verified today" = I read the current code on main (file reference given). "Per matrix" = status carried
from the matrix and **not re-verified** in this run; it is stated that way rather than assumed closed.

## A. Hard Code Review (8 findings)

| ID | Source | Problem | Sev | Status | Evidence (verified today) | Fix |
|---|---|---|---|---|---|---|
| HCR-1.1 | Hard Review | API key compare not constant-time; failed auth unlimited | High | FIXED | `app/security/auth.py`: `hmac.compare_digest`, `begin_auth_attempt` before compare, 401/429 | PR #37 |
| HCR-1.2 | Hard Review | API key / bot token not masked in `repr(settings)` | High | FIXED | `app/core/config.py:97,145,218` are `SecretStr` | earlier stage |
| HCR-1.3 | Hard Review | Response-draft approval does not pin recipient | Med | FIXED | `pinned_recipient` in `app/db/models/response_drafts.py:227` and `follow_ups.py:192` | PR #30 |
| HCR-1.4 | Hard Review | UI sessions cannot be revoked | Low | FIXED | `app/ui/auth.py` docstring: logout revokes session id; rotating API_KEY/UI_SESSION_SECRET revokes all | stage 2x |
| HCR-2.1 | Hard Review | `processed_email_messages` unique key ≠ lookup key | Med | FIXED | `uq_processed_email_messages_source_message_id` in `app/db/models/email_collectors.py:31` | PR #30 |
| HCR-2.2 | Hard Review | XING dedup merges distinct postings | Med | NEEDS_OPERATOR | not re-verified; known XING stubs carry no reference id (STAGE63 report) | — |
| HCR-2.3 | Hard Review | Manual collector runs bypass lease | Med | PARTIALLY_FIXED (per matrix/Stage 6x) | lease code present in `collector_runner.py`; not re-tested here | — |
| HCR-2.4 | Hard Review | Async endpoints block loop on sync DB | Med | PARTIALLY_FIXED | `jobs.py:48` uses `run_off_loop`; UI `assess_job` is budgeted (`app/ui/queries.py:60`) | PR #43/#45 |

## B. Audit index, items re-checked in code today

| ID | Problem | Sev | Status | Evidence | Fix |
|---|---|---|---|---|---|
| SEC-04 | Telegram link previews (Telegram fetches XING tracking URLs → recruiter view-notification risk) | P2 | **OPEN → FIXED in this run** | `app/services/telegram.py:135` had no preview control | `link_preview_options.is_disabled=true` on the httpx helper AND `Defaults(link_preview_options=…)` on the SDK application (`/job`, `reply_text`, `send_message`, `edit_message_text`); tests in `tests/test_overnight_telegram_link_preview.py` |
| SEC-05 | `/docs`, `/openapi.json` without key | P3 | FIXED | `app/main.py:46-48` gated by `API_DOCS_ENABLED`; STAGE62 S62-02 | Stage 62 |
| SEC-02/03 | Template key accepted; budget blocks right key | P1 | FIXED | `auth.py` `is_strong_secret` path; Settings rejects templates | PR #37 |
| API-01/02/03 | id ≥ 2^63, regex, input bounds | P1 | FIXED/PARTIAL | `app/security/id_bounds.py`; `tests/test_audit_input_bounds.py` | PR #36 |
| FLOW-08 | No NFC normalisation | P3 | FIXED | `unicodedata.normalize("NFC"…)` in `application_response_classifier.py:141,156` | — |
| ARC-01 | Bot/endpoints block loop | P2 | PARTIALLY_FIXED | `run_off_loop` in `jobs.py`; other routers not checked | — |
| ARC-06 | SQLite without WAL | P3 | NEEDS_OPERATOR | no `journal_mode` in `app/db/session*.py`; enabling WAL rewrites the production DB header → operator decision | — |
| ARC-09 | Engine created at import | P3 | OPEN | `app/db/session.py:40` module-level `create_engine` | — |
| CQ-04 | No type checker | P3 | OPEN | no mypy/pyright in `pyproject.toml` / CI | — |
| DEP-03 | No Dependabot / pip-audit | P3 | **OPEN → PARTIALLY FIXED** | none existed | `.github/dependabot.yml` added; `pip-audit` not added (needs network job; not run) |
| DOC-01 | README contradicts main | P2 | **OPEN → FIXED** | status table stopped at Stage 24 and said remediation was local-only | README status rows updated to PRs #30–#61 |
| DOC-05 | No CONTRIBUTING/LICENSE | P3 | **PARTIALLY FIXED** | `CONTRIBUTING.md` added; **LICENSE not added — choice of license is the owner's decision** | NEEDS_OPERATOR |
| PERF-03 | No `last_seen_at` index | P3 | OPEN | `app/db/models/jobs.py:84` no index; no migration | needs migration; low value |
| OPS-05 | CI permissions/timeouts | P3 | FIXED | `ci.yml`: `permissions: contents: read`, timeouts | PR #39 |
| FLOW-14 | Shortlist gate not wired | P3 | FIXED | Stage 62 gate; `docs/STAGE62_*` | PR #58 |

## C. Remaining audit rows (status carried from `AUDIT_2026_11_06_REMEDIATION_MATRIX.md`, **not re-verified today**)

- FIXED_AND_TESTED per matrix: SEC-01, SEC-02, SEC-03, API-01, API-02, ARC-02, FLOW-05, FLOW-12, FLOW-13, OPS-01, OPS-08.
- PARTIALLY_FIXED per matrix: API-03, ARC-03, DB-01, DB-02, DEP-01, DOC-02, FLOW-01, FLOW-02, FLOW-04, PERF-02, TEST-01, TEST-06, OPS-05.
- Still OPEN per matrix (P1 remainder): ARC-07/OPS-02 (no supervision/alerts), OPS-03 (deploy/rollback not automated), UI-01 (partly addressed by the budget, see above), FLOW-15.
- Still OPEN per matrix (P2/P3, no safe small fix today): API-04, API-05/06/07/08/09/10/11, ARC-04/05/08, CQ-01/02/05, DB-03/04/05/06, DEP-02, DOC-03/04, FLOW-06/07/09/10/11, OPS-04/06/07, PERF-01/04, RUN-01/02/03, SEC-06/07, TEST-02/03/04/05/07/08/09/12, UI-02/03/04, AI-01.
  Several were touched by Stage 58–65 (e.g. API-04 by `APPLICATION_IDEMPOTENCY_AUDIT.md`, SEC-06 by Stage 16/65) — classified here as `NOT_RE-VERIFIED`, not closed.

Stage 62 inventory (S62-03 weak production API key, S62-08 28 BA reference conflicts, S62-09 second backup) — all NEEDS_OPERATOR, unchanged.

## D. Counts
- Hard-review findings: 8 → 5 fixed, 2 partial (2.3, 2.4), 1 needs-operator (2.2).
- Audit index rows: 89 (+4 style items out of scope). Re-verified in code today: 18 distinct IDs in section B (section C lists some of them again; superseded by `FINAL_ISSUES_REGISTER.md`). Carried per matrix: the rest (see the final register for exact counts).
- Fixed in this run: SEC-04, DOC-01, DEP-03 (partial), DOC-05 (partial). No P0/P1 defects found open that are fixable without the operator.
