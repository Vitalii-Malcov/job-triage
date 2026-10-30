# Stage 22 — search quality

Source of the findings: the read-only search audit of 2026-10-30 (production
copy, 3 598 vacancies, 23 recorded applications). Stage 22 is delivered as
stacked pull requests on top of Stage 21 (PR #18):

| Part | What | PR | Migration |
|---|---|---|---|
| P0-3 | Advisory `SIMILAR_DESCRIPTION` duplicate signal for re-published / renamed vacancies | #19 | none |
| P0-1 | Full BA `referenznummer` persisted in `jobs.source_reference` | #20 | `d5f1a7c3e9b2` (down `c7e2a9d4f1b3`) |
| P0-2 | Read-only BA liveness check with explicit states | #20 | none |
| P1 | Structured BA facts, preliminary geography, home-office search, tier rework (STRETCH, role/seniority gate, education, German fluency) | stacked on #20 | `e3b7c9d1f4a6` (down `d5f1a7c3e9b2`) |

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

## P1 — structured facts, geography, remote, tiers

**Structured BA facts** (`app.collectors.bundesagentur.structured_facts`,
nine nullable `jobs` columns, migration `e3b7c9d1f4a6`): workplace
coordinates / postcode / region of the first `stellenlokation` and the
number of locations, `homeofficemoeglich` / `homeofficetyp` /
`homeofficeprozent`, `quereinstiegGeeignet`. Sanitized (an odd value is
None, never a dropped posting); refreshed by each observation that states
them, kept when omitted. Measured 2026-10-30: all 3 073 search results
carried coordinates.

**Two separate questions** (`app/agents/geo_estimate.py`,
`job_fit_assessment.location_criterion`):

| Question | Answered by | Result |
|---|---|---|
| A. Worth looking at? (tier) | straight line from the commute origin to the BA coordinates | `ESTIMATE_WITHIN` (≤ limit / 1.4), `ESTIMATE_NEAR_LIMIT`, `ESTIMATE_BEYOND` (> limit: a car route is never shorter), `ESTIMATE_BEYOND_RELOCATION` (beyond, but in a configured relocation region = Hessen) |
| B. May I apply? (decision, pre-Bewerbung) | a stored car route only | every estimate keeps `decision = NEEDS_VERIFICATION` |

`LOCATION_UNVERIFIED` replaces an estimate when the BA workplace is doubtful:
an agency posting, several work locations, or a posting that names another
workplace town (a labelled workplace address, "Arbeitsort/Einsatzort/Standort
X", "ausschließlich in X"). Neighbouring states (RLP, BW, Bayern, NRW,
Thüringen, Niedersachsen) are commutable; relocation is considered only
within the configured regions.

**Remote.** The remote search no longer sends `arbeitszeit=ho` (0 results on
the v6 API); it is nationwide and keeps only postings with
`homeofficemoeglich=true` (`skipped_not_home_office`, `truncated` and an
explicit `bundesagentur_remote_query_empty` warning are logged). A kept
posting is still not "remote": FULLY_REMOTE comes only from the posting
text; the BA flag only fills a silent text (PARTIAL_REMOTE / ONSITE).

**Tiers** (`FitAssessment._tier`), strongest first:

| Tier | Meaning | Triggers (examples) |
|---|---|---|
| `NOT_SUITABLE` | unsuitable | a hard blocker; `ESTIMATE_BEYOND`; `role OFF_ROLE` (explicit other profession: Aktuar, PhD/Doktorand, Trainer/Dozent, Sales/Vertrieb, Office/kaufmännisch only when the title has no IT word; IT administration/support stays allowed); `entry_level SENIOR_TITLE` (not with junior/trainee/Quereinstieg wording); `posting_type` SELF_EMPLOYMENT (BA: often a course) or APPRENTICESHIP; mandatory German C1/"verhandlungssicher"/"fließend"/native with a confirmed B1; ≥ 3 years asked and the matcher finds no such experience |
| `NEEDS_OPERATOR` | needs verification (data missing) | skills not evaluated, workplace/distance unverified, near the limit, possible duplicate, `role NO_IT_SIGNAL` (title outside the IT vocabulary -- a question, never proof) |
| `STRETCH` (new) | worth trying despite a stated gap | German B2 or "sehr gute" with B1 (B2 in progress is not a certificate); a stated experience gap; mandatory computer-science degree without alternative; weak skill coverage; a Werkstudent title (enrolment) |
| `PARTIAL_MATCH` | worth checking | partial skills, unconfigured role, salary below expectation |
| `GOOD_MATCH` | high fit | strong skills, configured role, nothing above |

New criterion `education`: `NO_REQUIREMENT`, `OPTIONAL`,
`DEGREE_OR_EQUIVALENT` (engineering/technical field, "oder vergleichbar",
Fachinformatiker), `QUEREINSTIEG` (BA flag or "Quereinsteiger"),
`IT_DEGREE_REQUIRED`; new criterion `posting_type` from the BA
`stellenangebotsart` (trainee programmes stay allowed). The Stage 21 cache key covers the new rules and
columns; the migration recreates the (derived) cache table for the new
`STRETCH` value.

## P1-2 — vacancy identity (dry run only)

`scripts/vacancy_identity_report.py --database PATH [--json]` (read-only)
groups rows sharing `(source, source_reference)` and lists, per row, every
table pointing at it -- with FK (`job_reference_tokens`, `job_commute_checks`,
`job_fit_assessments`, `telegram_vacancy_reviews` UNIQUE per job) and without
(`applications`, `application_package_reviews`, `bewerbung_drafts`,
`candidate_cv_drafts`, `candidate_job_matches`, `follow_up_proposals`,
`gmail_message_analyses`, `response_drafts`). It names the row a merge would
keep (oldest) and the risks: `APPLICATION_OR_APPROVAL` (never merged
automatically; approvals are never moved), `UNIQUE_CONFLICT:<table>`,
`DIFFERENT_COMPANY_TEXT`, `TITLE_CHANGED`. Production copy, 2026-10-30: 5
groups / 10 rows, all `TITLE_CHANGED` (the employer edited the posting:
"Junior Manager" -> "Manager", "(Senior)" added), 1 with an application.
No merge is implemented; a merge is a separate, approved stage.

## Matcher analyses for every vacancy

`scripts/stage22_match_vacancies.py` (dry run default; production `--apply`
needs `--snapshot`) runs the existing local, deterministic matcher for every
vacancy without a current analysis (production copy: 2 118 of 2 888, ~10 min).
Without it those vacancies stay `NEEDS_OPERATOR` (skills not evaluated).

## P2 — operations

Done:
- `transition` / `override --occurred-at ISO8601` (real event time, e.g. the
  employer mail's date; refused when in the future, without offset, or before
  the current status).
- Verified, no change needed: follow-up dates exist only for waiting statuses
  (SUBMITTED / RECEIVED / UNDER_REVIEW) -- a REJECTED application never gets
  one; nothing in the tracker sends a follow-up (`OUTBOUND_SENDING_ENABLED`
  stays false); job-board salary estimates (XING) are already labelled
  "job-board estimate, not stated by the employer" in the salary criterion.

Deliberately NOT done (needs its own approved stage):
- Snoozing a follow-up / a deadline-aware follow-up date: it needs a new
  application event type, i.e. a CHECK change on the append-only
  `application_events` table (SQLite table rebuild under its triggers) --
  too risky to bundle. Until then: note the date in OFFENE_AKTIONEN and do
  not follow up before the employer's deadline.
- "German B2 in progress" in the profile: the confirmed level stays B1 (a
  course is not a certificate); the operator may add it to the language's
  `notes` -- no code reads it as B2.

## Rollout (needs separate approval)

The new revision is applied by the same `alembic upgrade head` as Stage 21.
Order inside the production window (`docs/STAGE21_VACANCY_CACHE_PLAN.md` §9):

1. backup + `alembic upgrade head` (`b6c1e9a4d2f7` → `c7e2a9d4f1b3` → `d5f1a7c3e9b2` → `e3b7c9d1f4a6`);
2. `backfill_source_reference.py` dry run, then `--apply --snapshot`;
3. `stage22_match_vacancies.py` dry run, then `--apply --snapshot`
   (structured BA facts arrive with the next collector run);
4. **then** the Stage 21 cache backfill — `source_reference` and the new
   columns are part of the cached job-row hash, so filling them after the
   cache would mark every cached row stale;
5. smoke tests (tracker `list`, `check-job`, UI).

Rollback: `alembic downgrade c7e2a9d4f1b3` drops only the column and index
(rows untouched — tested); `b6c1e9a4d2f7` additionally drops the cache table.

## Quality check on a production copy (2026-10-30, scratchpad only)

Copy of production (2 888 vacancies), migrated to `e3b7c9d1f4a6`, references
backfilled, BA facts taken from the 2026-10-30 research search results for
1 419 matched vacancies, the (local, deterministic) matcher run for every
vacancy, then `assess_job`:

| Tier | Stage 22 P0 rules | Stage 22 P1 rules |
|---|---|---|
| GOOD_MATCH | 0 | 0 |
| PARTIAL_MATCH | 0 | 25 |
| STRETCH | — | 207 |
| NEEDS_OPERATOR | 2 496 | 1 150 (878 without BA coordinates yet) |
| NOT_SUITABLE | 392 | 1 506 |

NOT_SUITABLE reasons: senior title 569, straight line beyond the limit 437,
mandatory German C1-level 373, no IT word in the title 338, other profession
83, apprenticeship 80, self-employment/course 35. Every vacancy with a
recorded application keeps only its ALREADY_APPLIED barrier (no new false
barrier). Without a matcher analysis a vacancy stays NEEDS_OPERATOR
(`skills NOT_EVALUATED`): production has analyses for 770 of 2 888 rows, so
the matcher must run for all vacancies before the cache backfill.
