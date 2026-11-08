# Nationwide remote discovery — rollout

Status of this document: a plan for the operator. Nothing in it has been executed against the
production database, scheduler or `.env`.

## What changes when this is deployed

| Item | Effect |
|---|---|
| Alembic `a7c3e5f1b9d2` | Adds one nullable column `jobs.full_time_offered`. Additive; no row is rewritten; downgrade drops the column. |
| `LOCAL_DISCOVERY_ENABLED` | New flag, **default `true`**. Today's local (commute) Bundesagentur step keeps running exactly as before. Setting it to `false` removes only that step from the automation cycle. |
| `NATIONWIDE_REMOTE_DISCOVERY_ENABLED` | New flag, **default `false`**. Nothing new runs until the operator turns it on. |
| Fit assessment | `work_time` uses the source's own `arbeitszeitVollzeit` flag only where the posting text is silent. Text always wins. Flag `false` means PART_TIME, i.e. *review*, never exclusion. |
| Fit cache | Every job column is part of the cache key, so existing cached assessments become stale. Recompute with the existing offline tool when convenient; nothing is recomputed automatically. |

Everything else (thresholds, shortlist gate, `jobs.score`, applications, statuses, Telegram, Gmail)
is untouched.

## Settings (all optional, all in `.env`, none present today)

| Variable | Default | Meaning |
|---|---:|---|
| `LOCAL_DISCOVERY_ENABLED` | `true` | TRACK A on/off |
| `NATIONWIDE_REMOTE_DISCOVERY_ENABLED` | `false` | TRACK B on/off |
| `NATIONWIDE_REMOTE_DISCOVERY_INTERVAL_SECONDS` | `86400` | minimum time between two nationwide runs (the automation cycle may run more often) |
| `NATIONWIDE_REMOTE_REQUEST_DELAY_SECONDS` | `1.0` | pause between any two BA requests of a nationwide run, retries and detail calls included |
| `NATIONWIDE_REMOTE_MAX_REQUESTS_PER_RUN` | `60` | ceiling on search requests per run (a full backfill needs about 21 for 11 terms) |
| `NATIONWIDE_REMOTE_MAX_DETAIL_PER_RUN` | `400` | ceiling on detail requests per run, retries included |
| `NATIONWIDE_REMOTE_MAX_NEW_PER_RUN` | `150` | ceiling on vacancies *stored* per run |

## Recommended order

1. **Review and merge** this PR into the PR #48 branch (the operator, not this tool).
2. **Back up** the working SQLite with `scripts/backup_sqlite.py` (existing procedure).
3. **Migrate**: `alembic upgrade head` (applies `a7c3e5f1b9d2`). Check `alembic current`.
4. **Deploy with both flags as they are** (local on, nationwide off). Confirm one normal automation
   cycle behaves as before.
5. **Rehearse on a copy once more** with the real profile:
   `python -m scripts.nationwide_dry_run --db <copy.db> --json <new-file>.json`
   (refuses the production database).
6. **Enable** `NATIONWIDE_REMOTE_DISCOVERY_ENABLED=true`. The first run is a backfill (no window);
   it needs about 21 search requests and up to 400 detail requests at one per second, i.e. roughly
   8 minutes, and stores up to 150 vacancies. If it reports `deferred`, the next cycle continues;
   the step is not counted as complete until nothing is deferred, truncated or failed.
7. Later runs are incremental (BA `veroeffentlichtseit` 7 or 14 days, the only two values the API
   honours).
8. Watch the step result `bundesagentur_nationwide` in the automation run (`searches`, `requests`,
   `hits`, `created`, `updated`, `skipped_not_target_title`, `skipped_not_remote_candidate`,
   `deferred`, `truncated_searches`, `failed_queries`, `detail_unavailable`).

## Rollback

* Switch `NATIONWIDE_REMOTE_DISCOVERY_ENABLED=false`. Rows already stored stay (they are ordinary
  vacancies, assessed by the same gates). Nothing else needs undoing.
* Schema: `alembic downgrade -1` drops `jobs.full_time_offered`; the value returns with the next
  collection.

## Operating limits to know

* The BA API is unofficial and shared (`X-API-Key: jobboerse-jobsuche`). Its `arbeitszeit=ho`
  filter returns nothing; `arbeitszeit=vz;ho` behaves as OR; there is no usable remote filter.
  The search is fuzzy (OR-like): a term such as "Junior Data Engineer Python" returns about 300
  hits, most of them irrelevant. That is why a title pre-filter and a text check exist.
* Postings that are rejected (on-site, or no remote evidence) are **not stored**, so a later run
  looks at them again while they stay inside the 7/14-day window and costs a detail request each
  time. Keep `NATIONWIDE_REMOTE_MAX_DETAIL_PER_RUN` above the number of daily candidates (about
  250 on the first backfill, far fewer incrementally). A persistent rejection ledger would need a
  table and is deliberately not part of this change.
* The candidate order inside a run is a daily weighted shuffle (entry-level titles three times as
  likely to come early), so the same postings are not always the ones the caps leave out.
* Descriptions that BA itself does not provide are stored empty and are not counted as a failed
  run; a failing or exhausted detail endpoint is counted (`detail_unavailable`) and keeps the
  period open.

## What this does not do

* It does not send, approve or prepare anything. Any vacancy card or alert comes from the same
  flag-gated tail the local run already uses (`TELEGRAM_VACANCY_FEED_ENABLED`,
  `MIN_JOB_SCORE_TO_NOTIFY`), and a confirmed barrier (NOT_SUITABLE) is never proposed.
* It does not lower any threshold. A vacancy still needs the existing admission to be
  shortlisted, and admission is not approval to apply.
* It does not touch XING. The ~118 title-only XING rows need an operator (see the report,
  section "XING stubs").
