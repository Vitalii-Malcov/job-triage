# Stage 17 — Candidate profile & search configuration

Goal: the search constraints (place, commute, remote work, relocation,
roles, languages, salary, start date, already-sent applications) are stored
and applied by JobTriage itself, so a search never depends on an operator's
or an assistant's memory.

Migrations `a9d3f6b2c4e8` (revises `f4b8d2a6c9e1`) and `b6c1e9a4d2f7` (search
targets, car-route origin coordinates), both additive only.

## 17A — Explicit database selection

`app/core/database_target.py` classifies `DATABASE_URL` (pure: never
connects): `PRODUCTION_RUNTIME` (`runtime/job_triage_runtime.db`), `PILOT`
(`stage*_*.db` in the project root or `runtime/`), `BACKUP` (`*.pre-*`,
`job-triage-backups\`), `OTHER_SQLITE`, `MEMORY`, `SERVER` (PostgreSQL).
Both the literal and the percent-decoded spelling of the path are classified
(SQLite decodes `file:` URIs) and the most sensitive kind wins. A PostgreSQL
database whose name says pilot/stage/scratch or backup is classified like
the corresponding file. The query never changes the classification (the
named file decides), and a `production`/`pilot` process is refused if the
URL asks for `mode=memory` in any spelling (it must open the real file).
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
| `candidate_job_preferences` | `home_city/_postal_code/_region`, `commute_origin_label/_lat/_lon`, `max_commute_km`, `commute_measure` (`CAR_ROUTE`), `relocation_regions_json`, `remote_work_scope` (`GERMANY`/`UNKNOWN`), `remote_work_from_region`, `maximum_salary`, `salary_negotiable`, `earliest_start_date`, `primary_roles_json`, `secondary_roles_json`, `target_seniority`, `search_keywords_json`, `search_trust_json` | search configuration + per-field provenance |
| `candidate_profile_revisions` (new) | actor, reason, source, changed fields, before/after values | append-only audit (UPDATE/DELETE refused by triggers on SQLite and PostgreSQL) |
| `job_commute_checks` (new) | job, origin label + coordinates, destination, region, car km, minutes, method (`MANUAL`/`OSRM`), source note | verified car routes |

`minimum_salary`, `salary_currency`, `relocation`, `preferred_locations` and
`remote_preference` are the existing Stage 6A columns, kept consistent with
the configuration. The primary/secondary role priorities are stored only in
the search configuration; `profile.target_roles` is NOT changed by Stage 17,
because it drives the Stage 11A/11B collector gates: a non-"Junior" or
support role there would make them fail open and let senior or unrelated
postings reach APPLY. Those gates never exclude a posting merely for lacking
the word "Junior" (only titles that state Senior/Lead/... or are classified
IRRELEVANT); support, QA and IT-integration titles classify as UNKNOWN and
pass. The collector seniority gate instead reads the explicit
`target_seniority` (`JUNIOR` = entry-level target) when it is configured.
Entry level is reported separately (below).

**Writing it.** `scripts/stage17_candidate_config.py apply --config FILE`
reads an operator-confirmed file (`jobtriage.candidate-config/1`, kept outside
the repository because it holds contact data) and prints a plan. `--apply
--expected-version N` writes exactly that plan in one transaction (atomic
compare-and-swap on `profile_version`, one revision row). Protections:

- only keys the file states are changed (an omitted key is untouched), and
  the COMBINED result must still be a valid configuration (e.g. a partial
  file cannot drop the commute measure under a stored limit);
- a stored value whose source ranks higher (`USER_CONFIRMED` >
  `MANUAL_ENTRY` > `USER_PROVIDED_DOCUMENT` > `IMPORTED` > `INFERRED`) is never
  overwritten -- the plan shows `REFUSED` and nothing is written;
- a language level may only rise from a `USER_CONFIRMED` source; levels are
  never inferred (an ongoing course is a note, not a level);
- skills and languages are only added/updated, never removed; existing
  skills are never modified; learning/project experience stays labelled as
  such in the skill notes;
- `search_sources` lowers the provenance of individual values that did not
  come from the candidate (the Musterstadt Bahnhof coordinates are
  OpenStreetMap data: `IMPORTED`).

The Stage 6A PATCH endpoint now updates `candidate_job_preferences` in place
(replacing the row would wipe the search configuration) and refuses (409) to
replace a `USER_CONFIRMED` fact with document-grade data (`USER_PROVIDED_DOCUMENT`,
`IMPORTED`, `INFERRED`, `UNKNOWN`); a replaced language/skill collection may
not drop or alter a confirmed item through document-grade or empty data. A
direct manual PATCH is still allowed.

Known limitation: the Stage 13B transfer snapshot (SQLite -> PostgreSQL
rehearsal tool) covers the Stage 6A facts only; it does not carry the
Stage 17 columns.

## 17C — Search and assessment

**Discovery** (`app/services/search_strategy.py`): with a configuration the
Bundesagentur run performs a commute search (`wo=12345 Musterstadt`,
`umkreis` = the smallest BA radius step >= limit + 50 km, i.e. 150 km for a
100 km limit; the limit is capped at 150 km so a step always exists. The
50 km margin covers the distance between the BA place centre and the
commute origin. The configuration requires the origin label to name a
place inside `home_city`; no German municipality extends 50 km from its
centre, so the superset holds as long as the operator-confirmed origin
really lies in that place. The coordinates themselves are not
cross-checked (that would need geocoding the home place)) and, if remote work across Germany is configured, a nationwide
home-office search (`arbeitszeit=ho`, 5 pages). With `search_keywords`
configured (at most 6) each term gets its own commute and remote search;
otherwise `BUNDESAGENTUR_SEARCH_KEYWORDS` is the single term (configured
terms are also used with the legacy place/radius). The first search fails
the run as before; a later search that fails is logged with its term and
counted as `failed_queries` in the run result, and everything else is
still processed. The BA radius is a
straight-line distance; every workplace within 100 km by car is within it,
so the search discovers a superset and never decides anything. A posting
returned by both searches is processed once.

**Assessment** (`app/services/job_fit_assessment.py`,
`scripts/stage17_job_fit.py assess`), each criterion independent:

| Criterion | Source | Statuses |
|---|---|---|
| skills | persisted matcher v12 analysis (unchanged) | STRONG, PARTIAL, WEAK, NOT_EVALUATED |
| role | title vs configured primary/secondary roles (informational) | PRIMARY, SECONDARY, OTHER, NOT_CONFIGURED |
| experience | matcher experience requirements, stated years | OK, PARTIAL, GAP, UNKNOWN |
| language | posting's German requirement vs the confirmed level | OK, MINOR_GAP (preferred), GAP, MAJOR_GAP (mandatory, >= 2 levels), NO_REQUIREMENT |
| location | stored car route vs the limit | PASS, FAIL (+ relocation note), DISTANCE_UNVERIFIED, UNKNOWN, REMOTE_OK, NOT_CONFIGURED |
| remote | posting text | COMPATIBLE, INCOMPATIBLE (restricted to another state), PARTIAL_REMOTE ("bis zu 100%"), HYBRID, ONSITE, UNKNOWN |
| salary | stated amount (monthly x12) vs expectation | MATCH, BELOW (informational; negotiable), TARIFF, UNKNOWN |
| entry_level | title/text signals | ENTRY_LEVEL, OPEN (no seniority stated -- not negative), MIXED, EXPERIENCED |
| start_date | "ab sofort" vs the configured earliest start date | OK, EARLIER_REQUESTED |
| duplicate | Stage 15 duplicate check | ALREADY_APPLIED, IN_PREPARATION, POSSIBLE_DUPLICATE, CLEAR |

Decision: `BLOCKED` (location FAIL, remote INCOMPATIBLE, ALREADY_APPLIED),
else `NEEDS_VERIFICATION` (distance unverified/unknown, possible duplicate,
skills not evaluated), else `CANDIDATE`. No criterion is folded into a score.

**Car routes.** Only a `job_commute_checks` row makes a distance PASS/FAIL, and only if it starts
at the currently configured origin -- same label AND same coordinates
(otherwise it is stale: DISTANCE_UNVERIFIED).
`record-commute` stores a route the operator measured; `check-commute` asks
OSRM (`router.project-osrm.org`) for the driving route between the stored
origin coordinates and the given workplace coordinates -- only the two
coordinate pairs are sent, only on that explicit command; times are
free-flow. A straight-line distance is printed for information and never
stored. If the optional home-office search fails, the commute search's
results are still processed (logged `bundesagentur_query_failed`).

**Duplicates enforced.** `BewerbungService.generate` and
`ReviewPackageService.create` call `ensure_not_already_applied`: a STRONG
duplicate (same job row, BA reference, URL, fingerprint, or same employer +
role across StepStone/XING/LinkedIn/Indeed/BA) of an application that was
sent (SUBMITTED or later) refuses the draft (`AlreadyAppliedError`, HTTP 409).
Applications still being prepared are not refused.

Nothing in Stage 17 sends mail, contacts an employer or submits anything.
