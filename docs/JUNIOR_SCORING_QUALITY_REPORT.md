# Junior scoring quality — report

Branch `fix/junior-scoring-enrichment`, stacked on PR #49 (`feat/nationwide-remote-discovery`),
which is stacked on PR #48 and PR #47. Nothing here was run against the production database,
scheduler or `.env`; every measurement is on a copy.

Final status: see the last line.

## 1. Exact cause of the false SKIP

Reproduced on a copy of the database with `EvaluationTrace` (vacancy 3294, *Junior Engineer
(m/w/d) Schwerpunkt Python*, BFI Informationssysteme):

```
posting:TARGET
base_score:81:MAYBE
seniority:UNKNOWN:JUNIOR
role:UNKNOWN:SOFTWARE_DEVELOPMENT
low_cardinality_downgrade:1            <- Stage 11E: one distinct skill signal ("python")
sparse_rescue_not_applicable:role:UNKNOWN
final:81:SKIP
```

The "fewer than two skill signals" rule is `MINIMUM_UNIQUE_EVIDENCE_SIGNALS = 2` in
`app/agents/evidence_cardinality_classifier.py`, applied in `evaluate_job_score` (Stage 11E)
to an APPLY/MAYBE result. It exists because a single coincidental keyword (a training course that
says "Python" once) used to reach an automatic APPLY. It turns a thin result into SKIP; Stage 11C
then lifts it back to MAYBE (human review) when the title is a software role and no concrete
must-have is missing.

The rescue was the gap. It needed `classify_title_relevance == RELEVANT`, and "Engineer" alone is
deliberately not a software word, so *Junior Engineer … Python* was `UNKNOWN` and stayed SKIP.
A second layer repeated it: the triage category excluded the vacancy as `NOT_TARGET_ROLE`
because `role=OTHER` and `skills=WEAK`.

How the existing design already separates the cases (unchanged):

| Case | Mechanism |
|---|---|
| Short or empty description | `data_confidence < 0.45` gives `NEEDS_ENRICHMENT`, never SKIP |
| Thin skill list, no contradiction | Stage 11E downgrade, then Stage 11C rescue to MAYBE |
| Confirmed missing must-have | rescue refused (`missing_must > 0`) — stays SKIP |
| Senior/Lead title | Stage 11A exclusion, before the cardinality guard |
| Support / administration | Stage 11B and `support_admin_family` |

## 2. What changed

* `role_relevance_classifier.is_review_rescue_title`: a RELEVANT title, or an UNKNOWN title whose
  **whole text** is a plain engineer title: optional entry-level words, then `Python Engineer`,
  `Backend Engineer`, or `Engineer` with Python named as its focus (`Schwerpunkt Python`,
  `- Python`, `(Python)`). Gender markers and `(Junior)` are normalised first. Any other qualifier,
  discipline, second role, location suffix or non-Latin text makes the title not eligible.
* `job_score_evaluator._title_may_be_rescued`: Stage 11C uses it and additionally refuses any
  support/administration or hybrid support family. Still only SKIP -> MAYBE.
* `seniority_classifier`: `Sr.` / `Sr` is a senior marker (a gap found by Codex round 1; the widened
  rescue would otherwise have exposed it).
* `triage_category`: skills `WEAK` **without a named missing requirement** on an eligible title is
  `MANUAL_REVIEW` / `THIN_SKILL_EVIDENCE` instead of `EXCLUDED`; the reason keeps the first two fit
  reasons (language gap, route ...). `WEAK` with a named missing requirement ("missing required: ...")
  is a contradiction and stays excluded.

Not changed: the threshold of two, the language policy, the fit tier, `AUTO_PREPARE_TIERS`, the
shortlist gate, remote classification, anything in PR #47-#49.

How the rule was reached, honestly: the first version matched Python anywhere beside an engineer
word and also rescued *IT System Engineer - Backend*; the second used a list of excluded
qualifiers, and Codex found `Mechanical Design Engineer - Python` and `Engineer (Mechanical) - Python`
through it. A list of exceptions cannot be completed, so the final rule is a closed grammar matched
against the whole title. It is still a heuristic on title words: it is deliberately narrow, and a
title it does not recognise is simply not rescued (the safe direction); it is not "judged".

Important property, confirmed by Codex: **MAYBE is not a block.** `automation_shortlist` admits
MAYBE vacancies to automatic preparation when their live fit assessment allows it. For BFI the
live tier is `STRETCH` (skills WEAK, language GAP) and `AUTO_PREPARE_TIERS` does not contain it, so
nothing is prepared. A rescued title with strong skills and a good tier would be treated like any
other MAYBE; that is the existing behaviour for RELEVANT titles and was not changed.

## 3. BFI Junior Engineer (vacancy 3294)

| Fact | Value |
|---|---|
| Level, hours, place | entry level (`berufseinst`, `junior`, `quereinst`), full time offered, "100 % Remote mit gewisser Reisebereitschaft nach Ilmenau" |
| Skills | `WEAK`: required skill score 3/100, **no missing requirement named** (skills unconfirmed, not contradicted); PowerShell, Exchange, Windows/Linux operations in the text |
| Language | "Sehr gutes Deutsch in Wort und Schrift (min. B2-C1)" -> `GAP` ("German B2 required") at confirmed B1; the C1 half of the range is not shown in the reason |
| Remote | `COMPATIBLE` (FULL_REMOTE) |
| Before | score 81, SKIP, triage EXCLUDED |
| After | score 81, MAYBE, triage MANUAL_REVIEW (`THIN_SKILL_EVIDENCE`), tier STRETCH, **automatic preparation blocked** |

The posting is also half operations work and coordinates a developer team of three to four. It is
a vacancy to read, not one to recommend.

## 4. XING stubs

Unchanged and deliberately so. The 118 title-only XING rows carry no description, so they remain
`NEEDS_ENRICHMENT` through the confidence floor; no skills, experience or conditions are invented.
Their links are per-recipient tracking links and are never followed (CLAUDE.md). The manual list is
outside the repository.

## 5. Before / after on the database copy (3 368 vacancies)

Method: every row evaluated with the real evaluator with and without the rescue, then the whole
population analysed (`shortlist_e2e_report.analyse`) with and without the new triage branch. The
copy's stored scores come from older code and drift by a few points on unrelated rows; only
differences caused by this change are counted. Final scoring code `a1b67b3`.

| Metric | Before | After |
|---|---:|---:|
| Vacancies | 3 368 | 3 368 |
| Decisions changed by the rescue | - | 1 (SKIP -> MAYBE) |
| Triage PRIMARY | 3 | 3 |
| Triage ADJACENT | 5 | 5 |
| Triage MANUAL_REVIEW | 656 | 657 |
| Triage EXCLUDED | 2 704 | 2 703 |
| Fit tier STRETCH | 137 | 138 |
| Fit tier NEEDS_OPERATOR | 936 | 935 |
| Automatic shortlist | 2 | 2 |
| FULL_REMOTE | 73 | 73 |
| FULL_REMOTE + Vollzeit | 14 | 14 |
| Admin/Support wrongly admitted | 0 | 0 |
| Applications created or changed | 0 | 0 |

Vacancies whose decision changed:

| ID | Title | Source | Before -> after | Triage | Full text | Automatic preparation |
|---:|---|---|---|---|---|---|
| 3294 | Junior Engineer (m/w/d) Schwerpunkt Python | Bundesagentur | 81 SKIP -> 81 MAYBE | EXCLUDED -> MANUAL_REVIEW | yes | forbidden (tier STRETCH) |
| 919 | Sr. Softwareentwickler (w/m/d) Linux | Bundesagentur | 81 MAYBE -> 0 SKIP | senior exclusion | yes | forbidden |

Id 919 is the effect of recognising `Sr.`: a vacancy that used to look like a MAYBE is now excluded
as senior (measured separately, with and without the marker). The other four titles containing
`Sr` in the copy (1771, 1920, 2856, 2903) keep their decisions. An earlier draft of the rescue
changed three more vacancies (395, 713, 1462: experienced or non-entry engineering roles); the
closed grammar no longer rescues them, and they never left manual review.

Junior Python/Backend vacancies that were wrongly rejected and are now routed to review: **one**
(3294). The suspicion that many such vacancies are lost is not supported by this database: the
guard already rescued RELEVANT titles (for example *Quereinsteiger als Junior Python-Entwickler*,
id 3252, MAYBE; it already has application 25, SUBMITTED, and is not touched).

## 6. Tests

`tests/test_junior_scoring_enrichment.py` (75 tests): the BFI shape; never APPLY; a confirmed missing
requirement stays SKIP; short description and title-only stub give NEEDS_ENRICHMENT; unsuitable
titles (Senior, Sr., Lead, Systemadministrator, IT Support, Helpdesk, 2nd-level, Windows
Administrator, Java, DevOps, Maschinenbau, DevOps/System/Mechanical Engineer - Python) stay SKIP; the
title truth table incl. every bypass Codex named (suffix, second role, discipline in parentheses,
Cyrillic qualifier, unbalanced parentheses, location suffix); `Sr.` is a senior marker and is not
matched inside words; scoring twice gives one row; no application is created and the status is
unchanged; the language policy at confirmed B1 (B1, B2 required, B2-C1, B2 preferred, C1 mandatory,
none); the triage branch and its negatives, including WEAK with a named missing requirement and the
preserved language-gap reason. The positive test fails without the fix (checked). Remote and
distance behaviour is covered by the PR #48/#49 suites, unchanged.

## 7. Verification

**Linux CI (GitHub Actions) is the authority for "0 failed".**

| Run | Commit | Result |
|---|---|---|
| CI `test` (full suite, Linux) | f2971dd | 8 460 passed, 169 skipped, 0 failed |
| CI `scheduler-postgres` | f2971dd | 125 + 36 passed, zero skips |
| CI `test` (full suite, Linux) | a1b67b3 | **8 495 passed, 169 skipped, 0 failed** |
| CI `scheduler-postgres` | a1b67b3 | 125 + 36 passed, zero skips |

**Windows, full SQLite suite**, one complete run on `f2971dd` (before the last three scoring fixes):
8 454 passed, 168 skipped, **7 failed**, 1 h 00 min. The 7 failures are all
`tests/test_imap_deadline.py` (constructor greeting slow drip, silent peer after handshake, normal
handshake and greeting, legacy file attribute layout x2, watchdog force close, tcp socket
registered before connect). They fail identically on the unchanged PR #49 commit `6ec1934` when that
file is run alone on an idle machine, with `OSError: [WinError 4551]` (an application control
policy blocks the subprocess these tests start). They pass on Linux CI. No Windows protection was
changed to make them pass, and they are **not** counted as fixed locally.

After the last scoring fixes the targeted suites were run locally and pass (new file 75 tests; with
sparse evidence, evidence cardinality, role quality, seniority and matching quality: 263 tests at
the last multi-file run), and Ruff `check` and `format` are clean. A second full Windows run was
not repeated: it takes an hour and would show the same 7 environment failures.

Skips (168 on Windows): 161 are PostgreSQL integration tests (no `TEST_POSTGRES_URL` in that run;
they were run separately and pass, below), 1 needs the local canonical database, about 6 are
runtime skips that were not itemised.

**PostgreSQL 16** (throwaway container, existing containers untouched): `alembic upgrade head` ->
single head `a7c3e5f1b9d2`; integration phase 1: 125 passed; migration-cycle phase 2
(downgrade/upgrade): 36 passed; zero skips; `alembic current` at head afterwards. A first attempt
hung on `localhost` (IPv6 versus a port published only on 127.0.0.1) and one Gmail-alias test failed
once on a database left dirty by a process I had killed; on a fresh container with `127.0.0.1` the
phases are clean.

## 8. Codex review (read-only, static)

Codex cannot run pytest in its sandbox; every verdict below is code reading only, kept separate from
the runtime results in section 7.

| Round | Findings | Outcome |
|---|---|---|
| 1 | P2 Python rescued unrelated engineer titles; P2 `Sr.` not recognised; P3 `THIN_SKILL_EVIDENCE` mislabelled WEAK coverage | all fixed |
| 2 | P2 qualifier list bypassed (`Mechanical Design Engineer - Python`, `Engineer (Mechanical) - Python`) | replaced by an allowlist |
| 3 | 3 medium: unchecked suffix; validation and match on different engineer words; non-Latin qualifier ignored | replaced by a closed whole-title grammar |
| 4 | **ACCEPT**; one Low (unbalanced parenthesis) | fixed and tested; not re-reviewed |

Codex's formal verdict on the final grammar is ACCEPT (static). The unbalanced-parenthesis fix
after round 4 was not shown to Codex again. Codex also noted, and it is correct, that MAYBE is
admitted to automatic preparation by the existing shortlist; see section 2.

## 9. Data safety

All work ran on copies of the database and in a separate worktree. No production database,
scheduler, `.env`, migration, cache or Bewerbung was touched. Application 25 (SUBMITTED) is
unchanged; the new tests assert that scoring creates no application and does not change a job's
status. Nothing was sent.

## 10. Risks and what is needed before merging PR #47-#50

* The fit cache is keyed on job columns and the rule set; after deployment recompute with the
  existing offline tool. Nothing recomputes on its own.
* The rescue is a title heuristic; it can only add review items (MAYBE) and never an APPLY.
* Operator decisions still open from PR #49: how strictly to read "B2-C1" (it reads as "B2
  required"), whether to relax the two-signal rule further for FULL_REMOTE + entry level, a
  model-based remote extractor, XING enrichment by hand.
* Merge order: #47 -> #48 -> #49 -> this PR, each only by the operator.

Status: READY FOR OPERATOR REVIEW - NO PRODUCTION WRITE for the scoring change (Linux CI 0 failed on the
final scoring commit, PostgreSQL clean, Codex ACCEPT on the final grammar). Open items are listed above:
the 7 Windows-only IMAP failures stay environmental and unfixed locally, and the last Low fix was not
re-reviewed by Codex.
