# Data Retention & Capacity Projection — JobTriage

Builds on the per-table growth-driver classification in `docs/ADVERSARIAL_HARDENING_REPORT.md` ("Database Growth / Retention Capacity Analysis"). This document projects **rough order-of-magnitude row counts**, not disk sizes (no real production deployment exists yet to measure actual row widths against — a fake-precise byte estimate would be worse than an honestly rough row count). **No retention is implemented here** — analysis only.

## Usage-tier assumptions (explicit, not measured)

| Assumption | Light | Normal | Heavy |
|---|---|---|---|
| New distinct jobs/day (all collectors combined) | 5 | 35 | 120 |
| Gmail messages synced/day | 5 | 20 | 50 |
| Candidate profile edits/month | 1 | 3 | 8 (active tweaking during a job search) |
| Automation cycle cadence | manual only (~10/month) | hourly (`automation_scheduler_interval_seconds=3600`) | every 15 min |
| Bewerbung-draft regenerations/month | 10 | 100 | 500 |
| Review-package edits per package (avg) | 2 | 3 | 4 |

These are illustrative planning assumptions for a **single-user** deployment (this project's actual target, per `docs/DEPLOYMENT.md`) — not derived from any real usage telemetry, since none exists.

## Projected row counts (order of magnitude)

| Table | Light: 1mo / 1yr / 3yr | Normal: 1mo / 1yr / 3yr | Heavy: 1mo / 1yr / 3yr |
|---|---|---|---|
| `jobs` | 150 / 1.8k / 5.5k | 1k / 12.6k / 38k | 3.6k / 43k / 130k |
| `gmail_messages` | 150 / 1.8k / 5.5k | 600 / 7.2k / 22k | 1.5k / 18k / 55k |
| `gmail_message_analyses` (amplified by profile edits, see report) | 150 / 1.9k / 6k | 700 / 8.5k / 26k | 3k / 36k / 110k |
| `candidate_job_matches` (amplified) | 60 / 800 / 2.5k | 500 / 6k / 18k | 2k / 24k / 72k |
| `candidate_cv_drafts` (inherits amplification) | 20 / 250 / 750 | 200 / 2.4k / 7.2k | 800 / 9.6k / 29k |
| `bewerbung_drafts` (no dedup — 1:1 with regenerations) | 10 / 120 / 360 | 100 / 1.2k / 3.6k | 500 / 6k / 18k |
| `application_package_review_revisions` | 15 / 180 / 540 | 150 / 1.8k / 5.4k | 800 / 9.6k / 29k |
| `automation_runs` | 10 / 120 / 360 | 720 / 8.6k / 26k | 2.9k / 35k / 105k |
| `response_drafts`/`follow_up_proposals` (combined) | 10 / 120 / 360 | 100 / 1.2k / 3.6k | 500 / 6k / 18k |
| `telegram_digest_deliveries` | ≤30 / ≤365 / ≤1095 (hard cap: 1/day) | same | same |
| `company_research` (bounded by distinct companies, not calls) | ~50 total, grows slowly, roughly tracks distinct companies in `jobs` | ~300 total | ~1000 total |

**All numbers above are illustrative, rounded, order-of-magnitude estimates** — built from the explicit assumptions table, not measured. Their purpose is to show *relative* scale and *which tables grow fastest*, not to serve as a capacity-planning SLA.

## What these numbers mean in practice

Even the **heavy, 3-year** column tops out in the low hundreds of thousands of rows per table — this is a genuinely small dataset by any standard relational-database measure (PostgreSQL handles tables with hundreds of millions of rows routinely; the `jobs` list query measured in this session's live capacity simulation against 1,000 synthetic rows executed in under 1ms even with a full sequential scan). **No table in this project is remotely close to a scale where retention becomes an operational necessity within a 3-year horizon under any of these three usage tiers**, including "heavy." This is worth stating plainly rather than implying urgency that the actual numbers don't support.

## Archive / prune / partition / keep-forever classification

| Table | Recommendation | Why |
|---|---|---|
| `jobs` | **Keep forever** (for now); revisit only past ~1M rows | Even heavy 3yr projection (~130k) is small; `last_seen_at` has no index (confirmed via live `EXPLAIN` — Seq Scan + Sort), so a FUTURE index addition is the more relevant action than pruning, if this table ever grows enough to matter — see `docs/API_BOUNDARY_HARDENING_REPORT.md` BOUND-005 for the analysis-only query-plan finding. |
| `gmail_messages` | **Keep forever**; consider archive only if a mailbox has unusually high non-job-related volume | Read-only sync target, bounded per-sync (`MAX_MESSAGES_PER_SYNC=500`); heavy 3yr projection (~55k) is trivial for PostgreSQL. |
| `gmail_message_analyses` | **Archive candidate first among the amplified tables**, but not urgently | Explicitly a versioned audit trail by design (never overwrites) — pruning would need a deliberate "keep only the latest N revisions per message" policy, not blind deletion, since older revisions have genuine diagnostic value. Growth-amplification pattern (candidate_profile_version-keyed identity) makes this the fastest-growing table under heavy usage. |
| `candidate_job_matches` / `candidate_cv_drafts` | **Same class as analyses** — amplified, versioned, archive-not-prune candidate if ever needed | Same profile-version-keyed amplification; these are the two tables most likely to benefit from a future "collapse to latest N versions per (job, candidate)" archival job, not deletion. |
| `bewerbung_drafts` | **Prune candidate, if ever needed** — genuinely no cache-identity constraint, so old, never-sent, superseded regenerations have the least ongoing value of any table here | Unlike the others, there's no versioning narrative tying old rows to a specific audit requirement — they're just prior draft attempts. Still not urgent given projected scale. |
| `application_package_review_revisions` | **Keep forever, explicitly** | The model's own docstring states these "must never be cascade-deleted" — a deliberate, permanent audit-trail requirement, not a candidate for future pruning under any usage tier. |
| `automation_runs` | **Keep forever**; consider partition only far beyond any tier projected here | Fastest-growing table under Normal/Heavy purely from cadence (hourly/15-min), but each row is small (no large text/JSON payload) and it's a genuine operational audit log — a future partition-by-month strategy would be the right tool if this table ever reached tens of millions of rows, not deletion. |
| `response_drafts` / `follow_up_proposals` / their approvals/sends | **Keep forever** | Small, low-growth-rate, and directly tied to the human-approval audit trail this project's safety model depends on — never a pruning candidate. |
| `company_research` | **Keep forever** | Refreshed in place (versioned UPDATE), not appended per-call — genuinely bounded by distinct company count, not by usage volume. |
| `telegram_digest_deliveries` / `xing_scan_progress` / `automation_schedules` / `job_reference_tokens` / `gmail_permanent_skips` | **Keep forever** | Already naturally bounded (≤1/day, ≤1/mailbox-scope, self-syncing, or low-volume-by-construction) per the growth-driver analysis in `docs/ADVERSARIAL_HARDENING_REPORT.md` — no realistic scenario makes these a retention concern. |

## Honest bottom line

At this project's stated single-user deployment target (`docs/DEPLOYMENT.md`), **no table requires retention/archival/partitioning action within any realistic 1–3 year horizon**, even under the "heavy" assumptions above. The growth-amplification pattern identified in the adversarial hardening report (`gmail_message_analyses`, `candidate_job_matches`, `candidate_cv_drafts`) is real and worth monitoring if this project were ever repurposed for multi-tenant or much longer-lived use, but is not an action item today. This conclusion is itself only as good as the usage-tier assumptions above — if actual usage diverges substantially (e.g. a genuinely multi-tenant deployment, or profile edits far more frequent than the "heavy" tier assumes), these projections should be recomputed against real data rather than trusted indefinitely.
