# Security / Trust Model — JobTriage

This document explains what the application actually enforces in code, what it assumes the deployment environment will handle, and what remains future hardening. Cross-references `docs/PRODUCTION_READINESS_AUDIT.md` (AUD-IDs) for evidence.

## API authentication

**Implemented:** every protected route requires `X-API-Key` matching the configured `API_KEY` (`app/security/auth.py::require_api_key`). An unset `API_KEY` fails **closed** — every request is rejected, never defaults to "no auth" (`app/core/config.py`). `/health` is intentionally unauthenticated (it reveals nothing sensitive).

**Deployment responsibility:** TLS termination (the API key travels in a header — must not be sent over plaintext HTTP in any real deployment); rotating the key (see `docs/RUNBOOK.md`).

**Future hardening:** the comparison uses `!=` rather than `hmac.compare_digest` — a theoretical timing side-channel (AUD-006). Low real-world severity (one static key, network jitter dominates) but a standard hardening fix, deferred to reviewed follow-up.

## Rate limiting

**Implemented:** 14 independent in-memory sliding-window limiters (`app/security/rate_limit.py`), each with its own lock, covering distinct endpoint families with distinct limits (collector runs, drafting, sends, etc.) — not one shared global budget.

**Deployment responsibility:** this in-memory design is only meaningful under a **single FastAPI worker process** (`--workers 1`) — see `docs/DEPLOYMENT.md`. It is the deployment's job to not scale `web` horizontally until a shared store replaces it.

**Known limitations (not fixed in this session, security-relevant, reserved for Codex review):**
- No `X-Forwarded-For` awareness — behind a reverse proxy, all clients collapse to one bucket (AUD-003).
- Not multi-worker safe — effective limit multiplies by worker count if ever scaled (AUD-004).
- Per-host bucket dictionaries grow unbounded for the life of the process (AUD-005) — low priority for a private, low-traffic deployment.

## Trusted vs. untrusted job data

**Implemented, code-enforced (not just convention):** `TRUSTED_JOB_SOURCES = frozenset({"bundesagentur"})` (`app/services/response_draft.py`). Only job facts from a structured, authenticated API source may be interpolated into generated content. XING-sourced job facts — parsed from unauthenticated inbound email — are treated identically to "no matched job" (a placeholder + `missing_fields` entry) for generation purposes, never as usable real data, regardless of what the email actually contains.

## Email content trust

**Implemented:** inbound email content (XING digest, Gmail replies) is never passed to a generator as free text. The only email-derived signal that reaches generation is subject/body used for **language detection** (`detect_language`) — selecting a DE/EN template set, never contributing text to the output itself. `app/collectors/xing_email.py` additionally has a **hard, code-level constraint** against ever fetching a URL found in email content (zero HTTP client imports in that module) — this specifically prevents the collector from triggering XING's per-recipient tracking redirects, which would leak "this recipient opened/followed this link" back to XING/the sender.

## Candidate fact provenance

**Implemented:** every top-level candidate profile field carries a `field_trust`/`SourceType` classification (`FACT` / `INFERENCE` / `IMPORTED` / `UNKNOWN`). Generation only consumes fields that pass `is_top_level_fact_usable_for_generation` — an inferred or unconfirmed fact is never silently promoted to "stated fact" in generated text. A candidate's full name, for example, is only used if *both* first and last name independently pass this check from one consistent read of the profile (race-condition-safe, see `_derive_candidate_profile_facts`).

## Generated draft boundaries

**Implemented:** `app/agents/response_draft_generator.py` and `app/agents/follow_up_generator.py` are pure, deterministic, template-based generators — no LLM call, no network access, no database access. Every generated fact is traceable to a specific input the caller explicitly passed in (already validated/trusted by the time it arrives). `ResponseDraftRecord.requires_human_review` is always `True` — generation only ever *proposes*.

## Approval boundaries

**Implemented, enforced in code:** `send_response_draft`/`send_follow_up` (`app/services/response_draft_send.py`, `app/services/follow_up_send.py`) hard-require a persisted `*ApprovalRecord` with `decision == "APPROVED"` before any provider `send()` call — checked in the function itself, not merely gated by a UI convention that could be bypassed by calling the API directly. `ReviewPackageService` never auto-approves candidate-profile changes.

## Outbound email boundaries

**Implemented:** exactly two code paths can call `smtplib` transitively — `response_draft_send.py` and `follow_up_send.py`, both funneling through `app/providers/email/smtp.py`. No other module in the codebase calls it (verified via import/call-site scan). Duplicate-send prevention is unique-constraint-backed; ambiguous provider outcomes get a terminal `UNCERTAIN` state that is never auto-retried.

## External provider boundaries

**Implemented:** Gmail/XING IMAP access is **read-only** — never marks messages read, never deletes/moves/mutates mailbox state (verified: no `store`/`expunge`/`copy` IMAP commands issued anywhere in the sync path). Company Research (`app/services/company_research.py`) makes **zero** outbound network requests in v1 — a prior website-fetch sub-feature was removed after a Codex review identified a DNS-rebinding TOCTOU flaw in its SSRF guard, rather than attempting to harden it further; this is documented as a deliberate, permanent v1 scope decision, not a temporary gap.

## Secrets

**Implemented:** every credential (`API_KEY`, `BUNDESAGENTUR_API_KEY`, `XING_MAILBOX_APP_PASSWORD`, `GMAIL_APP_PASSWORD`, `TELEGRAM_BOT_TOKEN`) is a `pydantic-settings` field read exclusively from environment variables / `.env` — no hardcoded defaults anywhere. Several fail closed (503) rather than silently running unauthenticated/unconfigured when blank. Credentials are never logged: every IMAP/SMTP/Telegram failure log line uses `type(exc).__name__` only, never the exception message or raw response (which could embed a credential or token, e.g. an HTTP URL containing a Telegram bot token). `httpx`/`httpcore` INFO-level logging is deliberately suppressed specifically because it would otherwise print the Telegram bot token embedded in request URLs.

**Deployment responsibility:** actually keeping `.env` out of version control (already `.gitignore`d) and out of image layers (`.dockerignore` excludes it); injecting real values via the orchestrator's secret mechanism in a more mature deployment than a flat `.env` file.

## DB persistence

**Implemented:** schema is Alembic-versioned (never `create_all()` in production); every side-effecting operation persists through a repository, no ad hoc cross-request in-memory state. Idempotency/uniqueness constraints prevent duplicate inserts for the same logical action across process restarts.

**Deployment responsibility:** backups (`docs/RUNBOOK.md`), connection security (TLS to PostgreSQL in a real deployment — not configured by default, add via `DATABASE_URL` sslmode or network isolation).

## Known, pre-accepted limitation: XING mailbox credential scope

`XingEmailCollector` authenticates with a full-access IMAP App Password against the user's real mailbox, not a mailbox isolated to job alerts. This is a deliberate v1 compromise, already documented in this project's `CLAUDE.md`: read-only `SELECT`, no message mutation — but the credential itself grants broader access than the collector uses. **Not to be "fixed" opportunistically** — a dedicated/isolated mailbox is future work requiring its own reviewed change.

## Summary: implemented vs. deployment responsibility vs. future hardening

| Protection | Implemented (code-enforced) | Deployment responsibility | Future hardening (deferred) |
|---|---|---|---|
| Auth | Fail-closed API key check | TLS termination, key rotation | Constant-time comparison (AUD-006) |
| Rate limiting | Per-endpoint sliding-window limits | Single-worker deployment, private network | Proxy-aware IP, shared store for multi-worker (AUD-003/004/005) |
| Job/candidate trust | Source-based trust gating, provenance-based fact gating | — | — |
| Outbound email | Approval-gated, idempotent, dedicated send paths only | — | — |
| Secrets | Env-var only, never logged | Secret injection mechanism, `.env` hygiene | — |
| DB integrity | Migrations, CAS/lease concurrency control | Backups, TLS to DB | — |
| Health/monitoring | Structured JSON logs | Log aggregation | Dependency-aware health check (AUD-002), correlation IDs (AUD-010) |
| Mailbox credential scope | Read-only usage enforced in code | — | Isolated mailbox for XING (pre-accepted, not opportunistic) |
