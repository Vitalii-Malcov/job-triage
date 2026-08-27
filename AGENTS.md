# Agent Collaboration Rules

## Claude Code — Developer
Owns implementation, refactoring, tests and documentation.

## Codex — Reviewer
Must independently review proposed changes and should not merely confirm Claude's conclusions.

Review priorities:
1. Correctness and regressions
2. Security and secret handling
3. API/input validation
4. Async/network error handling
5. Test coverage
6. Architecture boundaries

## Merge gate
A change is ready only when:
- tests pass;
- Codex has no unresolved high/medium findings;
- security-sensitive changes have explicit review;
- user-facing automation preserves human approval where required.

## Review gate additions (v0.2)
Codex must reject a change if any of these regress:
- persistence/deduplication semantics
- notification isolation (Telegram failure must not break successful job scoring)
- API authentication/rate limiting on non-health endpoints
- tests, Ruff checks, or formatting
- secrets committed to source control
- any change to SQLAlchemy models without an accompanying Alembic migration in `alembic/versions/`

Before PASS, run or verify: `pytest -q`, `ruff check .`, and `ruff format --check .`.
