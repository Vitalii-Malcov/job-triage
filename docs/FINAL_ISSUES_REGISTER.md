# Final issues register - main @ 5834fe7 (+ PR #62 / #63), 2026-11-12

Sources read: Hard Code Review (8), `issues-index.md` (89 rows), `AUDIT_2026_11_06_REMEDIATION_MATRIX.md`, Stage 62 inventory, GitHub Issues (none exist), TODO/FIXME in code (none), PR review comments (none besides Codex rounds). Unavailable: `job-triage-audit.md` and `findings/*.md`.
Evidence tags: **verified in code 11.11** = I read the cited file/line on this checkout; **per matrix, not re-verified** = carried, not independently tested; **not re-verified** = no evidence gathered.

| ID | Source | Problem | Sev | Status | Evidence |
|---|---|---|---|---|---|
| HCR-1.1 | HardReview | API key non-constant-time/unlimited | High | FIXED | verified in code 11.11: app/security/auth.py hmac.compare_digest + begin_auth_attempt |
| HCR-1.2 | HardReview | secrets in repr(settings) | High | FIXED | verified in code 11.11: config.py SecretStr |
| HCR-1.3 | HardReview | response-draft recipient not pinned | Med | FIXED | verified in code 11.11: response_drafts.py:227 pinned_recipient |
| HCR-1.4 | HardReview | UI sessions not revocable | Low | FIXED | verified in code 11.11: ui/auth.py logout registry |
| HCR-2.1 | HardReview | processed_email unique key | Med | FIXED | verified in code 11.11: uq_processed_email_messages_source_message_id |
| HCR-2.2 | HardReview | XING dedup merges postings | Med | NEEDS_OPERATOR | not re-verified; XING stubs lack reference id |
| HCR-2.3 | HardReview | manual runs bypass lease | Med | PARTIAL | per matrix, not re-verified |
| HCR-2.4 | HardReview | async endpoints block loop | Med | PARTIAL | verified in code 11.11: run_off_loop in jobs.py; other routers not checked |
| SEC-01 | issues-index | see issues-index.md | P0 | FIXED | per matrix, not re-verified |
| SEC-02 | issues-index | see issues-index.md | P1 | FIXED | verified in code 11.11: auth.py is_strong_secret; Settings rejects template |
| SEC-03 | issues-index | see issues-index.md | P1 | FIXED | verified in code 11.11: auth.py strong-key path |
| SEC-04 | issues-index | see issues-index.md | P2 | FIXED | fixed in PR #62: httpx helper + SDK Defaults; tests/test_overnight_telegram_link_preview.py, test_telegram_gmail_draft_bot.py |
| SEC-05 | issues-index | see issues-index.md | P3 | FIXED | verified in code 11.11: main.py docs gated by API_DOCS_ENABLED |
| SEC-06 | issues-index | see issues-index.md | P3 | FIXED | verified in code 11.11: telegram_bot.py:580 private-chat check (ChatType.PRIVATE); callback sender check near line 896 |
| SEC-07 | issues-index | see issues-index.md | P3 | OPEN | not re-verified; xing_email.py filters by sender/subject only; no SPF/DKIM check seen |
| API-01 | issues-index | see issues-index.md | P1 | FIXED | verified in code 11.11: security/id_bounds.py |
| API-02 | issues-index | see issues-index.md | P1 | FIXED | per matrix, not re-verified |
| API-03 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| API-04 | issues-index | see issues-index.md | P2 | PARTIAL | verified in code 11.11: UniqueConstraints in application_documents.py; Stage 62 S62-04 notes bewerbung_drafts has none |
| API-05 | issues-index | see issues-index.md | P3 | OPEN | error shapes vary by router (api/http_errors.py exists; not exhaustively checked) |
| API-06 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: id tiebreak in some queries (repositories.py:115,783) but list_jobs orders by last_seen_at only (line 752) |
| API-07 | issues-index | see issues-index.md | P3 | OPEN | no extra=forbid on PATCH models (grep on app/models/job.py empty) |
| API-08 | issues-index | see issues-index.md | P3 | OPEN | no 202/async pattern in collectors/automation routers (grep empty) |
| API-09 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: datetime_utils.ensure_utc exists; coverage of all responses unchecked |
| API-10 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: 10/13 except blocks in follow_ups/response_drafts routers |
| API-11 | issues-index | see issues-index.md | P3 | NOT_REPRODUCED | UI redirects are relative 303 to /ui/... (ui/routes.py); no absolute http Location |
| ARC-01 | issues-index | see issues-index.md | P2 | PARTIAL | verified in code 11.11: run_off_loop in jobs.py |
| ARC-02 | issues-index | see issues-index.md | P1 | FIXED | per matrix, not re-verified |
| ARC-03 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| ARC-04 | issues-index | see issues-index.md | P2 | OPEN | not re-verified |
| ARC-05 | issues-index | see issues-index.md | P3 | OPEN | import-graph tests exist; edges not re-counted |
| ARC-06 | issues-index | see issues-index.md | P3 | NEEDS_OPERATOR | verified in code 11.11: no journal_mode/WAL; enabling rewrites the production DB header |
| ARC-07 | issues-index | see issues-index.md | P1 | OPEN | verified in code 11.11: no heartbeat hit in services/scheduler.py |
| ARC-08 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: repositories.py 1791 lines |
| ARC-09 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: db/session.py:40 module-level create_engine |
| CQ-01 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: company normalisation in repositories.py and email_matching.py |
| CQ-02 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: imaplib in collectors/xing_email.py, api/routers/collectors.py, providers/email/base.py |
| CQ-04 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: no mypy/pyright |
| CQ-05 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: collector_runner.py 1389 lines; complexity not re-measured |
| DB-01 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| DB-02 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified; scripts/backup_sqlite.py exists |
| DB-03 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: FKs present in jobs/telegram/gmail models; the 39 originally missing not recounted |
| DB-04 | issues-index | see issues-index.md | P3 | NEEDS_OPERATOR | retention/encryption policy is an operator/legal decision |
| DB-05 | issues-index | see issues-index.md | P3 | OPEN | redundant indexes not re-measured |
| DB-06 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: no server_default in jobs.py model vs migrations |
| DEP-01 | issues-index | see issues-index.md | P1 | PARTIAL | verified in code 11.11: constraints.txt pins (ruff==0.17.3); no hashed lockfile |
| DEP-02 | issues-index | see issues-index.md | P3 | FIXED | pre-commit ruff rev v0.12.10 vs pinned 0.17.3: bumped to v0.17.3 (tag exists) in this PR |
| DEP-03 | issues-index | see issues-index.md | P3 | PARTIAL | Dependabot added (PR #62); pip-audit not added or run |
| DOC-01 | issues-index | see issues-index.md | P2 | FIXED | README status table updated (PR #62) |
| DOC-02 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| DOC-03 | issues-index | see issues-index.md | P3 | OPEN | 7 stale claims not re-enumerated |
| DOC-04 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: no responses=/security metadata in main.py/routes.py |
| DOC-05 | issues-index | see issues-index.md | P3 | PARTIAL | CONTRIBUTING added (PR #62); LICENSE: NEEDS_OPERATOR (owner chooses) |
| FLOW-01 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| FLOW-02 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| FLOW-04 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| FLOW-05 | issues-index | see issues-index.md | P1 | FIXED | per matrix, not re-verified |
| FLOW-06 | issues-index | see issues-index.md | P3 | OPEN | not re-verified |
| FLOW-07 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: follow_up_send.py documents FAILED vs UNCERTAIN resolution |
| FLOW-08 | issues-index | see issues-index.md | P3 | FIXED | verified in code 11.11: NFC in application_response_classifier.py:141,156 |
| FLOW-09 | issues-index | see issues-index.md | P3 | OPEN | not re-verified |
| FLOW-10 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: follow_up_send.py:300 handles a newer reply |
| FLOW-11 | issues-index | see issues-index.md | P3 | FIXED | verified in code 11.11: telegram_operator_control.py APPROVE moves PACKAGE_READY->APPROVED |
| FLOW-12 | issues-index | see issues-index.md | P1 | FIXED | per matrix, not re-verified |
| FLOW-13 | issues-index | see issues-index.md | P1 | FIXED | per matrix, not re-verified |
| FLOW-14 | issues-index | see issues-index.md | P3 | FIXED | Stage 62 gate; docs/STAGE62_* |
| FLOW-15 | issues-index | see issues-index.md | P1 | OPEN | not re-verified; no change seen in telegram_operator_render.py |
| OPS-01 | issues-index | see issues-index.md | P1 | FIXED | per matrix, not re-verified; .gitattributes |
| OPS-02 | issues-index | see issues-index.md | P1 | OPEN | verified in code 11.11: compose healthchecks exist; no alerting/supervision |
| OPS-03 | issues-index | see issues-index.md | P1 | PARTIAL | verified in code 11.11: docs/BACKUP_AND_ROLLBACK.md describes rollback; not automated |
| OPS-04 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: no request id in app/ |
| OPS-05 | issues-index | see issues-index.md | P3 | FIXED | verified in code 11.11: ci.yml permissions/timeouts |
| OPS-06 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: no OS/python matrix in ci.yml |
| OPS-07 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: Dockerfile FROM python:3.13-slim unpinned, no resource limits |
| OPS-08 | issues-index | see issues-index.md | P3 | FIXED | per matrix, not re-verified |
| PERF-01 | issues-index | see issues-index.md | P2 | OPEN | verified in code 11.11: no eager-loading in ui/queries.py; N+1 not re-measured |
| PERF-02 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| PERF-03 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: no index on jobs.last_seen_at |
| PERF-04 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: fit_cache.compute_rules_hash hashes module sources |
| RUN-01 | issues-index | see issues-index.md | P3 | OPEN | CI test job 20m35s; per-file times not re-measured |
| RUN-02 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: tests/test_imap_deadline.py uses openssl |
| RUN-03 | issues-index | see issues-index.md | P3 | PARTIAL | CI: 1968 warnings, 1965 = ResourceWarning unclosed sqlite connection, test-side (attributed to whichever test triggers GC). Offline-script leak fixed in this PR; central test-side dispose tried, no effect, reverted |
| TEST-01 | issues-index | see issues-index.md | P1 | PARTIAL | per matrix, not re-verified |
| TEST-02 | issues-index | see issues-index.md | P2 | OPEN | not re-verified |
| TEST-03 | issues-index | see issues-index.md | P2 | PARTIAL | verified in code 11.11: concurrency tests for PostgreSQL in tests/integration; SQLite races not re-checked |
| TEST-04 | issues-index | see issues-index.md | P2 | FIXED | verified in code 11.11: tests/test_migrations.py and migration-parity tests exist |
| TEST-05 | issues-index | see issues-index.md | P2 | PARTIAL | verified in code 11.11: route-auth matrix test; no OpenAPI snapshot |
| TEST-06 | issues-index | see issues-index.md | P1 | PARTIAL | CI runs --timeout=600 --cov (CI log: coverage 96%); no coverage gate threshold seen |
| TEST-07 | issues-index | see issues-index.md | P3 | OPEN | verified in code 11.11: time.sleep in 3 PostgreSQL integration tests |
| TEST-08 | issues-index | see issues-index.md | P3 | OPEN | not re-verified |
| TEST-09 | issues-index | see issues-index.md | P3 | OPEN | not re-verified |
| TEST-12 | issues-index | see issues-index.md | P3 | OPEN | duplicate of RUN-01 |
| UI-01 | issues-index | see issues-index.md | P1 | PARTIAL | verified in code 11.11: ui/queries.py budget on assess_job |
| UI-02 | issues-index | see issues-index.md | P3 | OPEN | not re-verified |
| UI-03 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: html lang=de and label for=api_key present; table scope not checked |
| UI-04 | issues-index | see issues-index.md | P3 | PARTIAL | verified in code 11.11: Cache-Control no-store set in ui/routes.py:52 |
| AI-01 | issues-index | see issues-index.md | P3 | OPEN | LLM-provider stub text in providers/bewerbung/base.py; README states no LLM |
| FINAL-01 | Daily Discovery copy run | Markdown section headers (`**Ihr Profil**`, `### ...`) not recognised by `skill_extractor`; requirements below them (Python/FastAPI/PostgreSQL in the Dockweiler ad) were dropped | P1/P2 | FIXED | reproduced on copy: id 3245 extracted [git, oauth, rest]; after fix Python/SQL/PostgreSQL found; 329 of 3 535 stored jobs change skill sets (66 gain Python); `tests/test_skill_extractor_markdown_headers.py`; stored scores/fit cache refresh needs the operator |
| S62-03 | Stage62 | weak production API_KEY | P2 | NEEDS_OPERATOR | operator-owned |
| S62-05 | Stage62 | gate PARTIAL tier for DevOps / earlier start | P3 | NEEDS_OPERATOR | product decision |
| S62-08 | Stage62 | 28 BA reference conflicts | P2 | NEEDS_OPERATOR | never auto-resolved |
| S62-09 | Stage62 | no second off-disk backup | P2 | NEEDS_OPERATOR | needs another drive |

## Totals (101 rows)
- FIXED: 26
- NEEDS_OPERATOR: 7
- NOT_REPRODUCED: 1
- OPEN: 37
- PARTIAL: 30
- Rows with code-level evidence gathered in this run: 55; rows carried per matrix or not re-verified: 46.

No P0/P1 defect found that is fixable without the operator. Open P1 (OPS-02 alerting, ARC-07 liveness, FLOW-15, OPS-03 automation) need design/operator decisions and are not fixed here.
