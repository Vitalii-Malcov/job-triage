# Stage 22 — search quality

Source of the findings: the read-only search audit of 2026-10-30 (production
copy, 3 598 vacancies, 23 recorded applications). Stage 22 is delivered as
stacked pull requests on top of Stage 21 (PR #18):

| Part | What | PR | Migration |
|---|---|---|---|
| P0-3 | Advisory `SIMILAR_DESCRIPTION` duplicate signal for re-published / renamed vacancies | #19 | none |
| P0-1 | Full BA `referenznummer` persisted in `jobs.source_reference` | this branch | `d5f1a7c3e9b2` (down `c7e2a9d4f1b3`) |
| P0-2 | Read-only BA liveness check with explicit states | this branch | none |

None of it is merged into `main` or deployed to production until the
operator approves it (see "Rollout" below).

## P0-1 — the BA reference is kept

**Problem.** `BundesagenturCollector` mapped `referenznummer` into
`Job.source_reference`, but `JobRecord` had no column for it, so it was lost
at `upsert_job`. The only trace were reference tokens derived from the URL,
which miss every reference containing `_`, `.` or `/`
(`12265-444700_JB5270016-S`, `13410-k13572.15943-S`,
`13319-896770/1_626382LS-S`) and every posting whose URL is the employer's own
(`externeURL`). Consequences measured on 2026-10-30: 478 of 644 vacancies
considered "closed" were false 404s of truncated references.

**Change.**
- `app/collectors/ba_reference.py`: `storable_reference` (collector value,
  column-safe), `ba_reference_from_url` (whole path after
  `/jobsuche/jobdetail/`, BA host only), `extract_ba_references` (a pasted
  BA job URL, or a whole identifier token in the strict `<4-5 digits>-<id>-S`
  shape in any case -- never a prefix of a longer identifier; bounded to 8),
  `is_valid_ba_reference`.
- `jobs.source_reference` String(64), nullable, non-unique index
  `(source, source_reference)` (one BA posting can still be several rows until
  the fingerprint changes — P1, separate stage).
- `upsert_job`: stored on insert; on update set once atomically
  (`UPDATE ... WHERE source_reference IS NULL` in the job write's
  transaction), never blanked by an omitted/blank value, never replaced by a
  different value (logged as `job_source_reference_conflict`).
- E-mail matching: `JobCandidate.source_reference`; full references quoted in
  a mail are matched against stored references with the same precedence and
  ambiguity rules as every explicit reference (two jobs with one reference
  stay AMBIGUOUS); candidate lookup by exact `source_reference` equality also
  outside the recency window. The analysis context fingerprint includes the
  reference only when set (unchanged for rows without one).
- Duplicate check (`application_duplicates`): the stored reference is
  preferred; a BA URL reference is compared whole (the old path-segment regex
  cut slashed references).
- Send readiness: a stable `VacancyIdentity` also binds by the stored
  reference.
- `scripts/backfill_source_reference.py`: dry run by default; fills only
  empty BA rows from their own BA URL (compare-and-set on NULL, idempotent);
  reports references stored on several rows; production `--apply` needs a
  verified `--snapshot`.

Measured on a production copy (2 770 BA rows): 2 386 filled from the URL, 384
not derivable (employer URL; filled by the next collection run), 5 references
on 2 rows each.

## P0-2 — liveness without false "closed"

`app/services/ba_liveness.py` + `scripts/ba_liveness.py` (read-only database
access, output only to an optional JSON-lines file, 1 request/s):

| State | When |
|---|---|
| `ACTIVE` | 200 with a JSON object |
| `CLOSED` | 404 for a strictly valid full BA reference |
| `INVALID_REFERENCE` | no reference or not in the BA shape — never requested |
| `SOURCE_ERROR` | 429, 5xx, 408, timeout, network error, non-JSON 200, 401/403 |
| `UNKNOWN` | any other status, or a 200 naming a different reference |

Every `SOURCE_ERROR` except a rejected key is retried once (honouring
`Retry-After`, capped at 30 s); the request rate (`--rate`, default 1 s)
applies to every attempt, retries included; a rejected key (401/403) stops
the run.
A resumed run with the same `--output` skips final states and retries only
`SOURCE_ERROR`. No state is written to the database; a CLOSED result is a
hint for the operator.

## Rollout (needs separate approval)

The new revision is applied by the same `alembic upgrade head` as Stage 21.
Order inside the production window (`docs/STAGE21_VACANCY_CACHE_PLAN.md` §9):

1. backup + `alembic upgrade head` (`b6c1e9a4d2f7` → `c7e2a9d4f1b3` → `d5f1a7c3e9b2`);
2. `backfill_source_reference.py` dry run, then `--apply --snapshot`;
3. **then** the Stage 21 cache backfill — `source_reference` is part of the
   cached job-row hash, so filling it after the cache would mark every cached
   row stale;
4. smoke tests (tracker `list`, `check-job`, UI).

Rollback: `alembic downgrade c7e2a9d4f1b3` drops only the column and index
(rows untouched — tested); `b6c1e9a4d2f7` additionally drops the cache table.
