# Stage 17 — Candidate profile & search configuration

Goal: the search constraints (place, commute, remote work, relocation,
roles, languages, salary, start date, already-sent applications) are stored
and applied by JobTriage itself, so a search never depends on an operator's
or an assistant's memory.

Migration `a9d3f6b2c4e8` (revises `f4b8d2a6c9e1`), additive only.

## 17A — Explicit database selection

`app/core/database_target.py` classifies `DATABASE_URL` (pure: never
connects): `PRODUCTION_RUNTIME` (`runtime/job_triage_runtime.db`), `PILOT`
(`stage*_*.db` in the project root or `runtime/`), `BACKUP` (`*.pre-*`,
`job-triage-backups\`), `OTHER_SQLITE`, `MEMORY`, `SERVER` (PostgreSQL).
`DATABASE_ROLE` (`production` | `pilot` | `test` | `development`, optional)
says what the process is for; `check_database_target` runs at web and
scheduler startup and refuses a mismatch:

- unset role: never a pilot (needs the explicit `DATABASE_ROLE=pilot`) and
  never a backup. This closes the incident class "the shell forgot the
  `DATABASE_URL` override and the app ran on `.env`'s pilot database";
- `production`: only the runtime file or PostgreSQL;
- `test`/`development`: never the runtime file, a pilot or a backup.

The Stage 14D canonical runtime sets `DATABASE_ROLE=pilot` itself. `.env` is
not rewritten: `stage11_pilot.db` stays reachable for the Stage 13B/14A pilot
tools, which take an explicit `--database-url`. `tests/conftest.py` gives the
whole suite a temporary database and `DATABASE_ROLE=test` before any module
is imported, and asserts the shared engine is isolated. Error messages name
the kind and file name only, never a URL.

## 17B — Persistent configuration

Existing tables are extended instead of adding parallel ones:

| Table | New columns | Meaning |
|---|---|---|
| `candidate_profiles` | `contact_email`, `contact_phone`, `contact_website_url`, `contact_trust_json` | confirmed contact data for Bewerbungen + provenance |
| `candidate_job_preferences` | `home_city/_postal_code/_region`, `commute_origin_label/_lat/_lon`, `max_commute_km`, `commute_measure` (`CAR_ROUTE`), `relocation_regions_json`, `remote_work_scope` (`GERMANY`/`UNKNOWN`), `remote_work_from_region`, `maximum_salary`, `salary_negotiable`, `earliest_start_date`, `primary_roles_json`, `secondary_roles_json`, `search_trust_json` | search configuration + per-field provenance |
| `candidate_profile_revisions` (new) | actor, reason, source, changed fields, before/after values | append-only audit (UPDATE/DELETE refused by triggers on SQLite and PostgreSQL) |
| `job_commute_checks` (new) | job, origin, destination, region, car km, minutes, method (`MANUAL`/`OSRM`), source note | verified car routes |

`minimum_salary`, `salary_currency`, `relocation`, `preferred_locations` and
`remote_preference` are the existing Stage 6A columns, kept consistent with
the configuration. `profile.target_roles` = primary roles, then secondary
roles. Including non-"Junior" and support/QA roles makes the Stage 11A/11B
seniority and domain gates fail open (UNKNOWN) by their own design, so a
relevant posting is no longer excluded only because it lacks the word
"Junior"; entry level is reported separately (below).

**Writing it.** `scripts/stage17_candidate_config.py apply --config FILE`
reads an operator-confirmed file (`jobtriage.candidate-config/1`, kept outside
the repository because it holds contact data) and prints a plan. `--apply
--expected-version N` writes exactly that plan in one transaction (atomic
compare-and-swap on `profile_version`, one revision row). Protections:

- only keys the file states are changed (an omitted key is untouched);
- a stored value whose source ranks higher (`USER_CONFIRMED` >
  `MANUAL_ENTRY` > `USER_PROVIDED_DOCUMENT` > `IMPORTED` > `INFERRED`) is never
  overwritten -- the plan shows `REFUSED` and nothing is written;
- a language level may only rise from a `USER_CONFIRMED` source; levels are
  never inferred (German stays B1; the B2 course is a note, not a level);
- skills and languages are only added/updated, never removed; existing
  skills are never modified; learning/project experience stays labelled as
  such in the skill notes;
- `search_sources` lowers the provenance of individual values that did not
  come from the candidate (the Heusenstamm Bahnhof coordinates are
  OpenStreetMap data: `IMPORTED`).

The Stage 6A PATCH endpoint now updates `candidate_job_preferences` in place
(replacing the row would wipe the search configuration) and refuses (409) to
replace a `USER_CONFIRMED` fact with document-grade data (`USER_PROVIDED_DOCUMENT`,
`IMPORTED`, `INFERRED`, `UNKNOWN`). A direct manual PATCH is still allowed.

Known limitation: the Stage 13B transfer snapshot (SQLite -> PostgreSQL
rehearsal tool) covers the Stage 6A facts only; it does not carry the
Stage 17 columns.

## 17C — Search and assessment

**Discovery** (`app/services/search_strategy.py`): with a configuration the
Bundesagentur run performs a commute search (`wo=63150 Heusenstamm`,
`umkreis` = the smallest BA radius step >= limit + 5 km, i.e. 150 km for a
100 km limit) and, if remote work across Germany is configured, a nationwide
home-office search (`arbeitszeit=ho`, 5 pages). The BA radius is a
straight-line distance; every workplace within 100 km by car is within it,
so the search discovers a superset and never decides anything. A posting
returned by both searches is processed once.

**Assessment** (`app/services/job_fit_assessment.py`,
`scripts/stage17_job_fit.py assess`), each criterion independent:

| Criterion | Source | Statuses |
|---|---|---|
| skills | persisted matcher v12 analysis (unchanged) | STRONG, PARTIAL, WEAK, NOT_EVALUATED |
| experience | matcher experience requirements, stated years | OK, PARTIAL, GAP, UNKNOWN |
| language | posting's German requirement vs the confirmed level | OK, MINOR_GAP (preferred), GAP, MAJOR_GAP (mandatory, >= 2 levels), NO_REQUIREMENT |
| location | stored car route vs the limit | PASS, FAIL (+ relocation note), DISTANCE_UNVERIFIED, UNKNOWN, REMOTE_OK, NOT_CONFIGURED |
| remote | posting text | COMPATIBLE, INCOMPATIBLE (restricted to another state), PARTIAL_REMOTE ("bis zu 100%"), HYBRID, ONSITE, UNKNOWN |
| salary | stated amount (monthly x12) vs expectation | MATCH, BELOW (informational; negotiable), TARIFF, UNKNOWN |
| entry_level | title/text signals | ENTRY_LEVEL, OPEN (no seniority stated -- not negative), MIXED, EXPERIENCED |
| start_date | "ab sofort" vs 01.01.2027 | OK, EARLIER_REQUESTED |
| duplicate | Stage 15 duplicate check | ALREADY_APPLIED, IN_PREPARATION, POSSIBLE_DUPLICATE, CLEAR |

Decision: `BLOCKED` (location FAIL, remote INCOMPATIBLE, ALREADY_APPLIED),
else `NEEDS_VERIFICATION` (distance unverified/unknown, possible duplicate,
skills not evaluated), else `CANDIDATE`. No criterion is folded into a score.

**Car routes.** Only a `job_commute_checks` row makes a distance PASS/FAIL.
`record-commute` stores a route the operator measured; `check-commute` asks
OSRM (`router.project-osrm.org`) for the driving route between the stored
origin coordinates and the given workplace coordinates -- only the two
coordinate pairs are sent, only on that explicit command; times are
free-flow. A straight-line distance is printed for information and never
stored as a car route.

**Duplicates enforced.** `BewerbungService.generate` and
`ReviewPackageService.create` call `ensure_not_already_applied`: a STRONG
duplicate (same job row, BA reference, URL, fingerprint, or same employer +
role across StepStone/XING/LinkedIn/Indeed/BA) of an application that was
sent (SUBMITTED or later) refuses the draft (`AlreadyAppliedError`, HTTP 409).
Applications still being prepared are not refused.

Nothing in Stage 17 sends mail, contacts an employer or submits anything.
