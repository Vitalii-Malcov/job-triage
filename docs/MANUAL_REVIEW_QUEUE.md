# Manual review queue

`/ui/review` ("Needs review" in the navigation) lists the vacancies that wait for the operator's
decision, with the reason and what exactly to clarify. It reuses the existing read-only UI, the
fit assessment and the triage category. No new table, no migration, no new setting.

## What it is not

Being listed does **not** allow an automatic application. The page has no form and no action: it
lists, explains and links. It cannot prepare, approve or send anything, create an application or
change a status. The fit tier, `AUTO_PREPARE_TIERS`, the late re-check, approval and duplicate
protection are untouched and remain the only rules about preparation. The UI session is a
read-only database connection, so a write would fail even if code tried one.

## Who is in the queue

* Triage category `MANUAL_REVIEW`: `THIN_SKILL_EVIDENCE`, `ROUTE_REVIEW_REQUIRED`,
  `REMOTE_UNVERIFIED`, `HYBRID_ADMIN_ROLE`, `DATA_REVIEW_REQUIRED`, `ROLE_UNCLEAR`, `STATED_GAP`.
* Vacancies whose description is missing or too short (`NEEDS_ENRICHMENT`) with a Python / backend /
  developer-like title that triage excluded only for lack of skill evidence. A title-only stub is
  unknown, not unsuitable; nothing about its skills, hours or place is assumed.
* `LANGUAGE_REVIEW` is a secondary tag (German asked above the confirmed level) and also a filter.

Never in the queue: a confirmed barrier (fit tier `NOT_SUITABLE`: administration, support, senior,
out of reach, confirmed language barrier ...) and vacancies already applied for.

## Order

Junior Python / Backend titles first (selected in SQL, so they are reached directly), then all other
vacancies. Inside a page: Junior Python/Backend, then developer roles before adjacent roles before
other, administration/support-mixed titles last, then fully remote before hybrid before the rest,
then score, then newer first. Developer, adjacent and hybrid-admin roles carry separate labels and
are never mixed under one.

The ordering inside the "other" group is per page, not global (see limits).

## Cost and limits

* Opening the page assesses live at most `SCAN_BUDGET` (200) vacancies, like the computed filters of
  the vacancy list (about 30 ms each on SQLite, so a page can take several seconds). It never
  recomputes the database: the next page is a link.
* If the assessment cache is in use, a vacancy whose **fresh** cached assessment is `NOT_SUITABLE`
  is skipped without a live assessment. A stale or missing cache row never hides a vacancy.
* Measured on a copy of the database (3 368 vacancies): 663 entries, 8 of them Junior Python/Backend
  titles; a full pass over the whole database takes about 100 s, which is why the page is paged.
* Because the page is built from a scan, the queue size is not shown as a single number, and the
  "other" group is sorted within each page only.
* The Junior Python/Backend group is a title test (a level word and python/backend in the title); it
  is a heuristic, not a judgement about the posting.

## Not done

* Telegram: no queue message was added. Changing what Telegram shows is possible without touching
  sending or approval, but it is a separate decision.
* No action buttons by design.
