# E2E job discovery audit (Stage 58)

Date: 2026-11-10. Code: branch `fix/post-release-quality` (main `7c6c61d` + Stage 58 fixes).

## Scope and method — what was and was not run

* **Run live (read-only, official Bundesagentur Jobsuche API):** the nationwide remote track via
  `python -m scripts.nationwide_dry_run` on a **throwaway copy** of the production snapshot
  (migrated to `a7c3e5f1b9d2`, fit cache filled with `stage21_assess_vacancies.py` on the copy).
  Rate limit honoured (1 s between requests): 11 searches, 20 requests, 0 failures.
* **Not run live:** the local Hessen (commute) collector, XING e-mail ingestion and Stepstone. They were
  checked on the data already in the copy and by the existing tests, not by new network calls.
  No claim is made about their live behaviour tonight.
* Nothing was stored in production, no application was prepared, nothing was sent. Candidate
  criteria (Junior Python/Backend, Heusenstamm, B1/B2 German, 45–50 k, from 01.01.2027) are the
  existing configured criteria; none was written to any profile.

## Numbers (nationwide track, copy of the DB)

| Metric | Value |
|---|---:|
| Search hits reported by BA | 1,397 |
| Listings fetched and processed | 744 |
| New rows (not in the snapshot) | 127 |
| Already known, updated (duplicates by reference) | 265 |
| Deferred to a later run (per-run cap) | 79 |
| Skipped: not a remote candidate | 273 |
| Skipped: title not a search target | 327 (counted over hits) |
| Invalid / failed | 0 / 0 |
| Rows before → after | 3,234 → 3,361 |

The 127 new rows: 110 EXCLUDED, 17 MANUAL_REVIEW, 0 PRIMARY/ADJACENT, 0 automatic shortlist.
Fit tier: 109 NOT_SUITABLE, 17 NEEDS_OPERATOR, 1 STRETCH. Work time: 119 FULL_TIME, 5 PART_TIME, 3 CONFLICT.
German: 53 no requirement, 39 major gap, 34 gap to verify, 1 gap. Seniority-blocked: 0. Confirmed full remote: 5 (4 full time).

Whole copy after the run: 3,361 jobs, 660 MANUAL_REVIEW, 2,693 EXCLUDED, 5 ADJACENT, 3 PRIMARY,
`automatic_shortlist` = 0. 11 titles in the copy combine Junior/Trainee with Python/Backend.

Main exclusion reasons (new rows): no developer title signal / not a target role (327 skipped by title
before storing), then NOT_SUITABLE tier (language gap, seniority, role family), then onsite/unverified remote.

## Top new vacancies (real source links; scores are the stored match scores, not a recommendation)

| id | Title | Company | Score | Link |
|---|---|---|---:|---|
| 3260 | Junior Engineer (m/w/d) Schwerpunkt Python | BFI Informationssysteme GmbH | 81 | https://www.arbeitsagentur.de/jobsuche/jobdetail/15297-0gddsy3w-S |
| 3264 | Quereinsteiger als Junior Python-Entwickler (m/w/d) | Leani Business Solutions GmbH | 80 | https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003943814-S |
| 3284 | Datenbank- und Software-Entwickler MongoDB / Python | ArztData AG | 92 | https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003610826-S |
| 3331 | Entwickler Python/Django | Seller Math GmbH | 81 | https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1207682818-S |
| 3276 | Softwareentwickler Backend / Python | ALTEN GmbH | 80 | https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003678263-S |

(Leani already has a SUBMITTED application, #25, in the data; the duplicate guard must stop any second
preparation — do not apply twice.)
None of these is cleared for automatic preparation; each needs the operator's reading. The 5 listed rows
had no fit-tier row yet because the cache on the copy was filled before the run; the next
`stage21_assess_vacancies.py --apply` computes it.

## Pipeline stages checked

Collector → normalisation (BA reference ids kept, real `arbeitsagentur.de` detail URLs, external-apply links
preserved) → deduplication by source reference (265 updates, no duplicates created) → scoring → fit assessment
→ triage → shortlist gate (0 admitted) → manual review (`/ui/review`, 684 items on the copy after the Stage 58 fix).

## Limits

* One live run on one day; BA listings change daily.
* Commute/Hessen results and XING ingestion not re-run live (see scope).
* The classifier is heuristic; Vollzeit confirmation comes from text or the BA flag only.
