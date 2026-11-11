# Stage 62 — Gate-open candidates (read-only review)

Method: live `automatic_preparation_check` over all 3 234 jobs of a **copy** of the production
SQLite (revision `a7c3e5f1b9d2`). Production was not touched. Auto-prepare stays **OFF**; nothing
below is prepared, approved or sent. Duplicate status for all six: `CLEAR` (no application exists).

Block distribution on the copy: NOT_SUITABLE 2 142, NEEDS_OPERATOR 948, STRETCH 135,
**gate-open 6**, WORK_TIME_NOT_CONFIRMED 1, HYBRID_ADMIN_ROLE 1, FULL_TIME_NOT_CONFIRMED 1.

| ID | Title | Company | Location | Source | Full-time evidence | Tier / skills | Language | Real suitability for the candidate |
|---|---|---|---|---|---|---|---|---|
| 52 | DevOps Engineer | Ratiodata SE | Frankfurt (~12 km) | BA | BA flag `arbeitszeitVollzeit=true` (CONFIRMED) | PARTIAL / 50 (misses CI/CD, Kubernetes) | none stated | **Weak.** DevOps, not Python/backend; skill gap on required items. Manual look only. |
| 212 | Datenanalyst | BKK Linde | Wiesbaden (~40 km) | BA | BA flag true | PARTIAL / STRONG 100 | none stated | **Plausible.** Entry-level, Python-adjacent data role, partial home office. Operator decides. |
| 551 | Softwareentwickler Datenbank | outsourcing4work | Eltville (~50 km) | BA | BA flag true | PARTIAL / 50, role PRIMARY ("Junior Softwareentwickler") | none stated | **Plausible.** Closest to the target title; DB/SQL focus; remote unknown; check car route. Staffing intermediary. |
| 772 | DevOps Engineer | alpha-test GmbH | Mannheim (~69 km) | BA | BA flag true | PARTIAL / STRONG 100 | none stated | **Weak.** DevOps, ~69 km onsite, wants an earlier start than 01.01.2027. |
| 2351 | QA & Testautomatisierung | Statistisches Bundesamt | Wiesbaden (~39 km) | BA | BA flag true | PARTIAL / 50 (misses JavaScript, XML) | none stated | **Possible.** Public employer, tariff pay, partial home office; QA not backend. |
| 3203 | **Praktikum** Management Consulting Data Science | BASF SE | Ludwigshafen (~68 km) | BA | BA flag true | PARTIAL / 50 | none stated | **Not suitable.** An internship (`TRAINEE_OR_INTERNSHIP`), not a job. |

## Defect found by this review (fixed in this stage)

**G-1 (P1/P2): the gate admitted an internship.** `shortlist_gate.automatic_block_reason` never
read the `posting_type` criterion, so #3203 (BASF Praktikum) was gate-open. Fix: any
`posting_type` other than `EMPLOYMENT`/`UNKNOWN` returns the new reason
`NOT_EMPLOYMENT_POSTING`. Test: `tests/test_stage60_work_time_evidence.py::test_stage62_internship_posting_never_opens_the_gate`.
After the fix the gate-open count on this data is **5**.

## Observations not changed (operator decisions)

- #772: `start_date = EARLIER_REQUESTED` does not block the gate. The candidate starts
  01.01.2027; whether an earlier request is a barrier is a product decision, so it was not changed.
- DevOps titles with PARTIAL tier pass the gate (#52, #772). The tier model treats them as
  Python-adjacent; they are listed for manual review and are weak real fits. No rule was changed
  without a measured false-positive set larger than these two.
- None of the six states a German level, so the B1 constraint cannot be confirmed from the data.
