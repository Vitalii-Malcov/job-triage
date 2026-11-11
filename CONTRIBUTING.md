# Contributing

Single-maintainer project; these rules keep changes reviewable. See `CLAUDE.md` / `AGENTS.md` for the agent workflow.

1. Branch from `main`; one concern per branch/PR. Never push to `main` directly.
2. Every behaviour change ships with a test. Schema changes (`app/db/models/`) ship with an Alembic migration.
3. Before pushing: `ruff check .`, `ruff format --check .`, then targeted `pytest` (the full suite takes ~45-55 min; CI is the authority).
4. Secrets only via environment variables. Never commit `.env`, databases or key material.
5. Nothing may send a job application, e-mail or Telegram message without explicit operator approval
   (see `docs/SECURITY_MODEL.md`). Collectors must not request links extracted from external content.
6. Merge with a standard merge commit after CI (test, scheduler-postgres, docker) is green. No force push, no `--admin`.
