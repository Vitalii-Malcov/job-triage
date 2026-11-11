# Stage 65 — Final security report (2026-11-11)

Scope: merge PR #60, make every Telegram outbound path opt-in (fail-closed), re-check the other
senders, confirm API-key rotation readiness, rehearse Daily Discovery on a fresh copy, review the
five named candidates, independent Codex review.
**Production writes: 0. `.env` unchanged. API_KEY unchanged. Telegram token unchanged. Bewerbungen sent: 0.**

## Status

**PARTIAL — OPERATOR ACTION REQUIRED.** Code is ready for manual production discovery. Left for the
operator: merge this PR after green CI, the separate "DAILY DISCOVERY PRODUCTION WRITE" approval, and
API-key rotation. Nothing security-relevant is open.

## 1–2. PR #60

Verified before merging: open, base `main`, head `62d9e4f` as expected, CI green on that head, MERGEABLE/CLEAN,
no `.env`/DB change in the diff, lease heartbeat fix + word-aware internship regex + regression tests present,
Codex round 4 ACCEPT recorded for that head. Merged with a plain merge commit (`--match-head-commit`, no admin,
no branch deletion): new main **`b60491f`**. CI on `b60491f` (run 43025329227): **success**.

## 3. Telegram fix (fail-closed)

| Path | Before | Now |
|---|---|---|
| Control-center bot (lifespan, `start_bot_supervised`, `start_bot`) | started whenever token existed | only if `TELEGRAM_BOT_ENABLED=true` AND token AND chat id (`bot_start_block_reason`) |
| Legacy job alert (`TelegramNotifier`, collector runs + `POST /jobs`) | sent whenever token + chat existed | only if `TELEGRAM_JOB_ALERTS_ENABLED=true` (`job_alert_credentials`, 3 construction sites) |
| Vacancy feed / bewerbung draft / approval / Gmail draft / operator cards / daily digest | own flags (default false) | unchanged, verified |

Both flags default to false, are shown in the startup safety line (booleans only), listed in `.env.example`, and
forced off by `scripts/run_operator_ui.py` and `scripts/stage22_refresh_ba.py`. Refusal reasons contain no secrets.
**Behaviour change for production:** after this PR the bot and job alerts are OFF until the operator sets the flags in
`.env` (not done here).

Tests (mocks only; no Telegram client, no message): `tests/test_stage65_telegram_opt_in.py` (token+flag absent/false → no
start; flag true → mock start; flag true without token/chat → refusal without leak; uvicorn/Docker-style default
lifespan → no start; manual-UI env → no start even with the flag in the environment; failed start sends nothing),
`tests/test_stage65_telegram_outbound_matrix.py` (alert not sent with credentials only, sent to a mock only with the flag; a
source scan proves every notifier construction uses the gated credentials; feed sweep no-op; operator card DISABLED;
credentials never turn any outbound flag on). The Stage 64 matrix test was updated: a plain web start no longer starts the bot.

## 4. Outbound matrix

| Component | Credentials | Flag | Result (test) |
|---|---|---|---|
| Telegram bot | present | off | no startup |
| Telegram bot | present | on | mock startup only |
| Job alert | present | off | nothing sent |
| Job alert | present | on | mock send only |
| Gmail / SMTP | present | off | no connection opened (Stage 64 test) |
| Scheduler, auto-prepare, outbound sending, digest, feed, operator control | – | off | defaults false |

## 5. API key

`docs/STAGE63_API_KEY_ROTATION_PLAN.md` procedure re-proved by `tests/test_stage64_api_key_rotation.py` (part of the full suite):
old key 401, new key 200, old UI sessions invalid, rollback works, neither value logged. Production key still weak (23 chars),
**not changed.** `API KEY ROTATION — OPERATOR APPROVAL REQUIRED`.

## 6. Daily Discovery rehearsal (fresh consistent copy, live BA API reachable)

Copy: `E:\JobTriage-Temp\s65\rehearsal.db`; Stage 65 code via PYTHONPATH; production not opened for writing.

| Metric | Before | After pass 1 | After pass 2 |
|---|---|---|---|
| jobs | 3 234 | 3 525 (+291) | 3 525 |
| applications / events | 25 / 54 | 25 / 54 | 25 / 54 |
| found / new / updated | – | 1 395 / 291 / 5 | 1 396 / 0 / 0 |
| created / failed / skipped conflict | – | 291 / 0 / 0 | 0 / 0 / 0 |
| match analyses | – | 291, 0 failed | 0 |
| fit rows written | 3 234 | 296 | 0 |
| fit tiers NEEDS_OPERATOR / NOT_SUITABLE / PARTIAL / STRETCH | 948 / 2142 / 9 / 135 | 1027 / 2335 / 9 / 154 | same |
| reference conflicts (reported, never merged) | – | 24 | – |
| lease/heartbeat busy or lost lines | – | 0 | 0 |
| integrity / FK / alembic | ok / 0 / a7c3e5f1b9d2 | ok / 0 / same | ok / 0 / same |

All exits 0. No new PARTIAL_MATCH or GOOD among the 291 new vacancies; pass 2 is idempotent. Duplicate Application Gate and
Positive Full-Time Evidence are unchanged code covered by the suite.

## 7. Candidates (read-only from production; no Bewerbung created)

Profile: Junior Python/Backend, Heusenstamm / Rhein-Main, B1, start 01.01.2027, 45–50 k € brutto.

| # | Vacancy | Fit tier | Concrete reading |
|---|---|---|---|
| 324 | (Junior) Python Entwickler, Jobriver.de, Darmstadt (XING) | NEEDS_OPERATOR | Right role and salary overlap (44.5–59 k €, Vollzeit). Posting text is only the salary line: no skills, language or work mode known; Darmstadt route not yet verified. Needs a manual read of the full posting. |
| 212 | Datenanalyst, BKK Linde, Wiesbaden (BA) | PARTIAL_MATCH | SQL required, Python/R "wünschenswert" (skill score 100). But it asks for a completed (Wirtschafts-)Informatik/Wirtschafts degree "oder vergleichbare Qualifikation" and data-analysis experience; it is a BI/reporting role, not backend; ~40 km, hybrid. Not a Junior Python/Backend fit on content. |
| 551 | Softwareentwickler Datenbank, outsourcing4work for a media customer, Eltville (BA) | PARTIAL_MATCH | Title matches "Junior Softwareentwickler" but the text names Delphi/Java/C#, a degree or "adäquate Berufserfahrung", OO design and customer projects (skill score 50); ~50 km; Python absent. Weak fit. |
| 310 | (Junior) Backend Engineer, Instaffo, Frankfurt (XING) | NEEDS_OPERATOR | Role and salary (45–60 k €) fit; no skills/stack/language/mode extractable; Frankfurt route unchecked. Manual read. |
| 347 | (Junior) Backend Engineer, RUBINLAKE, Frankfurt (XING) | NEEDS_OPERATOR | As 310; salary 42.5–57.5 k €, lower edge below the 45 k € target. Manual read. |

Only #324, #310, #347 match the target role on the title and need the operator to open the original posting; #212 and #551 are
doubtful on content. The score (45–81) was not used as proof.

## 8. Tests / CI / Codex

| Check | Result |
|---|---|
| Ruff check / format | pass |
| Full local suite (started before the final job-alert edit, so it does not include it) | 8 932 passed, 168 skipped, 0 failed |
| Targeted after the final change (Stage 65 ×2, Stage 64 matrix, collector endpoints, bot, diagnostics, health) | 154 passed |
| GitHub CI on this PR (suite, PostgreSQL, Docker) | see the PR checks — this is the authority for the final diff |
| Codex round 1 | no code findings; REJECT only because its read-only sandbox cannot run pytest |
| Codex round 2 (test evidence supplied) | **ACCEPT**, no P0–P3; re-checked bot start, alerts, feed, digest, operator transport |

## 9. Production state

SHA-256 prefix before/after Stage 65: `a2cf5973` / `a2cf5973`; 3 234 jobs, 25 applications, 54 events; Alembic `a7c3e5f1b9d2`.
No migration in this PR.

## 10. First production Daily Discovery (not run)

Sequence: `docs/STAGE64_FINAL_REPORT.md` §"Production readiness" (backup to `E:\JobTriage-Backups`, dry run, then
`stage22_refresh_ba.py --apply --create-new --snapshot`, match, assess, verify 25/54). Expected: about 290 new vacancies, no new
GOOD/PARTIAL. Run it from a checkout of main that contains this PR; do not start the app with plain uvicorn.

`DAILY DISCOVERY PRODUCTION WRITE — APPROVAL REQUIRED` (the Stage 61 approval does not carry over).

## 11. Remaining operator decisions

1. Merge this PR after CI.
2. Whether to enable `TELEGRAM_BOT_ENABLED` / `TELEGRAM_JOB_ALERTS_ENABLED` in production `.env` (off by default now).
3. Approve the production discovery write.
4. API-key rotation.
5. 5 pre-existing duplicate BA references and the reported reference conflicts: manual review.
