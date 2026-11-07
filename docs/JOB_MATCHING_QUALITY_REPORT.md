# Job matching quality report

Branch `fix/job-matching-shortlist-quality`, stacked on PR #47 (`integration/audit-2026-11-06-release`, HEAD `fcc13bf`).
Nothing is merged to `main`. Production data, migrations, cache, `jobs.score`, Bewerbungen and `.env` were not touched.
All measurements ran on a consistent copy made with the SQLite backup API from a `mode=ro` source connection.
The branch also contains the full-time remote work filter (section 5).

## 1. Why the shortlist held only 2 of 3 234

The result is mostly correct, not a rule defect.

1. **The pool is thin.** 125 of 3 234 vacancies have Python, Backend, Django or FastAPI in the title. Only 57 of those
   are junior, open or mixed level (the rest are senior or ask for experience). Of the 57, 33 are excluded (weak skill
   match such as Java or PHP backends, strong German required, out of reach) and 24 are held for review (unverified route
   or empty description).
2. **The two Junior Python / Backend vacancies (jobs 705 and 752) are held for a proven reason.** Both are in Karlsruhe,
   about 122 km straight line from Heusenstamm (the car route is never shorter than the line; limit 100 km). They are posted by an
   agency, so the real workplace is unknown. Job 705 also infers German C1.
   - Where: `job_fit_assessment._estimate_criterion` returns `LOCATION_UNVERIFIED` when `workplace_doubts()` is not empty.
     That status is in `_VERIFY` but not in `_TIER_ACCEPTS`, so `FitAssessment._tier()` returns `NEEDS_OPERATOR`, and
     `shortlist_gate.automatic_block_reason` returns `FIT_TIER_NEEDS_OPERATOR`.
   - This is the intended behaviour: an unknown route is never consent to prepare a Bewerbung. They stay visible.
3. **The two that did pass are adjacent roles** (job 212 Datenanalyst, job 772 DevOps Engineer). Ranking ignored the role
   family, so nothing stopped them from taking slots from Python / Backend roles once more of those qualify.
4. **A hidden pool exists but cannot be judged automatically.** 268 vacancies have the recommendation `NEEDS_ENRICHMENT`; 118
   are XING alert stubs (title only, 52 characters, confidence 0.1). About 34 of them are junior developer titles, among them
   "(Junior) Python Entwickler" (324), "(Junior) Backend Engineer" (310, 347) and "Junior AI Software Engineer" (244). The XING
   collector deliberately never follows e-mail links (see `app/collectors/xing_email.py`), and the shortlist pre-filter only
   looks at APPLY / MAYBE. They show as MANUAL_REVIEW but need the operator to open them.

No threshold was changed to reach a target number.

## 2. Funnel on 3 234 vacancies (developer path, in order)

| Stage | In | Out | Rejected (top reasons) |
|---|---:|---:|---|
| All vacancies | 3 234 | 3 234 | - |
| Status NEW / SAVED | 3 234 | 3 234 | - |
| Target profession (developer role) | 3 234 | 513 | other 1 986, off-role 393, no IT signal 342 |
| Seniority (not senior / student / experienced) | 513 | 264 | experienced 169, senior title 69, student 11 |
| Recommendation APPLY / MAYBE (`jobs.score`) | 264 | 62 | SKIP 167, NEEDS_ENRICHMENT 35 |
| Skills not WEAK | 62 | 47 | weak 15 |
| Match score >= 80 | 47 | 10 | below 37 |
| Fit tier not NOT_SUITABLE | 10 | 3 | not suitable 7 |
| Automatic preparation allowed | 3 | 0 | NEEDS_OPERATOR (route / data) 3 |

The largest single cut is the stored `jobs.score` recommendation (167 developer roles with SKIP). It was not changed;
`jobs.score` is not recalculated here. The two vacancies that pass automatic preparation are the adjacent roles; they
are not on the developer path above.

## 3. Categories after this change (all 3 234, final code)

| Category | Count |
|---|---:|
| PRIMARY | 3 |
| ADJACENT | 5 |
| MANUAL_REVIEW | 669 |
| EXCLUDED | 2 557 |
| Enter the automatic shortlist | 2 (both ADJACENT: jobs 212 and 772) |
| Held for an unconfirmed route (`ROUTE_REVIEW_REQUIRED`) | 320 (43 with APPLY/MAYBE) |
| Held for unclear remote terms (`REMOTE_UNVERIFIED`) | 196 (35 with APPLY/MAYBE) |
| Held for experience / seniority (EXCLUDED) | 630 experienced + 618 senior title |
| Held for skills (EXCLUDED, weak skills) | 1 278 |
| Out of reach (EXCLUDED) | 718 |
| IT admin / support titles | 228 matched by an independent pattern: 225 EXCLUDED, 3 MANUAL_REVIEW (hybrid titles), 0 PRIMARY / ADJACENT, 0 shortlisted |

MANUAL_REVIEW by reason: `ROUTE_REVIEW_REQUIRED` 320, `REMOTE_UNVERIFIED` 196, `STATED_GAP` 95, `ROLE_UNCLEAR` 41,
`DATA_REVIEW_REQUIRED` 14, `HYBRID_ADMIN_ROLE` 3. The shortlist outcome is unchanged: 2, no admin / support, no NOT_SUITABLE.
Fit-tier counts moved only by the remote handling: NOT_SUITABLE 2 139 (was 2 150), NEEDS_OPERATOR 949 (was 933), STRETCH 137
(was 140), PARTIAL_MATCH 9 (was 11).

### Typical vacancies

| Vacancy | Category | Why |
|---|---|---|
| 705 (Junior) Software Developer Python | MANUAL_REVIEW / ROUTE_REVIEW_REQUIRED | agency posting, ~122 km line; visible, not prepared |
| 752 (Junior) Backend-Entwickler | MANUAL_REVIEW / ROUTE_REVIEW_REQUIRED | same |
| 212 Datenanalyst | ADJACENT | skills match, not a developer title |
| 772 DevOps Engineer | ADJACENT | same |
| 1353 Django developer | MANUAL_REVIEW / ROUTE_REVIEW_REQUIRED | route not yet checked |
| 188 Systemadmin | EXCLUDED | `role OFF_ROLE`, administration |
| 2231 Client Support | EXCLUDED | `role OFF_ROLE`, IT support |
| 2279 IT-Administratoren | EXCLUDED | `role OFF_ROLE`, administration |
| 1834 IT-Administrator / Softwareentwickler | MANUAL_REVIEW / HYBRID_ADMIN_ROLE | hybrid title; never prepared automatically |

## 4. What changed (matching quality)

- `app/services/triage_category.py` (new, pure): PRIMARY / ADJACENT / MANUAL_REVIEW / EXCLUDED with a reason code and a
  work format. Direction comes from the title (`target_role.is_developer_title`), not from which roles are configured as
  priorities. `ROUTE_REVIEW_REQUIRED` is a derived label, not stored. No schema change, no migration.
- `shortlist_gate.automatic_preparation_check` returns the block reason and the rank from one assessment.
  `automatic_preparation_block` keeps its contract. The automatic shortlist orders by role family, then work format
  (full remote, hybrid, on-site), then match score, `jobs.score`, id.
- Hybrid titles ("IT-Administrator / Softwareentwickler", "IT-Support für Softwareentwickler") are held from automatic
  preparation (`HYBRID_ADMIN_ROLE`). Fails safe: a false positive only costs a manual look. Support that names the product
  built after a developer role ("Python Entwickler Service Desk Tools") is not a hybrid.
- The administrator pattern now also covers Linux / Unix / Windows / Database / Cloud / Server / Network "Admin".
- `scripts/shortlist_e2e_report.py` reports categories, reason codes and work formats.

Not changed: thresholds, fit-tier rules, `AUTO_PREPARE_TIERS`, `confirmed_barrier` (Telegram proposals still show
NEEDS_OPERATOR), any status, any Bewerbung.

## 5. Full-time remote work filter

Rule implemented: a posting that is confirmed fully remote and workable from Germany does not get a commute question; the
employer's city plays no role. Everything else about the job is still checked.

| Work format | How it is decided | Geography |
|---|---|---|
| `FULL_REMOTE` | an affirmative full-remote phrase (100 % Remote, Fully Remote, Homeoffice 100 %, Remote Germany / EU / Europe, bundesweit remote) and nothing against it | no car route, `location = REMOTE_OK` |
| `HYBRID` | hybrid wording, or an office-presence obligation next to a remote claim | existing commute limit applies |
| `ONSITE` | no remote, "kein Homeoffice", remote denied (also "not permitted", "prohibited", "keine Option") | existing commute limit applies |
| `REMOTE_UNVERIFIED` | remote claimed but unclear, or "Homeoffice möglich" / "Remote möglich" | MANUAL_REVIEW; never treated as 100 % remote |

"Nothing against it" means, in `classify_remote`: no denial of remote work in its clause, no office-presence phrase
(strong phrases such as on-site, Präsenz, Büropräsenz, attend; weak ones such as Büro, vor Ort, Standort only together with an
obligation or frequency cue, and never inside a benefit sentence), and no unresolved eligibility wording (only / nur /
residents / must reside / restricted to ...) unless the same clause names Germany or Europe. Negation and "optional" only count
next to the phrase they modify.

- Foreign-country limits ("Remote US only", "nur aus Österreich", "Polish residency is required") are `INCOMPATIBLE`, a hard
  barrier. An unclear limit is `UNVERIFIED_ELIGIBILITY`, which needs the operator even for a short commute.
- A full-remote claim whose terms are unclear is not hard-excluded by distance: it goes to review. A plain on-site job that
  merely allows home office stays excluded when far away.
- New `work_time` criterion: part-time only is a stated gap (held for the operator), a missing working time blocks nothing,
  "Vollzeit oder Teilzeit" counts as full time, headcounts ("50 Vollzeitmitarbeiter") and negations are ignored.
- Ranking inside a role family: FULL_REMOTE, then HYBRID, then everything else. Role family still comes first.
- Not touched: role, seniority, skills, experience and German language checks. A differential check by Codex found no
  change in those gate outcomes.
- Not implemented: counting office days in a hybrid posting (the commute limit applies to every hybrid job).

### Effect on the 3 234 vacancies

| Measure | Before | After |
|---|---:|---:|
| Ads with confirmed FULL_REMOTE (`location REMOTE_OK`) | 60 | 68 |
| of these full time stated / part time / not stated | - | 8 / 34 / 26 |
| `UNVERIFIED_ELIGIBILITY` (claim needs a human) | 0 | 138 |
| Python / Backend titles (125) passing the geographic filter | 20 | 16 |
| **New Python / Backend vacancies passing the geographic filter** | | **0** |
| Entering the automatic shortlist | 2 | 2 |

**The filter finds no new Python / Backend vacancy in this database.** Of the 125 Python / Backend titles, 6 were already
fully remote before; all of them are blocked by checks that were left on (experienced or senior level, German C1 against the
candidate's B1, a teaching job). The 4 that moved out of "passing" were all excluded anyway. 11 vacancies became reviewable
instead of NOT_SUITABLE (2 Python / Backend: jobs 1398 and 3058, both experienced level, so they go to review only).

**Why there will not be more:** the Bundesagentur collector searches `wo=<place>` with `umkreis=<radius>` (default 25 km, up
to 200 km). A remote job in Berlin or München is only collected if the radius reaches it, and the BA home-office filter
(`arbeitszeit=ho`) is unreliable. The geographic exemption is correct, but it needs input. A nationwide remote search (or another
source) is a separate collector decision and was **not** made here.

## 6. Skills provenance (Stage 5)

The matcher already separates skill coverage from professional years: projects never satisfy an "N years" requirement, and
course skills carry the note "Learning/project experience only". A new test proves AWS / Terraform learned in a course
match the skill list but leave a "3 years professional" requirement MISSING with experience support 0. Profile facts were not
changed. All 26 stored skills have `proficiency = UNKNOWN`. A stored evidence level (course / project / commercial) would
need a schema change and is **proposed, not implemented**.

## 7. Safety

- Admission is only `AUTO_PREPARE_TIERS` plus the hybrid block: not wider than before for any title. An unknown route
  (`DISTANCE_UNVERIFIED`, `LOCATION_UNVERIFIED`, `UNKNOWN`, `ESTIMATE_NEAR_LIMIT`) is `FIT_TIER_NEEDS_OPERATOR`.
- Remote handling can widen admission in one place only: a confirmed FULL_REMOTE job skips the commute. That confirmation
  fails towards HYBRID / ONSITE / REMOTE_UNVERIFIED on any doubt (see section 5).
- Fail-closed paths are unchanged: an unavailable assessment is `ASSESSMENT_UNAVAILABLE`, the late re-check still runs.
- The e2e copy shows 0 admin / support and 0 NOT_SUITABLE vacancies in the shortlist.
- No new writes, no status change, no sending. Existing Bewerbungen are not touched (the ranking test asserts no
  application row is created). Admission is not approval: every Bewerbung still needs the existing operator approval.

## 8. Tests

TEST_RESULTS_PLACEHOLDER

## 9. Codex review

Read-only, on the diff. Matching quality: six rounds, rounds 1 to 5 REJECT with valid findings (all fixed), round 6 **ACCEPT**.
Remote filter: nine rounds, **not accepted**. Each round found further phrasings; the last round (9) had four P1 and two P2
findings on realistic wording, all of which were fixed afterwards, and no further round was run. The final code therefore has
no Codex ACCEPT.

| Round | Remote filter: what Codex found (all valid, all fixed) |
|---|---|
| 1 | hybrid, negated and foreign-country wording promoted to FULLY_REMOTE; part-time not held; no assess_job-level tests |
| 2 | customer markets cancelling "US only"; optional presence; negation leaking across sentences |
| 3 | negation window; presence wording; unlisted countries; unresolved eligibility not enforced |
| 4 | long-range denial; regional claim masking a restriction; restriction in hybrid mode; unrelated "no" |
| 5 | unrelated negation or optionality cancelling presence; masked restrictions; common denial predicates |
| 6 | "no exceptions" idiom; "not permitted"; "also" suppressing a requirement; customer wording |
| 7 | "required attendance"; prohibition predicates; skill-guard hiding a denial |
| 8 | no P1: unavailable / "kommt nicht infrage"; onboarding at a site; nationality + residency |
| 9 | declarative presence ("erfolgt vor Ort"); employer-imposed "can be required"; "keine Option"; position restrictions; benefit sentences |

Codex also reported that its differential probes found no change to the role, seniority, skills, experience or language
gates, and that human approval of every application bounds the consequence of a wrong admission to a prepared draft.

**Honest limit:** reading free-text job ads with patterns has no finish line. Every round turned up another phrasing, and
the rule set is now long. The design therefore fails towards review (a doubtful remote claim goes to the operator, never
skips the commute), and the remaining risk is a prepared draft for a job that needs office presence or a foreign
residence, which the operator would see at the mandatory approval. A model-based reading of the posting, with this
rule set as the fallback, is the sensible next step; it is not part of this change.

## 10. Known limits and decisions for the operator

- Role direction is title-based. Java / .NET / C++ "Software Developer" titles count as developer roles; their skill fit
  and gaps decide the tier. The tier hides weak skills behind an open route, so route-held vacancies show the note
  "skills WEAK" in their reason; they are not excluded because nothing proves them unsuitable.
- The category and work format are not yet rendered in the web UI or Telegram cards; those still show the fit tier.
- Whether adjacent roles (data analyst, DevOps) should be prepared automatically at all is a policy choice. They are
  allowed today (fit tier PARTIAL_MATCH) and rank after PRIMARY.
- The Karlsruhe agency postings need a human route check. Measure the car route, or the real client address, before any
  Bewerbung.
- About 34 junior developer XING stubs need the operator to open them (section 1).
- A nationwide remote collector run is needed before the full-remote rule can add vacancies (section 5).
- 8 of the 68 full-remote ads state full time; 34 state part time and are held. The "Vollzeit" condition therefore removes
  most of the remote ads in this database.

## 11. Before rollout

1. Operator review of this PR (base: the PR #47 branch).
2. Merge order: #47, then this PR. No migration, no data change, no new configuration.
3. After deployment the cached `job_fit_assessments` are unaffected (categories and work formats are computed, not stored).
4. Decide the open points in section 10, in particular whether to add a nationwide remote search.

PR: PR_LINK_PLACEHOLDER

STATUS_LINE_PLACEHOLDER
