# Junior Python / Backend vacancies waiting for manual review

Read-only extract from a **copy** of the production database (taken with the SQLite backup API on
2026-11-09, migrated to the chain head on the copy only; production was not touched). The data is
exactly what JobTriage stored; nothing was invented. `—` or `UNKNOWN` means the database does not
know.

How the group is selected: a title with a level word (junior, trainee, einsteiger ...) and
python/backend, without an administration/support word (`review_queue.is_junior_python_title`). It
is a title heuristic, not a judgement about the posting. All 8 have fit tier `NEEDS_OPERATOR`, and
none has a Bewerbung of its own (`applications.job_id` is empty for all eight).

Publication date: JobTriage stores only when it **first saw** the vacancy (`first_seen_at`), not the
source's publication date; the posting itself has to be opened for that.

XING links (`xing.com/m/...`) are per-recipient tracking redirects. They were **not** opened by this
work. Open them yourself from the e-mail or the XING app; do not let a script follow them.

| # | JT id | Title | Employer | City | Source | Link |
|---|-------|-------|----------|------|--------|------|
| 1 | 705 | (Junior) Software Developer Python (m/w/d) | FERCHAU GmbH Niederlassung Karlsruhe City | Karlsruhe, Baden | Bundesagentur | https://www.arbeitsagentur.de/jobsuche/jobdetail/12265-446410_JB5275915-S |
| 2 | 752 | (Junior) Backend-Entwickler (m/w/d) | FERCHAU GmbH Niederlassung Karlsruhe City | Karlsruhe, Baden | Bundesagentur | https://www.arbeitsagentur.de/jobsuche/jobdetail/12265-445951_JB5275886-S |
| 3 | 1337 | Junior Backend Engineer (m/w/d) | Airbus Defence and Space GmbH | Immenstaad am Bodensee | Bundesagentur | https://www.arbeitsagentur.de/jobsuche/jobdetail/16947-970420688-S |
| 4 | 347 | (Junior) Backend Engineer (m/w/d) | RUBINLAKE GmbH | Frankfurt am Main | XING | https://www.xing.com/m/CrV68YHN5uRnwD67EiFNgM (open manually) |
| 5 | 328 | (Junior) PHP Backend Entwickler (m/w/d) | Jobriver.de | Frankfurt am Main | XING | https://www.xing.com/m/biNWE1Wac8woKkqK7HsggZ (open manually) |
| 6 | 324 | (Junior) Python Entwickler (m/w/d) | Jobriver.de | Darmstadt | XING | https://www.xing.com/m/biNWE1Wac8woKkqK7HsggO (open manually) |
| 7 | 310 | (Junior) Backend Engineer (m/w/d) | Instaffo GmbH | Frankfurt am Main | XING | https://www.xing.com/m/W6FDAAgHNx9M6fUXcQEdgK (open manually) |
| 8 | 292 | Junior) Python Softwareentwickler KI (m/w/d) | ep group | Karlsruhe | XING | https://www.xing.com/m/sVVftvH48sqUgs6fcbJ-gL (open manually) |

(Title of #8 is stored with the missing opening parenthesis exactly as the source delivered it.)

| JT id | First seen | Working time (as stored) | Remote / Hybrid / Onsite | Score / recommendation | Fit tier | Why it waits (MANUAL_REVIEW) |
|-------|-----------|--------------------------|--------------------------|------------------------|----------|------------------------------|
| 705 | 2026-10-22 | **UNKNOWN in the posting** (see note A) | UNKNOWN | 82 / APPLY | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`: ~122 km straight line from Heusenstamm Bahnhof, no confirmed route; also `LANGUAGE_REVIEW` |
| 752 | 2026-10-22 | **UNKNOWN in the posting** (see note A) | UNKNOWN | 56 / MAYBE | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`: same distance; also `LANGUAGE_REVIEW` |
| 1337 | 2026-10-23 | **UNKNOWN in the posting** (see note A) | UNKNOWN | 41 / SKIP | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`: workplace known, car route not yet checked (limit 100 km) |
| 347 | 2026-10-20 | Vollzeit (stub text: "Beschäftigung: Vollzeit"); salary 42.500–57.500 € as stated | UNKNOWN | 45 / NEEDS_ENRICHMENT | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`; also `NEEDS_ENRICHMENT` (52-character stub, no skills extractable) |
| 328 | 2026-10-20 | Vollzeit (stub); salary 40.000–55.000 € | UNKNOWN | 45 / NEEDS_ENRICHMENT | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`; also `NEEDS_ENRICHMENT` |
| 324 | 2026-10-20 | Vollzeit (stub); salary 44.500–59.000 € | UNKNOWN | 45 / NEEDS_ENRICHMENT | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`; also `NEEDS_ENRICHMENT` |
| 310 | 2026-10-20 | Vollzeit (stub); salary 45.000–60.000 € | UNKNOWN | 45 / NEEDS_ENRICHMENT | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`; also `NEEDS_ENRICHMENT` |
| 292 | 2026-10-20 | Vollzeit (stub); salary 48.500–67.000 € | UNKNOWN | 45 / NEEDS_ENRICHMENT | NEEDS_OPERATOR | `ROUTE_REVIEW_REQUIRED`; also `NEEDS_ENRICHMENT` |

**Note A (a finding, not fixed here).** The work-time rule labels 705, 752 and 1337 `PART_TIME`
("part-time only"). The only evidence is benefit wording — "Teilzeitmöglichkeiten" (705, 752) and
"part-time options" (1337) — which says part time is *possible*, not that the job is part time only.
These three are therefore shown as UNKNOWN above. The rule (`classify_work_time`) should be fixed in
a separate change, because it alters the rules hash and invalidates every cached assessment.

## What to clarify and recommendation

| JT id | What to clarify | Existing Bewerbung | Recommendation |
|-------|-----------------|--------------------|----------------|
| 705 | Is the Karlsruhe place reachable (~122 km, no route check) or is remote/hybrid offered? Is Vollzeit possible? German level asked? | None for this vacancy. **FERCHAU GmbH Niederlassung Giessen has Bewerbung #22 ((Junior) Informatiker, RECEIVED)** — same company group, check before applying again. | **Check now** (best score, full 3 090-character posting). |
| 752 | Same as 705; weaker skill match (PARTIAL). | None; same FERCHAU note as above. | Check now, together with 705 (same employer, same branch). |
| 1337 | Car route Heusenstamm → Immenstaad am Bodensee not yet checked (limit 100 km); no remote information. | None. | Check now, low priority (route unverified, no remote information). |
| 347 | Open the XING posting by hand: skills, place, remote, hours. Nothing is known beyond title, employer, place and salary band. | None. | **Additional information required** (stub only). |
| 328 | As 347. Title contains **PHP**, not Python — check against your skills. | None. | Additional information required. |
| 324 | As 347. | None. | Additional information required. |
| 310 | As 347. | None. | Additional information required. |
| 292 | As 347. Karlsruhe place (see 705 for distance). | None. | Additional information required. |

No application, status, message or external request was created or made while preparing this list.
