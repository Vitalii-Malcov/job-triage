# Stage 64 — Final report (2026-11-11)

Scope: close the Stage 62–63 leftovers (internship regex, lost lease), prove crash recovery,
prepare key rotation, check Telegram/Gmail/outbound, rehearse the full Daily Discovery on a copy,
independent Codex review. **Production writes: 0. Bewerbungen sent: 0. `.env` unchanged.**

## Final status

**PARTIALLY READY — MANUAL MODE ONLY, one step from READY.** Code and procedure are ready for
the first production Daily Discovery in manual mode; it becomes `READY FOR PRODUCTION DAILY
DISCOVERY` once (1) this PR is merged after green CI and (2) the operator says "go".
Remaining non-code blockers: the API key is still weak (rotation needs approval), see the
Telegram note (§ Security). No critical issue is open.

## GitHub

| Item | Result |
|---|---|
| PR #59 (Stage 63 docs) | merged as `5c112ff1a9b6eaf0af08d923cf6ab63cf2f0b89e` after review: 5 docs-only files, no secrets (grep hits were only the text `API_KEY=` in an instruction), CI green on its head `7f86bbc`, CLEAN |
| CI on the new main `5c112ff` | run 42979811718: success (checked on the new main, not the old one) |
| Stage 64 PR | branch `fix/stage64-discovery-reliability` from `5c112ff`, 5 commits, **not merged** (needs the operator) |
| Stage 64 CI | see the PR checks (recorded there; not copied here) |

## Defects

### 1. Internship regex (fixed)

Stage 62's `intern(ship)?` had no word boundary. On the rehearsal copy 21 titles matched wrongly
("Interne Revision", "Mietwagen International", "Internet-Filiale", "interne Systeme"); none of
them was in an auto-prepare tier, so no gate-open candidate was affected, but any such future
vacancy would have been silently excluded. Now `is_non_employment_title()` in
`app/services/shortlist_gate.py`:
blocks `Praktikum/Praktikant/Praktika`, whole-word `Intern`/`Internship`, `Werkstudent`,
`Working Student`, Abschluss-/Bachelor-/Master-/Diplomarbeit, `(Master|Bachelor)thesis`,
`Azubi/Auszubildende`, `Duales Studium`, and `Ausbildung` (offer forms `Ausbildung zum/zur/2027/ab/Beginn/Start`
always; the bare word unless it is a qualification clause: `mit/nach/ohne/oder/abgeschlossene…/erfolgreiche… Ausbildung`).
Trainee programmes are still admissible; no other gate was changed.

| Title (all from the brief) | Result |
|---|---|
| Praktikum Python Developer, Python Internship, Intern Software Engineer, Werkstudent Python, Ausbildung Fachinformatiker | blocked |
| International Python Developer, Interne Automatisierung Python, Internet-Filiale Entwickler, Softwareentwickler interne Systeme, Trainee Python Developer | not blocked |

Tests: parametrized title cases (32) and gate-level cases (5) in `tests/test_stage60_work_time_evidence.py`.
Real data (copy): old rule 227 matches, new rule 274 (19 released as false positives at that time, +66
real apprenticeships/dual studies such as "Ausbildung Fachinformatiker …" newly caught by title);
gate-open stays **5** (52, 212, 551, 772, 2351), `NOT_EMPLOYMENT_POSTING` 1 (#3203). No new duplicate applications.

### 2. Lease lost during discovery (root cause established by reproduction, fixed)

Evidence, Stage 63 rehearsal (`automation_runs` + `jobs.first_seen_at` of the copy): run 2 started
06:22:54.38, last successful renewal 06:23:04.4 (`lease_expires_at` 06:23:34.41 = +30 s TTL), the
83 vacancies were created between 06:23:13.8 and 06:23:20.2 (83 transactions in 6.4 s), the run
ended FAILED/LeaseLostError at 06:23:20.23. The heartbeat renewal due at ~06:23:14.4 therefore ran
during the create burst.

Mechanism, reproduced on a throw-away SQLite (`E:\JobTriage-Temp\s64\repro_lease.py`): a writer
that commits transactions back to back (each ~70 ms, no idle gap) starves the heartbeat's own
connection; SQLite's busy handler polls with sleeps and never gets the lock inside its 5 s busy
timeout; the renewal raises `database is locked` (SQLITE_BUSY, code 5) after ~5.6 s and the heartbeat
treated that single error as "ownership lost" although the lease (TTL 30 s) was still valid.
Reproduction: 87–88 writer transactions in 6.7 s → `lease_lost=True`; with 30 ms idle gaps between
transactions, 235 transactions in 25 s → no loss. The second Stage 63 run succeeded because its
heartbeat tick fell outside the burst; it was timing luck, not a different state.
Not the cause: TTL expiry, a competing process (none was running), a protective-code misreading
of another event (the error type was genuinely `OperationalError`/BUSY).

Fix (minimal; TTL unchanged, locking unchanged, exit code 3 unchanged, still one worker only):

1. `app/services/automation.py` `_RunLeaseHeartbeat`: SQLITE_BUSY/LOCKED is retried (1 s) only while less
   than one TTL has passed since the last successful renewal; a refused renewal (`False`), any other
   error, TTL exceeded, **or a success that arrived after the TTL** (Codex R1: the renewal compares
   with the clock from before it waited) still ends the run. The log line now carries `sqlite_code`.
2. `scripts/stage22_refresh_ba.py`: `WRITER_YIELD_SECONDS = 0.05` after each write transaction so a
   renewal can get the lock (this removes the starvation; the retry is the safety net).

Regression tests: 10 cases in `tests/test_stage64_lease_heartbeat_contention.py` (fake BUSY, past-TTL busy,
refused/other-error still immediate, real two-connection lock, late success). With the old code 5 of
them fail (checked by disabling the helper).

Rehearsal after the fix (fresh copy, §Discovery): 280 vacancies created in one run, **no
heartbeat busy/error line, exit 0**.

### 3. Crash recovery and idempotency (proved on throw-away SQLite)

| Scenario | Evidence |
|---|---|
| normal apply; repeated apply | existing tests + rehearsal pass 2: `new=0`, `created: 0` |
| stop after a partial write, then rerun | `test_a_run_stopped_after_a_partial_write_is_continued_without_duplicates`: 1 of 3 created, rerun creates 2, third run 0, 0 duplicate fingerprints, protected digest unchanged |
| two parallel workers | `exclusive_run_lock` + collector slot refusal (exit 2, nothing written) |
| lease lost | exit 3 `ALERT … partial write`, run recorded FAILED, rerun continues |
| SQLite temporarily unavailable | create stops loudly (`OperationalError`), 0 rows written, rerun creates the vacancy |
| interrupted collector / failed search | existing tests (`a failed search … apply writes nothing`) |
| same BA reference twice; conflicting reference | created once; conflicts listed and never merged (26 conflicts in the rehearsal) |
| Bewerbungen / events | `applications` 25 and `application_events` 54 byte-identical to production after every pass |

Data note (pre-existing, not from this stage): production already has 5 pairs of BA vacancies with the
same `source_reference` (ids 105/2356, 644/1787, 363/2353, 1156/2328, 169/2354). Unchanged; for manual review.

## Security

| Item | Result |
|---|---|
| API key | **API KEY ROTATION READY — APPROVAL REQUIRED.** Plan `docs/STAGE63_API_KEY_ROTATION_PLAN.md`; production key still 23 characters = weak, not changed. Rotation is proven on an isolated configuration (`tests/test_stage64_api_key_rotation.py`): old key 401, new key 200, UI sessions of the old key invalid, rollback restores access, diagnostic `weak → strong`, neither value in a log. Scan for the key value (value never printed): 0 hits in 603 commits of git history, 0 in docs/app/scripts/tests/postman, 0 in logs/scratch files on E: (`s62\key.txt` is a throw-away 43-character test key, not the production one) |
| Telegram | token **and** chat id are present in `.env` (Stage 61 wrongly said "no token"). **No flag is needed for the legacy channel:** a plain web start (uvicorn/docker with this `.env`) starts the Telegram bot (`telegram_bot._start_bot_attempt`) and `TelegramNotifier.send_job` can alert the operator chat for a new APPLY ≥ 80 vacancy of an authenticated REST/collector run. `scripts/run_operator_ui.py` and `stage22_refresh_ba.py` blank the token for their process, so the documented operator path never starts it. The startup safety line now shows `telegram_token_configured` / `telegram_chat_configured` (presence only). Do not start the app with plain `uvicorn` on production while this is not intended |
| Gmail | credentials present, `OUTBOUND_SENDING_ENABLED=false`, draft flag false: no mail connection is opened (SMTP/IMAP doubles raise if touched, test) |
| Scheduler / auto-prepare / outbound / Gmail & follow-up cycles / digest / operator control | all false in `.env` and in code defaults |

| Component | Configured | Enabled | Can send |
|---|---|---|---|
| Telegram (bot + job alert) | yes (token + chat) | no flag exists; starts on a plain web start, **blank under the operator launcher and discovery scripts** | to the operator chat only; employers never |
| Gmail | yes | no | no |
| Scheduler | – | no | no |
| Auto-prepare | – | no | no |
| Outbound | – | no | no |

No real message was sent and no real account was contacted; everything used doubles.

## Discovery (full rehearsal on a fresh consistent copy of production)

Copy SHA-256 equal to production (`a2cf5973…`), integrity ok, 25/54, then the Stage 63 pipeline with the Stage 64 code, twice:

| Metric | Pass 1 | Pass 2 (idempotency) |
|---|---|---|
| found | 1 400 | 1 401 |
| existing / unchanged | 1 098 / 1 092 | 1 379 / 1 375 |
| new / created | 280 / 280 (0 failed, 0 conflict-skips) | 0 / 0 |
| updated | 2 | 0 |
| reference conflicts (not merged) | 26 | – |
| fingerprint duplicates | 0 | 0 |
| match | 280 analyses, 0 failed | 0 |
| assess | 282 rows | 0 |
| exit codes / heartbeat busy-or-error lines | all APPLIED / 0 | all APPLIED / 0 |
| duration | apply 63 s, match 65 s, assess 12 s | 8 s, 4 s, 3 s |

Final state of the copy: 3 514 jobs, integrity ok, foreign_key_check empty, `applications` and
`application_events` identical to production, three COMPLETED `ba_refresh` runs. A later extra run
created 3 more (BA publishes continuously).

Fit result of the new vacancies: NOT_SUITABLE 189, NEEDS_OPERATOR 73, STRETCH 18, **GOOD 0, PARTIAL 0**
(MAYBE does not exist as a tier). Reasons, not filters: the new Python/backend titles are senior or
need German C1 / Java / .NET (3263, 3270, 3320, 3493), are 130–230 km away (3263, 3288, 3320, 3493), or are
not backend work (3288); #3264 is the already-applied Leani posting (duplicate guard works); #3336 is
fully remote with 70–90 k € but a Senior title and TypeScript/CI-CD requirements, a manual read only.
Gate-open 5 (52, 212, 551, 772, 2351) and the 10 Junior Python/Backend titles are unchanged from
`STAGE63_JOB_CANDIDATES.md`; the best ones to read first remain #324, #212, #551, #310, #347.
No Bewerbung was created, no mail or Telegram sent, no filter was loosened.

## Database

| Item | Value |
|---|---|
| Production SHA-256 before / after Stage 64 | `a2cf5973be486b45fa572ec5aa3e5c7ea865f11c27b979852db2ce7aebdb38df` / identical (measured at the start and at the end; mtime 10.11. 22:03) |
| Applications / events | 25 / 54 (unchanged, digest-identical on every rehearsal pass) |
| Integrity | production `ok`, foreign keys empty, no `-wal`/`-shm`, no server listening on 8000 |
| Copy changes | +280 jobs (+3 later), 3 `automation_runs`, fit/analysis rows; nothing else |

## Testing

| Check | Result |
|---|---|
| Ruff check / format | pass (670 files) |
| Targeted local run (Stage 64/60/62/23, lease, collector exclusion, refresh/match, startup diagnostics, Stage 15/16/20 safety, follow-up send) | **511 passed** |
| Full suite, PostgreSQL job, Docker | GitHub CI of the Stage 64 PR (the Linux suite is the authority; not repeated locally) |
| Codex (independent, read-only) | round 1 REJECT 3×P2 (retry could revive an expired lease; "Praktika"; "mit abgeschlossener Ausbildung" false block) → fixed; round 2 REJECT P2 (offer "Starte mit Ausbildung 2027 zum …" let through) → fixed; round 3 REJECT P2 (qualification + "zum") → fixed; **round 4 ACCEPT, no findings** |

Resources: no pytest/Codex ran at the same time as the rehearsal; peak working set of the discovery
process 151 MB; E: 229 GB free; `E:\JobTriage-Temp` 3.4 GB (s64 680 MB); nothing deleted.

## Production readiness — first Daily Discovery (do NOT run without the operator's go)

1. `git -C E:\Projects\job-triage-stage64 fetch` and confirm `origin/main` contains this PR's merge and its CI is green. Run the commands from a checkout of that main that has the production `.env` in the working directory (the discovery scripts need `BUNDESAGENTUR_API_KEY` from it): update `E:\Projects\job-triage` to main first (it is on an old docs branch today; keep your untracked files).
2. Safety flags: `Select-String -Path .env -Pattern '^(OUTBOUND|AUTOMATION_|TELEGRAM_.*ENABLED|TELEGRAM_GMAIL)'` → all `false`. Never start the app with plain `uvicorn`.
3. If the UI runs: stop it by its recorded PID only (`Get-Content E:\JobTriage-Temp\logs\ui_launcher.pid`), not by a name mask.
4. Verified backup: `.\.venv\Scripts\python.exe -m scripts.backup_sqlite --db runtime\job_triage_runtime.db --dest E:\JobTriage-Backups --label before-daily-discovery`; check the `.sha256` sidecar and `PRAGMA integrity_check`.
5. Dry run: `.\.venv\Scripts\python.exe scripts\stage22_refresh_ba.py --database runtime\job_triage_runtime.db`.
6. Apply: `... stage22_refresh_ba.py --database runtime\job_triage_runtime.db --apply --create-new --snapshot E:\JobTriage-Backups` (exit 0 expected; exit 3 → read the ALERT line, rerun is idempotent unless it says "protected tables changed" → stop and restore).
7. Deduplication is part of step 6 (reference/fingerprint guard); check "reference conflicts" and `created`.
8. `... stage22_match_vacancies.py --database runtime\job_triage_runtime.db --apply --snapshot E:\JobTriage-Backups`.
9. `... stage21_assess_vacancies.py --database runtime\job_triage_runtime.db --apply --snapshot E:\JobTriage-Backups`.
10. Verify: `applications` = 25, `application_events` = 54, integrity ok, foreign keys empty, no new fingerprint duplicates.
11. `... scripts\run_operator_ui.py --database runtime\job_triage_runtime.db --snapshot E:\JobTriage-Backups`, then `/ui/review` (filter *junior*): read the new items yourself.

Expected from today's data: about 280 new vacancies, 0 GOOD/PARTIAL, a handful of NEEDS_OPERATOR to read.

## Remaining items

| Item | Owner / action |
|---|---|
| Merge this PR after CI | operator |
| First production Daily Discovery | operator "go", then the sequence above |
| API-key rotation | operator "go"; plan in `STAGE63_API_KEY_ROTATION_PLAN.md` |
| Telegram bot starts on a plain web start with this `.env` | decide: keep (documented) or remove the token from `.env`; do not use plain `uvicorn` on production meanwhile |
| 5 pre-existing duplicate BA references; 26–28 reference conflicts | manual review, never auto-merged |
| `EARLIER_REQUESTED` start dates, DevOps tiers, second off-disk backup (S62-04…09) | operator decisions |
| `main` checkout `E:\Projects\job-triage` on an old docs branch | switch when convenient |
