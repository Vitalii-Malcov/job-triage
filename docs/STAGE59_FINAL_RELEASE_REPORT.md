# Stage 59 — final release report

**Result: NOT MERGED — OPEN BLOCKERS.** PR #57 stays open. Reason: the independent Codex review never
returned ACCEPT (see §5), and the merge condition "no unclosed P2 that can wrongly open auto-prepare"
is therefore not met. Production was not touched.

Branch `fix/post-release-quality`, PR #57 → `main`. Head at the time of writing: `874e4ef`.

## 1. Defect
`Teilzeit, später Vollzeit möglich` (and 20+ relatives) was classified `FULL_TIME`: a *future*
full-time possibility counted as the vacancy's *current* terms. `FULL_TIME` is the only unsafe error,
because `AUTO_PREPARE` requires confirmed `FULL_TIME` + duplicate `CLEAR`. Reproduced before fixing: 7 of the listed wordings read `FULL_TIME` although the current terms are part time
(`Teilzeit, später Vollzeit möglich`, `…mit späterer Vollzeitoption`, `…Vollzeit nach Einarbeitung`, `Zunächst/Aktuell Teilzeit, später
Vollzeit`, `Part-time initially, full-time possible later`, `Part-time role with full-time opportunity`), plus
`Vollzeit perspektivisch möglich` (should be UNKNOWN) and `Vollzeit ab 2027, aktuell Teilzeit`.

## 2. Fix (`app/agents/job_fit_signals.py`)
Rule: **a future full-time possibility is not evidence of current full time.**
* A full-time mention is *deferred* (discarded; the vacancy is held) when its **sentence** holds a
  forward-looking marker (später, danach, erst, nach … Einarbeitung/Probezeit/Monaten, after …, ab/from a
  year ≥ 2027, ab Januar / next month / ab dem 2. Monat …). A sentence is ended by `;`, newline, or a
  period before whitespace + capital letter; dates, ordinals and abbreviations stay inside it.
* *Soft* wording (Option/Übergang/Perspektive auf Vollzeit, `Vollzeitoption`, `Teilzeit, Vollzeit`) only
  matters next to a **current** part-time statement (non-offer, non-negated `Teilzeit`, or stated weekly
  hours < 35 in the same sentence), which then wins → `PART_TIME`. Real offers such as
  `Option auf 4 oder 5 Tage Vollzeit (32–40 Stunden)` stay `FULL_TIME`.
* A marker after `Vollzeit` that qualifies `Teilzeit` (`Vollzeit, später Teilzeit möglich`) does not defer.
* Staff statements (`Unsere Kollegen arbeiten in Teilzeit/Vollzeit`) are not this vacancy's terms; applicant
  address (`Sie arbeiten …`) and recruiting wording are not staff statements.
* An exhausted scan budget answers `PART_TIME` (held), never `FULL_TIME`.
* Unchanged: BA-flag CONFLICT logic (`work_time_criterion`), `AUTO_PREPARE` rule, seniority / skills /
  language / remote / commute / duplicate gates, Manual Review.

## 3. Test phrases (all in `tests/test_work_time_future_fulltime.py`, 100+ cases)
| Wording | Before | After |
|---|---|---|
| Teilzeit, später Vollzeit möglich | FULL_TIME | PART_TIME |
| Teilzeit mit späterer Vollzeitoption | FULL_TIME | PART_TIME |
| Teilzeit, Vollzeit nach Einarbeitung möglich | FULL_TIME | PART_TIME |
| Zunächst Teilzeit, später Vollzeit / Aktuell Teilzeit, später Vollzeit | FULL_TIME | PART_TIME |
| Part-time initially, full-time possible later | FULL_TIME | PART_TIME |
| Part-time role with full-time opportunity | FULL_TIME | PART_TIME |
| Vollzeit perspektivisch möglich | FULL_TIME | UNKNOWN |
| Nur Teilzeit, Vollzeit später möglich | PART_TIME | PART_TIME |
| Vollzeit, Teilzeit auf Wunsch möglich | FULL_TIME | FULL_TIME |
| Vollzeit oder Teilzeit; Vollzeit (40h) oder Teilzeit (30h) | FULL_TIME | FULL_TIME |
| Teilzeitmöglichkeiten; Part-time options available | UNKNOWN | UNKNOWN |
| Unsere Mitarbeiter arbeiten Voll- und Teilzeit | PART_TIME | PART_TIME (held; see §9) |
| BA flag true + `Teilzeit, später Vollzeit möglich` | FULL_TIME | CONFLICT |
Also covered: #705/#752/#1337 stay UNKNOWN; #551/#1261 stay in the queue; #49/#2463/#547/#1297/#1342/#1707
(real full-time wording that a first draft wrongly held) stay `FULL_TIME`; flag true/false/null; hostile
repetition stays bounded.

## 4. Data before/after (fresh backup-API copy of production, migrated copy only)
Copy `job_triage-20261110T062505Z-stage59.db` (sha256 `6d0b2be7…f5a3`), migrated `d6e9a2b4c8f1 →
a7c3e5f1b9d2`; all 3,234 jobs assessed live with the old code (worktree at `ef28fe8`) and the final code.
| Metric | Before | After |
|---|---:|---:|
| FULL_TIME | 677 | 677 |
| PART_TIME | 144 | 144 |
| UNKNOWN | 2,413 | 2,413 |
| CONFLICT | 0 | 0 |
| Manual Review Queue | 684 | 684 |
| Junior Python/Backend review | 8 | 8 |
| Auto-prepare eligible | 1 | 1 |
| New Bewerbungen | 0 | 0 (25 applications / 54 events kept) |

**No vacancy changed classification** — the defect is real for future ads but latent in the current data.
(`full_time_offered` is NULL on the copy, so the BA flag path was tested synthetically only.)
Two intermediate drafts did change real jobs (#547/#1297 `ab 32 Stunden pro Woche`; #1342, #1707, #49,
#2463 `Option`/`Perspektive`); the real-data run caught them and they are now regression tests.
The single auto-eligible job (#2005, Softwaretester für Flugsicherungssysteme) is unchanged and is *not*
cleared by this report — a higher score is not a confirmation of fit.

## 5. Codex verdict — **REJECT (no ACCEPT obtained)**
27 read-only rounds. Every round returned REJECT with 1–10 findings, all in the same family: wording
variants (German/English mirrors, date and duration shapes, option/transition phrasing, negation and
staff scope) in which a *future-only* or *option* full-time remark still reads `FULL_TIME`. Rounds 1–26
were fixed one by one; round 27 asked for an exhaustive sweep (10 classes) and 4 cheap classes were fixed afterwards. Not re-reviewed after that last
commit. Codex's own closing judgement: *"acceptable for manual triage with AUTO_PREPARE disabled; not yet
acceptable for enabling automatic preparation."*
Still open from the sweep (not fixed): `Full-time starting 2027 / in the second month / from the seventh
month`; `ab 01.01.27`, `from mid-2027`, `Febr.`; `6-Monats-Probezeit`, `six-month period`;
`full-time afterwards`, `possibility of full-time`, `upon completion of probation`; `Vollzeit bleibt eine
Option`, `conversion possible`, `Vollzeit nach Vereinbarung möglich`; `Vollzeit wird später Teilzeit
ablösen`. The tail of wording variants is open-ended; a regex stack will not close it.

## 6. Tests and CI
* Targeted work-time/queue tests: 233 passed. Ruff check and format clean.
* Full local pytest on the final code (`874e4ef`): **8,796 passed, 168 skipped, 0 failed** (44 min).
* GitHub CI on `874e4ef`: `test` (Linux), `scheduler-postgres` (PostgreSQL), `docker` — all SUCCESS.
* Alembic rehearsal on a copy: `a7c3e5f1b9d2 → d6e9a2b4c8f1 → a7c3e5f1b9d2`, integrity `ok`, 3,234 jobs /
  25 applications intact. No schema change in this stage.
* No outbound sending, no Gmail/Telegram/Bewerbung was triggered.

## 7. PR / merge status
PR #57 OPEN, MERGEABLE, CI green on `874e4ef`; **not merged** (§5). `main` unchanged. No force push, no
`--admin`. Push was a fast-forward of `fix/post-release-quality` (`ef28fe8 → 874e4ef`).

## 8. Production integrity
`runtime/job_triage_runtime.db` was only read via the backup API (sha prefix `aef7383ef194fd6a` before and
after, identical to the Stage 58 note). No migration, cache refresh, scheduler, `.env` or flag change.

## 9. Open risks
1. The work-time classifier is heuristic; §5 lists wording that still yields `FULL_TIME`. **Keep
   `AUTO_PREPARE` disabled.** Manual review is unaffected.
2. Several rules are deliberately conservative (any timing word in the sentence holds the vacancy), so some
   genuine full-time ads become UNKNOWN. Safe direction; none in the current data.
3. Staff-statement ads (`Unsere Mitarbeiter arbeiten Voll- und Teilzeit`) read PART_TIME/UNKNOWN — held.
4. The BA `full_time_offered` path has no production data yet (column is new).

## 10. Next action
Operator decision on PR #57. Two honest options:
* **Merge as is** — only if the operator accepts §5 with `AUTO_PREPARE` staying off (the diff is strictly
  safer than `main`, CI green, production-neutral). This needs the operator's explicit OK; this stage's
  merge condition 2 was not met, so it was not done automatically.
* **Close the family properly** — replace the phrase heuristics by a positive-evidence rule for
  `AUTO_PREPARE` (e.g. require a labelled field `Arbeitszeit: Vollzeit` / `Vertragsart: Vollzeit` or the
  BA flag `true` with no part-time word in the sentence), a new design decision outside this stage.
Rollout steps: `docs/STAGE59_ROLLOUT_RUNBOOK.md` (nothing in it has been executed).
