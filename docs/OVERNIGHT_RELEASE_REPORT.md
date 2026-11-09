# Overnight release report — PR #47–#55 merged into main

Date: 2026-11-09/10. Final status: **SUCCESS — ALL PRs MERGED**. Final `main` SHA: `3ebc40ae69975b165c25058bcb88cf902cc8b265`.

## GitHub

All nine PRs were a linear chain (each base = previous head, each head a descendant of its base).
Merged with plain merge commits (no squash/rebase, no `--admin`), each base retargeted to `main`
after its parent merged. Every PR was MERGEABLE/CLEAN with green CI on its head before merge.
`main` has no branch protection and no required reviews (`reviewDecision` empty on all PRs).

| PR | Head before merge | Merged (UTC) | Merge commit |
|---|---|---|---|
| #47 audit final integration | fcc13bf | 2026-11-09 22:36:05 | 8cf1efa |
| #48 job matching quality | 1ada253 | 22:38:32 | 351fa3f |
| #49 nationwide remote discovery | 6ec1934 | 22:38:55 | bc96906 |
| #50 junior scoring enrichment | 43d4095 | 22:39:18 | 2d59e39 |
| #51 manual review queue | ea614d1 | 22:39:41 | c5c5f64 |
| #52 review queue optimization | 0d0e6d9 | 22:40:04 | 5a558a8 |
| #53 work-time classification | 98ad8a8 | 22:40:27 | a7996c5 |
| #54 confirmed full-time gate | 86dc6e6 | 22:40:38 | 0bac5e5 |
| #55 application idempotency | 4d2d808 | 22:40:50 | 3ebc40a |

The tree of `main` (`6e82909…`) is byte-identical to the tree of the release candidate (#55 head
`4d2d808`) that was tested locally. GitHub CI on main HEAD `3ebc40a`: test, docker,
scheduler-postgres all **success** (run 42714027655).

## Checks

- Ruff check + format check: pass (646 files formatted).
- Full pytest (local Windows, release-candidate worktree): **8672 passed, 168 skipped, 0 failed** (43 min). No `WinError 4551`.
- Linux CI (test, docker smoke, scheduler-postgres/PostgreSQL integration): green on every PR head and on final main. Docker and PostgreSQL were not re-run locally; CI is the evidence.
- Alembic: single head `a7c3e5f1b9d2`. A migration rehearsal against a production copy was **not** run locally; CI migration tests passed.
- Codex (read-only, release candidate): **VERDICT: REJECT, four P2, no P0/P1.** Not an ACCEPT; none is claimed. Findings:
  1. `job_fit_signals.py:1038` `vollzeit\w*` accepts "Vollzeitnah (30 Std.)" as FULL_TIME.
  2. `job_fit_signals.py:1255` "Nur Teilzeit. Vollzeit eventuell später möglich." resolves FULL_TIME.
  3. `job_fit_assessment.py:657` source flag `full_time_offered=True` overrides explicit "Keine Vollzeit möglich" (should be CONFLICT).
  4. `review_queue.py:142` jobs held for `WORK_TIME_NOT_CONFIRMED` do not appear in the manual review queue.
  Impact: 1–3 can only make the automatic gate too permissive, and auto-prepare is OFF (`AUTOMATION_AUTO_PREPARE_ENABLED=false`); with it ON they matter. 4 hides held jobs from `/ui/review`. Recommend fixing 1–3 before ever enabling auto-prepare. Per the mission rules (block only on P0/P1) merge proceeded. Formal Codex ACCEPT for #52/#53 was never obtained.

## Safety (read-only verification)

Consistent SQLite backup-API copy of `runtime/job_triage_runtime.db`: integrity ok, 0 FK violations,
3,234 jobs, 25 applications, 54 application_events, Alembic `d6e9a2b4c8f1` (all match history).
SHA-256 prefix of the production file before and after the whole night: `aef7383ef194fd6a` — **unchanged**
(no migration, no cache refill). Flags in `.env` (values only as booleans): OUTBOUND_SENDING_ENABLED=false,
AUTOMATION_AUTO_PREPARE_ENABLED=false, AUTOMATION_SCHEDULER_ENABLED=false, ALEMBIC_AUTO_UPGRADE=false;
NATIONWIDE_REMOTE_DISCOVERY_ENABLED unset, code default `False`. Nothing was sent; no Gmail/Telegram/portal action.

## Open risks

- Codex P2 items above; Codex limits on #52/#53 (no formal ACCEPT).
- Concurrent preparation can still leave local duplicate drafts in a race (idempotency is check-then-write at application level).
- Source aliases: dedup relies on source_reference/canonical source; unknown aliases of the same posting may not match.
- Work-time classifier is a regex heuristic; UNKNOWN is held, but wording like "Halbtags", "50 %" is only recognised as UNKNOWN.
- Cold cache: `/ui/review` is incomplete until the fit-assessment cache is refilled (step 5 below).
- Production vs test nationwide data differ; the nationwide flag stays OFF until a supervised first run.
- Local `main` checkout has the operator's own uncommitted/untracked files (CLAUDE.md edit, postman/, review docs); untouched.

## Operator runbook (NOT executed; production untouched)

1. Stop JobTriage app and scheduler.
2. Backup: `python -c "import sqlite3;s=sqlite3.connect('runtime/job_triage_runtime.db');d=sqlite3.connect('runtime/job_triage_runtime.pre-release-20261110.db');s.backup(d)"`; check `PRAGMA integrity_check`, row counts (3234 / 25 / 54) and SHA-256 of the copy.
3. Rollback plan: stop app, restore the backup file over the runtime DB, `alembic current` must show `d6e9a2b4c8f1`, check out the previous commit `d0f576e`.
4. `git pull` on main (`3ebc40a`), then `alembic upgrade head` (expect `a7c3e5f1b9d2`).
5. `python scripts/stage21_assess_vacancies.py --apply` (refills the fit cache).
6. Re-run integrity check; counts of applications/events must be 25/54 (plus nothing deleted).
7. Start JobTriage with all four flags still false.
8. Open `/ui/review`; check the Junior Python / Backend list.
9. Confirm no outbound sends (Telegram/Gmail logs empty, `application_events` unchanged).
