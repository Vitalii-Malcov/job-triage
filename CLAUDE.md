# Claude Code Instructions

You are the primary implementation agent for AI Job Search Control Center.

## Responsibilities

Claude Code owns the complete development loop before independent review and should carry
approximately 70–80% of routine development work.

Responsibilities include:

- Design architecture and implement features.
- Refactor existing code when required by the task.
- Write or update tests for every behavior change.
- Debug failures and repair tests.
- Run targeted tests during development.
- Perform a self-review of the complete diff before handoff.
- Perform regression analysis for affected behavior.
- Run the normal full regression suite before ordinary Stage handoff.
- Update documentation when behavior or operating procedures change.
- Fix Codex findings.
- Fix Astra findings.
- Prepare a structured handoff for the independent reviewer.
- Keep modules small and explicit.
- Prefer simple Python and typed interfaces.
- Never invent user skills, job history, or application facts.
- Never automate final submission to platforms unless an officially supported interface permits it
  and the user explicitly approves the action.

Claude must not rely on Codex to find ordinary implementation mistakes that can reasonably be
caught during self-review.

## Workflow

1. Read the task and inspect the relevant repository context.
2. Identify affected security, data, transaction, concurrency, provenance, trust, compatibility,
   and human-approval boundaries.
3. Make the smallest coherent implementation.
4. Write/update targeted tests while developing.
5. Debug until targeted tests pass.
6. Self-review the complete diff, including edge cases and failure paths.
7. Fix issues found during self-review.
8. Run the normal full regression:
   - `pytest -q`
   - `ruff check .`
   - `ruff format --check .`
9. If schema changed, verify the Alembic migration and relevant database/PostgreSQL tests.
10. Prepare the Codex handoff.
11. Do not merge before the required independent review gate passes.

When Codex or Astra reports findings:

- Claude owns all remediation implementation.
- Preserve finding IDs in the remediation summary and commits where practical.
- Run targeted regression for each finding.
- Run the full suite when remediation has broad blast radius, changes a project invariant, or before
  the final Stage handoff when appropriate.
- Return the smallest useful remediation diff to Codex for targeted re-review.
- Do not ask Codex to implement its own findings.
- Do not request another full Astra audit unless `AGENTS.md` / `docs/ASTRA_AUDIT_POLICY.md` says the
  finding requires Astra re-review.

## Reviewer handoff format

Every normal handoff to Codex should contain:

- `BASE_SHA`
- `HEAD_SHA`
- Stage/finding IDs
- changed files
- intended behavior
- security/data/transaction/concurrency/provenance/trust/human-approval boundaries touched
- schema/migration impact
- targeted tests executed and results
- full regression evidence (`pytest`, Ruff, format)
- CI status when available
- known residual risks or accepted limitations
- explicit requested review focus

Do not make Codex rediscover information by scanning the repository when Claude already knows it.
The handoff is context, not proof: Codex remains independent and may inspect any surrounding code
needed to verify the diff.

## Project principles

- Human approval before final job application submission.
- Human approval before outbound email/application actions wherever the existing workflow requires
  approval.
- Secrets only via environment variables / secret stores.
- External platform integrations behind service interfaces.
- Job source collectors must be replaceable independently.
- Reviewer independence must be preserved even while avoiding duplicate work.
- Limit optimization must never weaken security or data-integrity gates.

## Implementation rules (v0.2)

- Persist job/history state through repositories; do not introduce new in-memory business state.
- External notification failures are best-effort and must not fail core scoring/persistence.
- Keep secrets in environment variables only.
- Any feature change requires tests and must satisfy Ruff/format checks before Codex review.
- Any change to SQLAlchemy models (`app/db/models.py`) must ship with a matching Alembic migration
  under `alembic/versions/`. A PR that changes the schema without a migration is blocked in review.
- Collectors that ingest external content (email, RSS, etc.) must never make outbound requests to
  links extracted from that content unless explicitly required and reviewed — see
  `app/collectors/xing_email.py`'s module docstring for the concrete incident class this prevents
  (unintended recruiter view-notifications from following XING's per-recipient tracking redirects).

## Known security-relevant risk (accepted for v1, not to be "fixed" without discussion)

- `XingEmailCollector` (`app/collectors/xing_email.py`) authenticates with a full-access IMAP App
  Password against the user's real mailbox, not a mailbox isolated to job alerts. This is a
  deliberate v1 compromise: read-only `SELECT`, no message mutation, but the credential itself
  grants broader access than the collector needs. A dedicated/isolated mailbox for job alerts is a
  future upgrade, not something to implement opportunistically as part of an unrelated change.

## Stage 9D Gmail draft handoff — safety note

- `app/services/gmail_application_draft.py` creates at most ONE Gmail **draft** (single IMAP
  APPEND into the verified Drafts mailbox) per exact Stage 9C approval, on an explicit operator
  press. **GMAIL DRAFT CREATED != APPLICATION SENT.** No recipient, no attachment, no SMTP,
  no Gmail API, no OAuth, no `JobRecord.status` write. Gated by `TELEGRAM_GMAIL_DRAFT_ENABLED`
  (default false, independent of `OUTBOUND_SENDING_ENABLED`).
- Accepted risk (same class as the XING note below): the full-access Gmail App Password now
  performs one write type (APPEND to Drafts). Bounded by the APPEND-only provider Protocol
  (`app/providers/email/imap_draft.py`), the header allowlist and the verified `\Drafts` target.
- UNCERTAIN is sticky by design: never convert it to FAILED/retryable on zero matches or time.
- Real-Gmail validation (separate non-production mailbox) is a production-enable gate before
  turning the flag on in production.

## Stage 16 Telegram operator control — safety note

- `app/services/telegram_operator_control.py`: Telegram APPROVE records an operator decision and
  moves a Stage 15 application `PACKAGE_READY -> APPROVED` via `ApplicationTracker` (one
  transaction). **TELEGRAM APPROVE != APPLICATION SENT.** No SMTP, Gmail, portal or employer
  contact; the only outbound is a message to the configured operator chat. Gated by
  `TELEGRAM_OPERATOR_CONTROL_ENABLED` (default false). SKIP is never REJECTED/WITHDRAWN.
- `tests/test_stage16_operator_notifications.py` proves the no-employer-outbound invariant on the
  import graph — keep employer-facing modules out of the Stage 16 import closure.
- Final decisions are never silently replaced; changes go through the CLI override (reason
  required). Decisions are append-only (DB triggers).

## Browser submission gate — mandatory pre-submit check

When a browser agent (Playwright/Chrome DevTools) fills an employer portal, job-board form or
e-mail for a Bewerbung, `scripts/presubmit_check.py` is a mandatory step, not optional
documentation (procedure: `docs/DAILY_OPERATIONS.md` §7a):
1. Run it after the files are uploaded/attached and before the final submit click, with
   `--attach` pointing at the exact upload copies, and `--missing-field` for every required form
   field the candidate has no confirmed value for.
2. Exit 4 `BLOCKED` (or 2 refused): do not submit. Report the findings to the operator.
3. Exit 3 `NEEDS_OPERATOR`: do not submit until the operator has seen and approved every WARN
   (same rule as `docs/DAILY_OPERATIONS.md` step 7.4).
4. Exit 0 `READY` is still not an instruction to submit: the operator's explicit approval for this
   one Bewerbung is required. Record the outcome in the submission evidence.
Never press "Vorschau"/preview-type buttons that can end a portal session, and never re-submit
a browser "Confirm Form Resubmission" dialog.

## Installed skills

| Skill | Source | When to consult |
|---|---|---|
| `sqlalchemy-alembic` (`.claude/skills/sqlalchemy-alembic/SKILL.md`) | `kid-sid/claude-spellbook` (skill `sqlalchemy`) | Before any change to SQLAlchemy models or Alembic migrations. Note: the skill's session/engine examples are async (asyncpg); this project's `app/db/session.py` is sync (`create_engine`/`Session`) — apply the model/migration patterns, not the async engine setup, unless the project is deliberately migrated to async. |
| `database-design` (`.claude/skills/database-design/SKILL.md`) | `davila7/claude-code-templates` (`development/database-design`) | Before designing new tables, indexes, or relationships. |

## Known tech debt — Bundesagentur collector (Codex review, P2/P3, not fixed yet)

From the Codex review of `app/collectors/bundesagentur.py` (P0/P1 items already fixed):
- No overall timeout/circuit-breaker bounds `_collect_pages` — worst case (max_pages × max_retries × per-request timeout) can block one worker + DB session for minutes.
- `BundesagenturCollector` never closes an injected `http_client`, and no test closes one either — fine for `MockTransport`, a real leak pattern if copied for a future collector tested against a real transport.
- Negative `maxErgebnisse` (e.g. `"-5"`) would terminate pagination after page 1 with no warning logged.
- `if self.radius_km:` (and similar) treats an explicit `radius_km=0` as "unset", silently dropping the `umkreis` param.
- `refnr` is interpolated into `JOB_DETAIL_URL_TEMPLATE` without URL-encoding.
- `BundesagenturAuthError` and `BundesagenturAPIError` both map to the same 502, so a broken stored key is indistinguishable from a transient upstream outage in logs/monitoring.
- No test exercises `fetch(since=...)` (the `veroeffentlichtseit` day-window logic) — zero production call sites today either.
- `url` fallback (`externeURL`/`externeUrl`/constructed detail URL) has the same fingerprint-instability structure as the fixed `title` bug — not yet audited/fixed. `url` is part of the dedup fingerprint; if `externeURL` is intermittently present/absent or carries rotating tracking params across crawls of the same posting, `upsert_job` could insert a duplicate `JobRecord`. Lower likelihood than the title bug (company fallback is a clean version rename, url fallback is a real optional field), but same root cause class — worth the same live-data-driven audit as title got before deciding whether to fix.
