# Nationwide remote discovery — report

Branch `feat/nationwide-remote-discovery`, on top of PR #48 (`1ada253`). Production was not
touched: no merge, no working-SQLite write, no production migration, no cache refill, no
`jobs.score` update, no change to applications or statuses, no Gmail/Telegram/Bewerbung sent, no
scheduler job enabled, no `.env` or secret change, no backup deleted, no force push.

## 1. What the audit found (Stage A/B)

| Source | Scope | Remote filter | Employment type | Full text | Auth / limits |
|---|---|---|---|---|---|
| Bundesagentur Jobsuche API (unofficial, shared client id) | nationwide when `wo` is omitted | none usable | `arbeitszeitVollzeit` flag in search results | detail endpoint, one request per posting | no personal key; no documented rate limit; paced and capped here |
| XING e-mail alerts | whatever the alerts contain | – | – | **title only** | IMAP App Password; links are per-recipient tracking redirects and are never followed (project rule) |

Measured live on 2026-11-08 (read-only GET requests, ≈ 70 in total, paced):

* `arbeitszeit=ho` returns **0** results; `arbeitszeit=vz;ho` behaves as OR (2 487 vs 2 290 for
  "Python"). There is no remote filter. The `homeofficemoeglich` flag is set on a minority of
  postings (for "Junior Python": true 11, false 19, unset 58 of 89) and is employer-set.
* The search is fuzzy: "Junior Data Engineer Python" returns 312 hits, "Python Entwickler" 268.
  Narrow terms are subsets of broad ones, so only terms that added vacancies were kept (11 terms).
* **Correction to my earlier report:** a nationwide search already existed (`remote_work_scope =
  GERMANY`, no `wo`). It was limited to 10 pages per term, ran only with the configured broad
  keywords, and kept only postings with `homeofficemoeglich = true`, which drops most of them.
  The statement "the collector searches only by radius" was inaccurate. The database copy
  already held 243 postings more than 150 km from Heusenstamm.

## 2. What was implemented

* **TRACK B search** (`app/services/nationwide_discovery.py`, `search_strategy.nationwide_remote_queries`):
  11 development terms, no place, no radius, employment only, title pre-filter
  (`is_search_target_title`; support/admin titles never pass), up to 5 pages per term.
* **Home-office flag is a helper only.** It no longer excludes anything. A posting that is new to
  the database is stored only if its text mentions remote work, or the text says nothing and the
  flag is true (`is_remote_candidate`). On-site/hybrid postings far from home are not stored; a
  posting already in the database is always updated, never dropped.
* **Employment type:** the BA flag `arbeitszeitVollzeit` is stored (`jobs.full_time_offered`,
  additive Alembic `a7c3e5f1b9d2`) and used by `work_time_criterion` only where the text is
  silent (text wins; `false` → PART_TIME, i.e. review, never exclusion).
* **Request control (`RequestGate`):** one gate per run paces every HTTP attempt (search, detail,
  retries) at ≥ 1 s, caps search attempts (60) and detail attempts (400), and caps stored
  vacancies (150). A transient detail failure stores the posting without text for later
  enrichment instead of rejecting it.
* **Incremental discovery:** the watermark is the last *complete* nationwide step (nothing
  deferred, truncated, failed or transiently without text), found by querying only runs that
  contain that step. The BA window is 7 or 14 days (the only values the API honours), else full.
  Candidate order is a daily weighted shuffle so caps do not always cut the same postings.
* **Scheduling:** `LOCAL_DISCOVERY_ENABLED` (default **true**: today's behaviour) and
  `NATIONWIDE_REMOTE_DISCOVERY_ENABLED` (default **false**), independent interval, step inside the
  existing automation cycle (existing lease, heartbeat, per-step isolation; no parallel run).
* **Dry-run tool:** `jobtriage nationwide-dry-run` / `scripts/nationwide_dry_run.py`
  (refuses the production, pilot and backup databases).

Not changed: thresholds, shortlist gate, remote classifier rules (PR #48), `jobs.score`, anything
that sends.

## 3. Dry-run (copy of the 3 234-vacancy database, live BA API, nothing sent)

| Metric | Value |
|---|---:|
| Search requests / terms | 21 / 11 |
| Hits returned by BA (sum over terms, with overlaps) | 1 425 |
| Postings that passed the development-title pre-filter and were examined | 762 |
| Dropped by title pre-filter (not development roles) | 331 |
| Already in the database (updated, **no duplicate row**) | 276 |
| Rejected as not remote candidates (on-site / hybrid, not stored) | 266 |
| Deferred by the detail ceiling (to the next run) | 86 |
| **New unique vacancies stored** | **134** |
| – with entry-level wording | 9 |
| – senior title / experience required | 88 |
| – German C1 / MAJOR_GAP | 42 |
| – stated Vollzeit (text or BA flag) | 123 |
| **Confirmed FULL_REMOTE among the new** | **5** (4 Vollzeit) |
| REMOTE_UNVERIFIED among the new | 129 |
| New PRIMARY / ADJACENT | **0** |
| New MANUAL_REVIEW / EXCLUDED | 14 / 120 |
| Admitted to the automatic shortlist | **0** (still 2 of 3 368) |
| Violations (admin/support or NOT_SUITABLE in the shortlist) | 0 |

Whole database, before → after: rows 3 234 → 3 368; confirmed FULL_REMOTE 68 → 73; confirmed
**Vollzeit + FULL_REMOTE 8 → 14** (the BA flag supplies Vollzeit where the text is silent);
PRIMARY+ADJACENT 8 → 8; automatic shortlist 2 → 2.

**New unique Junior Python/Backend vacancies passing the geographic filter: 0.** The concrete
cases:

* "Quereinsteiger als Junior Python-Entwickler (m/w/d)", Leani Business Solutions, Vollzeit,
  Homeoffice 100 %: stored in the first rehearsal variant (before the not-remote-candidate
  check, which now keeps it out), where it **already matched application 25 (SUBMITTED)** →
  `duplicate = ALREADY_APPLIED`, EXCLUDED. This shows the history check works across tracks.
* "Junior Engineer (m/w/d) Schwerpunkt Python", BFI Informationssysteme, Ilmenau, ref
  `15297-0gddsy3w-S`: confirmed FULL_REMOTE, Vollzeit, entry level, but German B2–C1 stated; the
  existing scorer gave `SKIP` at score 81 (the "fewer than two skill signals" guard), so no match
  is prepared and the role stays unassessed. **Operator decision**, not changed here.
* "Python Developer mit fachlicher Koordination (m/w/d)", digatus personal, Ilmenau, ref
  `19886-USQ4F38W70GZRDWT-S`: FULL_REMOTE, Vollzeit, but experience required and team
  coordination → MANUAL_REVIEW.
* "Entwickler Python/Django (w/m/d)", Seller Math, Nonnenhorn, ref `10000-1207682818-S`: Vollzeit,
  open level, remote unverified → MANUAL_REVIEW (read the posting).

I do not claim that these vacancies are still active; BA lists them as published on the dates the
API returned, and only a later run can tell.

An earlier rehearsal variant that excluded postings by the BA flag stored the same 134 rows; the
flag-free design costs more detail requests (hence the 86 deferred) but loses nothing.

## 4. Remote classification (Stage D)

The PR #48 classifier is the baseline and was not changed. No new evidence about its quality was
produced beyond the rehearsal above: 5 confirmed FULL_REMOTE among 134 stored, all others
REMOTE_UNVERIFIED or worse. The structural limit stands: reading free-text ads with patterns has
no finish line (nine Codex rounds on PR #48 each found new phrasings). **Prototype not built.**
A model-based extractor would need: a fixed JSON schema (work_format, location_restriction,
office_days, evidence quote, confidence), validation that the quote occurs in the text, fall-back
to the regex result and `MANUAL_REVIEW` when the model is unavailable or unsure, no candidate
personal data in the prompt (only the posting text), a cost estimate (≈ 250 postings × ~1 500
tokens per backfill, then a few dozen per day) and the operator's explicit permission before any
external call. It must never decide to send an application.

## 5. Deduplication (Stage E)

Both tracks use the same persist path (`score_and_persist` → fingerprint / `identity_key`). In the
rehearsal 276 postings found again were updated in place, none duplicated. Tests prove a posting
found by both tracks is one row whose id, status and `first_seen_at` are unchanged, and that an
already stored posting is updated even if it would no longer qualify. Nothing in this change
writes applications, events, review packages or approvals.

## 6. XING stubs (Stage G)

118 XING rows, all `NEEDS_ENRICHMENT`, all links `www.xing.com/m/<token>` (per-recipient tracking
redirects, the documented incident class); 45 have a junior-and-development-like title (broader
pattern than the earlier "about 34"; includes SAP/Java/Linux roles). They are never admitted to
automatic preparation (title-only). No automated enrichment was added or is safe. A list for
manual opening (id, title, company, location, link) is kept **outside the repository** because the
links are personal: `E:\Projects\jt-match-work\xing_manual_enrichment.csv`.

## 7. Tests

* Targeted (collector, automation, search, fit, cache, remote, matching): 1 305 passed.
* New `tests/test_nationwide_discovery.py`: 37 tests (nationwide request parameters, pagination
  and ceilings, pacing across collectors and detail calls, retries charged, home-office flag as a
  helper, title filter, source Vollzeit flag, dedup between tracks, history kept, caps/deferral,
  failure isolation, watermark rules, daily shuffle, cycle flags and defaults, nothing sent).
* Full SQLite suite: **8 419 passed, 168 skipped, 2 failed** on the first complete run; both
  failures were new-column fixtures (schema column list, cache-key fixture) and are fixed; the two
  modules were re-run: 147 passed. The full suite was not repeated afterwards (44 minutes).
* PostgreSQL (throwaway `postgres:16`, one pytest process): `tests/integration` +
  migration gate **195 passed**; `alembic upgrade head` → `downgrade -1` → `upgrade head`
  clean; single head `a7c3e5f1b9d2`.
* Ruff check and format: clean. Docker smoke: not run (no Docker file changed).

## 8. Codex (read-only, cannot run pytest)

| Round | Verdict | Findings |
|---|---|---|
| 1 | REJECT | 3 P1 (rejected postings used the new-vacancy cap; incomplete runs advanced the watermark; missing detail treated as evidence against remote), 4 P2 (request budget escaped failures/retries; pacing reset per term and skipped detail calls; BA flag as hard exclusion; watermark lost after 50 unrelated runs). Dedup, shortlist gate, risk to applications: ACCEPT. |
| 2 | REJECT | 1 P1 (rejections can starve deferred postings), 2 P2 (rows saved without text advance the watermark; detail cap counted postings not HTTP attempts). |
| 3 | REJECT | P1 starvation still deterministic with strict priority groups; P2 `detail_unavailable` only on created rows. |
| 4 | REJECT (formal) | **No defect listed.** Areas 1–4 and 6 ACCEPT; area 5 (tests) "BLOCKED: pytest cannot start in the sandbox". |

Every valid finding was fixed. The final verdict is **not an ACCEPT**: round 4 found nothing to
fix but, in its own words, the merge gate is unverified because it cannot run the tests; those
were run here (section 7). Accepted limitation: no persistent rejection ledger (needs a table);
mitigation = daily weighted shuffle, ceilings and rollout advice.

## 9. Risks and open decisions

1. **Only 2 of 3 368 reach the automatic shortlist.** The pool of junior Python/backend roles
   that are fully remote *and* open to a B1 speaker is tiny; more discovery adds mostly senior,
   C1 or on-site postings. Do not expect many; do not lower thresholds to compensate.
2. The "fewer than two skill signals → SKIP" guard hides at least one genuine candidate
   (Ilmenau, ref `15297-0gddsy3w-S`). Decide whether to relax it for FULL_REMOTE + entry level.
3. German "B2–C1" wording is `MAJOR_GAP`/`GAP`; confirm how strictly that should apply.
4. First backfill needs two cycles (86 deferred at the default ceilings); raise
   `NATIONWIDE_REMOTE_MAX_DETAIL_PER_RUN` to ≈ 700 if one run is preferred.
5. BA API is unofficial and could change shape; the shared client id is public, not a secret.
6. The fit cache goes stale (new column in every key); recompute offline when convenient.
7. XING stubs need a person.

Rollout steps: `docs/NATIONWIDE_REMOTE_DISCOVERY_ROLLOUT.md`.

PARTIAL COMPLETION — OPEN BLOCKERS

Open blockers: (a) no Codex ACCEPT (formal REJECT in round 4 only because its sandbox cannot run
tests); (b) the remote classifier remains heuristic (no model prototype); (c) the full SQLite
suite was not repeated after the two fixture fixes (affected modules re-run green).
