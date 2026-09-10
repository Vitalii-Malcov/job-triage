# Portfolio Notes — JobTriage

Recruiter-facing material. Everything here is verified against the actual repository at HEAD `chore/production-readiness-r1` — no aspirational claims.

## 1-sentence description

An AI-assisted job-search automation backend (FastAPI/PostgreSQL) that collects and scores job postings, drafts CVs, cover letters, and email responses deterministically, and gates every outbound action behind explicit human approval.

## 3-sentence description

JobTriage is a FastAPI + PostgreSQL backend that automates the repetitive parts of a job search — collecting postings from multiple sources, scoring them against a candidate profile, and drafting CVs, cover letters, and email responses — while keeping every action that could have a real-world consequence (sending an email, mutating application status) behind an explicit, database-recorded human approval step. It's built with production concerns in mind from the start: Alembic-versioned schema migrations, compare-and-swap concurrency control for its automation scheduler (verified against real PostgreSQL, not just SQLite), and 1900+ tests including dedicated PostgreSQL concurrency integration tests in CI. Content generation is deterministic and template-based rather than free-form LLM output, so every generated fact traces back to a specific, trust-classified source — including a hard rule that content parsed from unauthenticated inbound email can never reach generated text.

## CV bullet version

- Designed and built a FastAPI/PostgreSQL job-search automation backend (Python, SQLAlchemy, Alembic) with database-enforced concurrency control (lease + CAS locking, verified against real PostgreSQL in CI), deterministic content generation with explicit data-provenance/trust gating, and human-approval-gated outbound actions; 1900+ tests, Dockerized deployment.

## LinkedIn / GitHub portfolio version

**JobTriage — AI-Assisted Job Application Automation Platform**

A backend I built to automate the repetitive parts of job hunting (collecting postings, scoring fit, drafting CVs/cover letters/email replies) while keeping a hard human-approval gate around anything that sends real email or touches application status.

Highlights: FastAPI + PostgreSQL + SQLAlchemy + Alembic; database-enforced concurrency control for the automation scheduler (compare-and-swap claiming, lease-based crash recovery, tested against real PostgreSQL — not just SQLite); deterministic, trust-gated content generation instead of free-form LLM calls, so generated text is always traceable to a verified source; 1900+ tests including PostgreSQL-only concurrency integration tests in CI; Dockerized with a documented, validated deployment path.

Built primarily with Claude Code as the implementation agent and Codex as an independent reviewer — a real case study in AI-assisted software engineering with human-owned architecture decisions and safety boundaries.

## Technical interviewer version

JobTriage is a layered FastAPI application (`api → services → agents/providers/collectors → repositories → SQLAlchemy models`) built to explore three things simultaneously: (1) correct handling of concurrency and idempotency in a background-automation system — the scheduler uses a partial unique index plus a renewable lease for run-exclusivity and an atomic `UPDATE ... WHERE next_run_at = :observed` for schedule claiming, both verified under real PostgreSQL concurrent load in dedicated integration tests, not asserted from SQLite alone; (2) a genuinely hard safety boundary around untrusted input — job facts and candidate facts each carry an explicit trust/provenance classification, and content parsed from unauthenticated inbound email (a XING digest, or a Gmail reply) is structurally prevented from reaching generated drafts, enforced in code rather than by convention; and (3) IMAP protocol-level correctness under adversarial network conditions — a full wall-clock deadline covers every phase of an IMAP session, with DNS resolution isolated into a subprocess specifically because a blocked C-level `getaddrinfo()` call can't be forcibly cancelled from a Python thread, only from a separate OS process. The project also documents its own limitations honestly rather than glossing over them (see `docs/PRODUCTION_READINESS_AUDIT.md`): the in-memory rate limiter isn't multi-worker safe yet, and the health endpoint doesn't check downstream dependencies — both recorded findings, not hidden gaps.

## Strongest engineering points (verified)

- **FastAPI backend** — `app/main.py`, `app/api/routes.py` (50 routes).
- **PostgreSQL** — production target, exercised in CI via a real `postgres:16` service container (`.github/workflows/ci.yml`).
- **SQLAlchemy 2.x** — `app/db/models.py`, repository pattern throughout `app/db/`.
- **Alembic** — 30 migrations, single head `c7d3f9a1e5b8`, CI asserts the chain actually reaches it against a fresh real database.
- **Pydantic v2 / pydantic-settings** — `app/models/`, `app/core/config.py`.
- **Concurrency / CAS** — `app/db/automation_repository.py`, `app/db/automation_schedule_repository.py`; verified via `tests/integration/test_scheduler_postgres_concurrency.py`.
- **Automation scheduler** — `app/scheduler.py`, deliberately a separate process from the API workers, crash-safe by design.
- **IMAP/SMTP** — `app/providers/email/`, including a from-scratch deadline/socket-lifecycle mechanism (`imap_deadline.py`).
- **Retries/deadlines** — retryable-vs-permanent failure classification across collectors and email providers.
- **Human approval** — `app/services/response_draft_send.py`, `app/services/follow_up_send.py` — "no approval, no send," enforced in code.
- **Provenance/trust** — `app/services/response_draft.py::TRUSTED_JOB_SOURCES`, `app/models/candidate_profile.py::is_top_level_fact_usable_for_generation`.
- **1900+ tests** — `1934 passed, 4 skipped, 0 failed` at the audited HEAD (`pytest -q`).
- **GitHub Actions** — two-job CI (unit+lint+format, and a real-PostgreSQL concurrency/migration gate), plus a Docker build/config gate added in this branch.
- **Docker** — `Dockerfile`, `compose.yaml`, locally built and smoke-tested against a real PostgreSQL container (see `docs/DOCKER.md`).

## GitHub repository description (recommendation)

> AI-assisted job application automation backend — FastAPI + PostgreSQL, deterministic drafting, human-approval-gated sends, CAS-based automation scheduler, 1900+ tests.

## GitHub topics (recommendation, 10)

`fastapi` `python` `postgresql` `sqlalchemy` `alembic` `pydantic` `docker` `job-search-automation` `imap` `github-actions`

(Not proposing repository setting changes — this is a recommendation only, per instruction not to modify GitHub settings automatically.)
