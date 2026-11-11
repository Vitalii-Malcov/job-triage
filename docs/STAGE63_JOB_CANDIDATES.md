# Stage 63 — Job candidates (manual review list)

Method: the merged `main` (6f5c7f0) code run over a **copy** of the production SQLite after the
Stage 63 rehearsal (`E:\JobTriage-Temp\s63\daily.db`: 3 513 vacancies = 3 234 production + 279 new
Bundesagentur postings). `automatic_preparation_check`, `assess_job` and the review-queue
title rule were used as they are. Production was not touched; nothing was prepared, approved or
sent; no link was opened by the tool (XING links are per-recipient tracking redirects and must
only be opened by the operator, see `app/collectors/xing_email.py`).

**A gate-open or fit tier is a suggestion for reading, not proof that the job fits.** Distances are
BA straight-line values from Heusenstamm Bahnhof (a car route is never shorter); the candidate's
confirmed German level is B1, available from 01.01.2027.

## Result in one line

**No vacancy is confirmed suitable.** None can be applied for without reading the posting. The
most plausible ones to read first are #212, #551 (BA, gate-open) and the XING junior stubs
#324, #310, #347 (need the XING posting opened by you to learn skills, language and remote).

## Gate-open (5, after the Stage 62 fix; #3203 BASF Praktikum is now `NOT_EMPLOYMENT_POSTING`)

| ID | Vacancy / employer | City, distance | URL | Remote | Work time | Language | Tech | Salary | Duplicate | Fit | Recommendation |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 212 | Datenanalyst (m/w/d) — Betriebskrankenkasse Linde | Wiesbaden, ~40 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1002528340-S) | partial home office mentioned, not confirmed | Full time, BA flag | none stated | Python, SQL (skills 100/100) | not stated ("Vergütung mit mehreren Sonderzahlungen") | CLEAR | PARTIAL_MATCH, entry level | **Read first.** Data role, not backend; check whether Python is really daily work |
| 551 | Softwareentwickler Datenbank (m/w/d) — outsourcing4work GmbH | Eltville, ~50 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1000008804-S) | no information | Full time, BA flag | none stated | no skills extracted (skills 50/100) | not stated | CLEAR | PARTIAL_MATCH, role PRIMARY | **Read.** Developer title, DB focus; staffing intermediary; clarify client, stack, car route |
| 2351 | QA und Testautomatisierung — Statistisches Bundesamt | Wiesbaden, ~39 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/19853-01506195-S) | home office by agreement | Full time, BA flag | none stated | Python, SQL; missing JavaScript, XML (50/100) | tariff (amount not stated) | CLEAR | PARTIAL_MATCH | Possible. Public employer; QA, not backend; check required test tooling |
| 52 | DevOps Engineer — Ratiodata SE | Frankfurt, ~12 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003567170-S) | partial home office mentioned | Full time, BA flag | none stated | CI/CD, Docker, Kubernetes; misses CI/CD, Kubernetes (50/100) | not stated | CLEAR | PARTIAL_MATCH, role OTHER | Weak: DevOps, not the target profile |
| 772 | DevOps Engineer — alpha-test GmbH | Mannheim, ~69 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003819581-S) | no information | Full time, BA flag | none stated | Docker, Git, Linux (100/100) | bonus mentioned, base not stated | CLEAR | PARTIAL_MATCH | Weak: DevOps, ~69 km on-site, asks for an earlier start than 01.01.2027 |

## Junior Python/Backend titles (review-queue rule, 10 incl. 2 not in queue)

| ID | Vacancy / employer | City | URL | Remote | Work time | Language | Tech | Salary (from posting text) | Duplicate | Fit | Recommendation |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 324 | (Junior) Python Entwickler — Jobriver.de | Darmstadt | [XING link](https://www.xing.com/m/biNWE1Wac8woKkqK7HsggO) (open by hand) | unknown | Vollzeit in text, **not confirmed** | none stated | not extracted | 44 500–59 000 € | CLEAR | NEEDS_OPERATOR, entry level | **Best of the stubs:** Python junior, near home; platform/agency listing: the employer may not be named, check it |
| 310 | (Junior) Backend Engineer — Instaffo GmbH | Frankfurt | [XING link](https://www.xing.com/m/W6FDAAgHNx9M6fUXcQEdgK) | unknown | Vollzeit in text, not confirmed | none stated | not extracted | 45 000–60 000 € | CLEAR | NEEDS_OPERATOR | Read; stack unknown (backend of what?) |
| 347 | (Junior) Backend Engineer — RUBINLAKE GmbH | Frankfurt | [XING link](https://www.xing.com/m/CrV68YHN5uRnwD67EiFNgM) | unknown | Vollzeit in text, not confirmed | none stated | not extracted | 42 500–57 500 € | CLEAR | NEEDS_OPERATOR | Read; stack unknown |
| 292 | (Junior) Python Softwareentwickler KI — ep group | Karlsruhe | [XING link](https://www.xing.com/m/sVVftvH48sqUgs6fcbJ-gL) | unknown | Vollzeit in text, not confirmed | none stated | not extracted | 48 500–67 000 € | CLEAR | NEEDS_OPERATOR, role PRIMARY | Only if remote/hybrid: Karlsruhe is ~120 km |
| 705 | (Junior) Software Developer Python — FERCHAU, Karlsruhe City | Karlsruhe, ~122 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/12265-446410_JB5275915-S) | unknown | Full time, BA flag | **German ~C1 inferred** ("sehr gute Deutschkenntnisse"), B1 confirmed → gap | Python, Linux (100/100) | not stated | CLEAR | NEEDS_OPERATOR | Good stack, but distance + language gap; ask about remote and German level |
| 752 | (Junior) Backend-Entwickler — FERCHAU, Karlsruhe City | Karlsruhe, ~122 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/12265-445951_JB5275886-S) | unknown | Full time, BA flag | German ~C1 inferred | SQL (50/100) | not stated | CLEAR | NEEDS_OPERATOR | As #705, weaker stack |
| 1337 | Junior Backend Engineer — Airbus Defence and Space | Immenstaad (Bodensee) | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/16947-970420688-S) | unknown | **unknown** | English professional | Ansible, Git, Linux, Terraform, Unix (60/100) | not stated | CLEAR | NEEDS_OPERATOR | Far away (Bodensee, well over 250 km), infra-flavoured; only if remote is possible |
| 328 | (Junior) PHP Backend Entwickler — Jobriver.de | Frankfurt | [XING link](https://www.xing.com/m/biNWE1Wac8woKkqK7HsggZ) | unknown | not confirmed | none stated | PHP | 40 000–55 000 € | CLEAR | NEEDS_OPERATOR | **PHP, not Python.** Listed by the title rule only |
| 1581 | Trainee Digitale Bildung & Softwareentwicklung — Zusammen mit der Ukraine e.V. | Fellbach, ~143 km | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1002875691-S) | partial | **part time** | German A1/A2 | JavaScript, Python | not stated | CLEAR | NOT_SUITABLE | Part time + distance: no |
| 3264 | Quereinsteiger als Junior Python-Entwickler — Leani Business Solutions | unknown | [BA](https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003943814-S) | partial (100 % stated in BA data) | Full time, BA flag | German ~B2 inferred | Python | not stated | **ALREADY_APPLIED** (application 25, same BA reference) | NOT_SUITABLE (duplicate) | Do not re-apply; existing Bewerbung is tracked |

## New in this run (279 BA postings; ids 3235+)

Tiers after matching and assessing on the copy: NOT_SUITABLE 189, NEEDS_OPERATOR 72, STRETCH 18,
**GOOD/PARTIAL 0**. Python/backend titles among them, all correctly rated down:

| ID | Vacancy / employer | City | Why not now |
|---|---|---|---|
| 3336 | Senior Softwareentwickler Python und TypeScript — Institut für Angewandte Informatik, Uni Leipzig | Leipzig, **fully remote** (work from Hessen allowed) | Senior title, TypeScript/CI-CD/Playwright required (50/100), asks for an early start; 70 000–90 000 €. The only remote + salary match — worth one manual read of the real requirements, but a senior role |
| 3270 | Senior C#/Python Developer — Insiders Technologies | Kaiserslautern, ~98 km | Senior, 3+ years, C#, German ~C1 |
| 3263 | Softwareentwickler Backend & KI-SaaS — ISR Information Products | Münster, ~225 km | German C1, Java, experienced |
| 3320 | Softwareentwicklung GisboTimer — Gisbo | Zweibrücken, ~136 km | German C1, distance |
| 3288 | Blender Technical Artist (Python) — W&S Digitalagentur | Esslingen, ~151 km | Not a backend role, distance |
| 3492 | Backend (.NET/C#) — Volksbank eG | Offenburg, ~187 km | .NET, distance |

## Honest conclusion

Real, directly applicable vacancies are not present in today's data. Reasons: (1) the entry-level
Python/backend supply within the 100 km limit is tiny in BA data; (2) the matching junior
postings come from XING e-mail stubs without structured data, so skills, remote and German level
are unknown until a human opens the posting; (3) the Karlsruhe cluster fails on distance and on a
likely C1 German requirement against a confirmed B1; (4) the remote results are senior roles.
Suggested manual order: #324, #212, #551, #310, #347, then #3336 as a stretch.
