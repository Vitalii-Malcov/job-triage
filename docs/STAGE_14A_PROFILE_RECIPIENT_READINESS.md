# Stage 14A — Profile completion, score consistency, recipient verification

Status: implemented on `feat/stage-14a-profile-recipient-readiness`. Nothing in Stage 14A sends,
submits, contacts, or turns `OUTBOUND_SENDING_ENABLED` on.

## Stage roadmap

| Stage | Scope | State |
|---|---|---|
| 13D | Controlled live pilot: one real vacancy card, 9B/9C in Telegram, one real Gmail **draft** | **CLOSED** — live draft flow proven 2026-10-15. Its secure environment is frozen and must not be reused or modified. |
| 14A | Profile readiness, score labelling (83 vs 80), application-destination model, read-only send-readiness gate, E362/B research | This stage |
| 14B | Send-ready **draft** package: real verified recipient/channel + CV/PDF, consuming the 14A gate. Still draft only. | Future |
| 14C | Human-send observation period: the human sends; the system only observes outcomes | Future |
| 15 | Optional explicit, confirmed automatic send | **NOT IMPLEMENTED / NOT AUTHORIZED** |

## 1. Why the 13D Bewerbung said `NO_TRUSTED_TITLE`, `NO_RELEVANT_EXPERIENCE`, "keine"

Not a generator bug. The trust pipeline (Stage 6A `is_usable_for_generation`) did exactly what it
should with the profile it was given. The profile (transferred unchanged from the Stage 11 pilot in
13B, plus trusted first/last name added in 13D) contains:

| Area | Structured & trusted | Note |
|---|---|---|
| Name | yes | |
| Professional title | **no** | `target_roles` is set, but a target role is not a current title and is never used as one |
| Summary | yes | free text, copied verbatim, never parsed into claims |
| Skills | yes (11) | |
| Experience | **no rows** | mentioned only in the free-text summary |
| Projects | **no rows** | mentioned only in the free-text summary |
| Education | **no rows** | mentioned only in the free-text summary |
| Languages | **no rows** | not mentioned at all |
| Certifications | no rows | |

The summary does not contain an employer, job title, institution, project name, date or language
level, so none of it can be normalized into structured rows without inventing facts. **Nothing was
migrated.** These facts require operator input (template below).

`app/agents/profile_readiness.py` now reports each area as `VERIFIED`, `UNSAFE_TO_CLAIM`
(rows exist but are untrusted), `FREE_TEXT_ONLY` (summary hint only) or `MISSING`, plus caveat notes
(incomplete degree, language level unknown, certification not completed, target roles ≠ title).

## 2. Generation changes

- CV adapter `v2`: empty sections are warnings, never silent "keine":
  `NO_RELEVANT_PROJECTS`, `NO_TRUSTED_EDUCATION`, `NO_TRUSTED_LANGUAGES`, and
  `NO_MATCH_RELEVANT_EXPERIENCE` (trusted experience exists but none overlaps the job).
- Bewerbung generator `v3`: the deterministic provider cites the first relevant trusted
  **experience** and the first relevant **project** (experience first). Before, a single slot was
  always won by a project, so a trusted relevant experience could be dropped from the letter.
- Unchanged and re-tested: untrusted facts are excluded everywhere; education and certifications are
  never rendered in the letter; a language is claimed only with a known CEFR level.
- Preview renderer `tg-bw-preview-v2`: German texts for the CV gap codes.

Version bumps make previously published 9B packages non-current (by design of
`compute_input_identity`); no historical row is modified.

## 3. "Match 83%" vs "Kandidaten-Match 80"

**Two intentionally different scores, both persisted once and never recomputed for display:**

| Surface | Value | Source | Formula |
|---|---|---|---|
| Vacancy card / details | 83 | `JobRecord.score` ← `JobScorer` (ingestion) | 0.70·must overlap + 0.20·nice overlap (0.5 if none) + 0.10·profile-skill mentions in text |
| Bewerbung preview / approval doc | 80 | `CandidateJobMatchRecord.overall_score` ← `compute_match` v1 | 0.6·required coverage + 0.2·preferred coverage (100 if none) + 0.2·experience/project backing |

E362/B: must = {python, git, rest}, no nice-to-haves, 3/11 profile skills in the text, no trusted
experience/project → `70 + 10 + 2.7 = 83` vs `60 + 20 + 0 = 80`.

The defect was labelling: both appeared as a bare "Match". Fix: `app/agents/score_labels.py` —
card shows **"Stellen-Score (Vorauswahl): 83/100"**, Bewerbung shows
**"Kandidaten-Match (belegte Profil-Nachweise): 80/100"** plus a one-line explanation.
Regression: `tests/test_stage14a_score_consistency.py` reproduces the exact pair.

Known, documented divergence (not changed, would require `ALGORITHM_VERSION` v2): with no
nice-to-have/preferred requirements JobScorer uses a neutral 0.5 while the matcher uses 100.

## 4. Application destination model

`app/models/application_destination.py` — vacancy-scoped (an employer may take a portal for posted
jobs and email only for unsolicited ones, see E362/B), so it is deliberately **not** part of company
research. Fields: kind (`EMAIL` / `APPLICATION_PORTAL` / `CAREERS_PAGE` / `UNKNOWN`), value,
source_url, source_type, verification_status, confidence, verified_by, vacancy_specific,
provenance, checked_at, reason.

Fail-closed rules (hardened after the Codex 14A review):

- `UNKNOWN` needs a reason; CR/LF/NUL rejected; URL values/sources must be `https://` (email: shape
  check).
- **`VERIFIED` is a human statement.** The model refuses `verification_status="VERIFIED"` unless
  `verified_by="OPERATOR"`, plus `checked_at` and an https employer `source_url` (or an operator
  entry). Aggregators/job boards can never verify. Research never sets `OPERATOR`: an employer
  portal discovered by research stays `UNVERIFIED` until a human confirms it.
- **Package-ready kinds:** `APPLICATION_PORTAL` and `EMAIL` only. A generic `CAREERS_PAGE` is never
  package-ready.
- `is_channel_verified` (package-ready) requires operator verification, a verifiable source,
  `checked_at`, `vacancy_specific` and HIGH/MEDIUM confidence. For `EMAIL` there is no weaker path:
  it must pass the strict email rule `is_email_send_ready` — operator-verified against the
  employer's own **job ad** for this vacancy, HIGH confidence. Scraped, general-inbox and
  company-level addresses (careers@, careers-site FAQ) are never package-ready.
- Both predicates re-check every condition themselves, so an instance built with `model_construct`
  (validation bypassed) still fails closed.

No DB table in 14A (no schema change, no migration). Research results are committed as JSON under
`docs/research/` and passed to the gate explicitly. 14B decides on persistence.

## 5. E362/B research (read-only GET, 2026-10-15)

| Question | Finding | Source |
|---|---|---|
| Still open? | **Open** — employer ad live, BA listing edited the day before | employer job ad; BA jobdetail `20079-GR6IFL6L-S` |
| Authoritative location | **Hannover oder Münster**; "die Abteilung ist ausschließlich am Standort Hannover angesiedelt" | employer job ad |
| Why Frankfurt? | BA lists "verschiedene Arbeitsorte" (Hannover, Frankfurt am Main, Münster) with the same Hannover note; the collector persisted only Frankfurt am Main | BA listing |
| Application channel | **Employer online portal** ("Jetzt bewerben!" → `#bewerbung` on the ad). Employer FAQ: "Bewerbungen sind ausschließlich über das Online-Portal möglich", email only for Initiativbewerbungen | employer ad + `f-i.de/karriere/haeufige-fragen-faq` |
| Direct recruiting email for this vacancy | **No.** Only the general careers address (questions / unsolicited applications) is published | employer FAQ |
| BA points to portal or email? | Neither visible: BA hides employer contact data behind a security question (not solved — no interaction performed) | BA listing |

The portal form is script-rendered and was not inspected. Recorded as `APPLICATION_PORTAL`,
`UNVERIFIED`, confidence MEDIUM in `docs/research/stage14a_e362b_destination.json`; an operator
must confirm it in a browser **without submitting anything**. No recruiter personal data is stored.

Consequence for 14B: E362/B is a portal vacancy. A 14B package for it is a human portal-upload
handoff (CV/PDF + letter text), not an email draft with a recipient.

## 6. Send-readiness gate

`app/services/send_readiness.py` — pure `evaluate_send_readiness` + read-only
`load_send_readiness_inputs`. Every check is required; the gate is **vacancy-specific** and fails
closed. `ProfileReadinessReport` (section 1) stays a vacancy-independent, advisory diagnostic and is
never accepted as proof by the gate.

| Check | Passes only if |
|---|---|
| `TRUSTED_CANDIDATE_TITLE` | the approved package's reviewed CV title **equals** the current trusted profile title (whitespace-normalized). A differing title — including a human edit to another title — is NOT ready. There is no override path. |
| `RELEVANT_STRUCTURED_EVIDENCE` | at least one experience or project that the **pinned match** marks relevant (non-empty `matched_skills`), that is still trusted in the current profile, and that the approved CV cites. Unrelated trusted history does not count. |
| `REQUIRED_CV_DATA` | trusted first and last name. |
| `EDUCATION_REQUIREMENT_SATISFIED` | every binding (`REQUIRED`/`UNKNOWN`-importance) education requirement of the pinned match is `MATCH` on trusted, **completed** education rows, and the CV never shows an unfinished degree as completed. No requirement → no education needed; `PREFERRED` is advisory. |
| `LANGUAGE_REQUIREMENT_SATISFIED` | every binding language requirement is `MATCH` on trusted rows with a **stated** level (`UNKNOWN` never satisfies a required level; an insufficient level is `PARTIAL`). No requirement → no language needed. |
| `APPROVED_BEWERBUNG_REVISION` | an APPROVED review whose loaded revision is exactly its `approved_revision_id`. |
| `APPROVED_PACKAGE_MATCHES_CURRENT_PROFILE` | the package-currency check below reports no problem. |
| `APPLICATION_CHANNEL_VERIFIED` | `is_channel_verified` (section 4): operator-verified, vacancy-specific portal or strict-rule email. |
| `RECIPIENT_TYPE_KNOWN` | destination kind is not `UNKNOWN`. |
| `NO_CONTRADICTORY_LOCATION_EVIDENCE` | location rules below. |
| `OUTBOUND_STILL_DISABLED` | `OUTBOUND_SENDING_ENABLED` is false. |

**Package currency — stale approved packages fail closed.** `package_currency_problems` rejects: a
superseded review (a newer review exists for the job); a non-APPROVED or other-job review; an
approved revision that is missing, not owned by the review, or not the review's current revision
(revision number ≠ `review_version`, or a newer revision exists); a missing or inconsistent pinned
match/CV/letter triple (every id and every version/fingerprint pin compared across review, match,
CV and letter); a stale profile version; a stale job fingerprint; a matcher / CV-adapter /
generator version older than the running code; and — reusing the Stage 8C/9B authority
`bewerbung_draft_is_current` — a letter not current for the job's exact title/company display
context. Packages generated under older semantic versions (e.g. CV adapter v1, generator v2)
therefore never pass. Inputs assembled without the currency check default to
`PACKAGE_CURRENCY_NOT_CHECKED` (fail).

**Location rules.** Only an employer source (`EMPLOYER_JOB_AD` / `EMPLOYER_CAREERS_SITE`) with an
https `source_url` and `checked_at` may be authoritative (validated, and re-checked in the gate).
Authoritative sources are evaluated independently, never unioned: `AUTHORITATIVE_SOURCES_DISAGREE`
if they differ; `JOB_LOCATION_MISSING` if the persisted job has no location;
`JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS` if the persisted location is outside any
authoritative set; `SOURCE_LISTS_OTHER_LOCATIONS:<type>` if a board/aggregator lists other places.
A board can never overrule the employer. No operator location resolution is modeled in 14A: a
conflict stays NOT ready until the stored data or the evidence changes.

**Strict read-only loader.** `load_send_readiness_inputs` refuses (`SessionNotCleanError`) a session
with pending new/dirty/deleted ORM state **before** any read, performs every read under
`no_autoflush`, and never flushes, commits or mutates.

**Zero-network CLI.** `scripts/stage14a_send_readiness.py` accepts only a local read-only SQLite URI
`sqlite:///file:<local path>?mode=ro&uri=true`, validated before any engine is created. It rejects
PostgreSQL/MySQL/any other dialect, driver suffixes, hosts/credentials, plain or writable SQLite
URLs, extra URI parameters, `:memory:` and UNC / `file://host` paths. The connection also runs
`PRAGMA query_only = ON`.

```
python scripts/stage14a_send_readiness.py \
  --database-url "sqlite:///file:stage11_pilot.db?mode=ro&uri=true" --job-id 27 \
  --destination-json docs/research/stage14a_e362b_destination.json \
  --location-json docs/research/stage14a_e362b_locations.json
```

Result on the Stage 11 pilot copy of E362/B (re-run after hardening, DB SHA-256 unchanged):
`READY_FOR_SEND_PACKAGE = NO` — only `REQUIRED_CV_DATA` (name), `RECIPIENT_TYPE_KNOWN` and
`OUTBOUND_STILL_DISABLED` pass. Location: stored/BA Frankfurt am Main vs employer Hannover/Münster
→ `JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS` + `SOURCE_LISTS_OTHER_LOCATIONS:JOB_BOARD_LISTING`.
The portal stays `UNVERIFIED` until human confirmation. (The 13D approved package lives in the
closed 13D environment and was deliberately not read.)

## 7. Operator input template (no data is pre-filled)

Enter through the normal authenticated `PATCH /api/v1/candidate-profile` (with the current
`expected_profile_version`). Only enter facts you can stand behind; leave anything unknown out.
Defaults are `MANUAL_ENTRY` / `CONFIRMED`.

```json
{
  "expected_profile_version": "<current>",
  "professional_title": "<your current/most accurate title, not the target job>",
  "experiences": [
    {"company": "<employer or client>", "job_title": "<role>",
     "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD or omit", "is_current": false,
     "location": "<optional>", "description": "<optional>",
     "responsibilities": [], "achievements": [], "technologies": ["<only tech used there>"]}
  ],
  "projects": [
    {"name": "<project>", "role": "<optional>", "description": "<optional>",
     "technologies": ["..."], "repository_url": "https://... (optional, public only)",
     "start_date": "YYYY-MM-DD (optional)", "end_date": "YYYY-MM-DD (optional)", "highlights": []}
  ],
  "education": [
    {"institution": "<name>", "program": "<optional>", "degree": "<optional>",
     "field_of_study": "<optional>", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
     "completed": false}
  ],
  "languages": [
    {"language": "Deutsch", "level": "A1|A2|B1|B2|C1|C2|NATIVE|UNKNOWN", "certificate": "<optional>"}
  ],
  "certifications": [
    {"name": "<optional>", "issuer": "<optional>", "status": "COMPLETED|IN_PROGRESS|PLANNED|UNKNOWN"}
  ]
}
```

Notes: list fields **replace** the stored list (send the full list). `completed` must be `true`
only for a finished degree. The current Weiterbildung belongs in `education` with
`completed: false` unless finished. Contact data (phone/address) is not modelled yet (14B).
