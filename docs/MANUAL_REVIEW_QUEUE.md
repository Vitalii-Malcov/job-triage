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

The whole queue is sorted **before** it is paged, so page 2 never contains something that belongs
before page 1: Junior Python / Backend titles first, then developer roles, adjacent roles, other
roles, and administration/support-mixed titles last; inside a group fully remote before hybrid
before the rest, then the higher score, then the newer vacancy. Pages are keyset pages on that sort
key (`cursor=`), so a vacancy is never listed twice and a vacancy arriving between two requests does
not shift the next page. A malformed cursor is ignored (first page).

Filters: profession (Junior Python/Backend, developer, adjacent, other, admin/developer hybrid),
reason, source. Each entry shows the work format, the working time (Vollzeit/Teilzeit as classified
from the posting or the source flag), the plain-language reason, what to clarify, the source link and
an "Existing Bewerbung" mark when an application touches the vacancy (an already applied vacancy is
not listed at all).

## Cost and limits

* The queue is built from the Stage 21 assessment cache (`job_fit_assessments`). A row is used only
  while its stored input key equals the current one (SQL + hashing, `fit_cache.current_keys`); a
  fresh row is turned into an entry from its stored criteria with **no live assessment**, a fresh
  confirmed barrier or applied vacancy is dropped. About 60 % of the request time is the freshness
  check itself (hashing every vacancy row), so the cost is a flat ~0.7-1 s per page regardless of
  depth.
* A stale or missing cache row is never trusted and never hides a vacancy. At most
  `STALE_LIVE_BUDGET` (60) of them are assessed live per request (Junior Python/Backend titles
  first, then newest); the page states how many were **not** recomputed and that they may be missing
  from the list. After a rules/profile change the whole cache is stale; refilling it is an explicit
  offline step: `scripts/stage21_assess_vacancies.py --apply` (never from an HTTP request).
* If profile, rules or applications change while a page is built, the page is built again without
  the cache (bounded by the same live budget).
* The route is a plain `def`: FastAPI runs it in the thread pool, so it does not block the event loop.
* The page writes nothing (read-only session) and contacts nobody; links are shown, never opened.
* Measured on a copy of the production database (3 234 vacancies): 682 entries, 8 Junior
  Python/Backend. See `docs/FINAL_RELEASE_READINESS.md` for before/after timings.
* Earlier breakdown of 673 entries: 632 Bundesagentur and 41 XING (title-only stubs that stay visible for analysis);
  by main reason `ROUTE_REVIEW_REQUIRED` 304, `REMOTE_UNVERIFIED` 194, `STATED_GAP` 96,
  `ROLE_UNCLEAR` 41, `DATA_REVIEW_REQUIRED` 18, `NEEDS_ENRICHMENT` 16, `HYBRID_ADMIN_ROLE` 3,
  `THIN_SKILL_EVIDENCE` 1; `LANGUAGE_REVIEW` is also tagged on 256 of them.
* The Junior Python/Backend group is a title test (a level word and python/backend in the title); it
  is a heuristic, not a judgement about the posting.

## Not done

* Telegram: no queue message was added. Changing what Telegram shows is possible without touching
  sending or approval, but it is a separate decision.
* No action buttons by design.

## Known limits (Codex review, accepted and disclosed)

* **Cold cache is incomplete by design.** Stale rows beyond `STALE_LIVE_BUDGET` are neither assessed
  nor reachable by paging; the page says how many were skipped and that the list may be missing
  vacancies. The way out is the explicit offline refill, not a hidden recompute in the request.
* **Per-request work is O(vacancies), not O(page).** Every request hashes the matching vacancy rows
  (freshness proof) and builds and sorts the whole queue before applying the cursor. Measured at
  ~0.7-1 s for 3 234 vacancies; it grows linearly. Making it O(page) needs stored, queryable sort
  fields (schema change + migration), which is a separate decision.
* **No single snapshot.** Keys, vacancy rows and cached assessments are read in one session but
  SQLite gives no snapshot across statements; a collector write in that window can show one entry
  with its previous tier. The list is advisory: every gate and the vacancy page recompute.
* **Retry can double the live work.** If profile/rules/applications change mid-request the page is
  rebuilt without the cache, so one request may run up to 2 x `STALE_LIVE_BUDGET` live assessments.
