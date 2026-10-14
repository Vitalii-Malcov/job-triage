# Stage 13B — SQLite pilot archive, CandidateProfile transfer, PostgreSQL rehearsal

Stage 13B moves JobTriage from the SQLite pilot database to the production PostgreSQL topology (`docs/DEPLOYMENT.md`) **without importing historical runtime or outbound state**. It performs no employer-facing action and contacts no external provider (Gmail, SMTP, XING, Bundesagentur, Telegram). `OUTBOUND_SENDING_ENABLED` and `TELEGRAM_GMAIL_DRAFT_ENABLED` stay `false` throughout. The remaining stages are separate and not started here: Stage 13C (real Gmail draft validation, using a separate non-production Gmail mailbox first) and Stage 13D (controlled live pilot).

Tooling: `scripts/candidate_profile_transfer_stage13b.py` (operator CLI) on top of `app/services/candidate_profile_transfer.py` (snapshot format, validation, import, comparison).

```
EXISTING SQLITE PILOT ──read only──► verified archive copy (outside the repo, read-only file)
        └── CandidateProfile facts + provenance ONLY ──► deterministic snapshot (JSON, digest)
                                                              │
FRESH PostgreSQL ── alembic upgrade head ── import (one transaction) ── semantic verification
        └── /health + /ready + profile read smoke ── pg_dump ── restore into a 2nd fresh DB ── verify
```

## 1. Archive policy — SQLite is historical after cutover

- The SQLite pilot (`stage11_pilot.db`, the file `DATABASE_URL` pointed to) becomes a **read-only historical record** at cutover. Nothing writes to it again; it is never migrated, vacuumed, checkpointed or patched.
- `archive` byte-copies it to a new file **outside the repository** (`C:\Users\<user>\AppData\Local\job-triage-backups\` on the pilot machine — keep every backup there, never delete), marks the copy read-only, and validates **the copy** with SQLite opened `mode=ro&immutable=1`:
  `PRAGMA integrity_check` = `ok`, exactly one `alembic_version` row equal to the application head, exactly one `candidate_profiles` row, per-table row counts (counts only, never contents).
- Consistency: the source must be in rollback-journal mode (header bytes 18/19 = 1) with **no** `-wal`/`-journal`/`-shm` sidecar — i.e. no open writer and no hot journal. Otherwise `archive` refuses; stop every process using the file instead of checkpointing it. The source SHA-256 is taken before and after the copy and must equal the archive's SHA-256; the source's size/mtime and the archive's hash/mtime after validation must be unchanged.
- The source itself is never opened through SQLite by `archive`; the transfer `export` reads the source (or the archive) through a `mode=ro` URI only.

## 2. What is transferred — CandidateProfile only

Transferred: the singleton CandidateProfile's factual domain — `first_name`, `last_name`, `professional_title`, `location_city`, `location_country`, `professional_summary`, `career_goal`, `target_roles`, the exact `field_trust` map, and every `skills`/`experiences`/`education`/`certifications`/`projects`/`languages` item with all attributes and its `source`/`confidence`, plus `job_preferences` (or its absence).

**Deliberately NOT transferred** (remains only in the archived SQLite file): jobs and job reference tokens, company research, automation runs/schedules/mail progress, scheduler leases, Gmail threads/messages/claims/skips/analyses, response drafts/approvals/sends, follow-up proposals/approvals/sends, Gmail application drafts, Telegram digest/vacancy-review/Bewerbung preparation/approval state, review packages, CV/Bewerbung drafts, candidate-job matches, XING scan progress, processed-email markers, rate-limit state, any PENDING/SENT/FAILED/UNCERTAIN state, and the legacy `user_profiles` table (never a candidate source).

Rationale: the PostgreSQL pilot starts with a **clean operational state**. Historical approvals and send states refer to messages, vacancies and profile versions of the old environment; replaying them could re-trigger or mis-attribute outbound actions. Only the candidate's own facts are durable truth worth carrying forward.

This is **not** a general SQLite→PostgreSQL migration tool (no pgloader, no table mirroring), and it requires **no schema migration**.

## 3. Why GET → PATCH is not a safe transfer

`CandidateProfilePatchRequest` defaults a top-level field written without an explicit `field_trust` entry to `MANUAL_ENTRY/CONFIRMED`, and `FieldTrust`/the nested models default an absent `source`/`confidence` the same way. Replaying a profile through the API would silently **upgrade** a non-empty fact whose trust entry is absent (and any data whose provenance got lost) into a generation-usable fact. The transfer therefore:

- uses snapshot models with **no defaults** (every attribute, every `source`/`confidence` required; no type coercion; unknown keys rejected);
- reads `candidate_profiles.field_trust_json` **raw** and strictly (the repository's lenient loader would fill a missing `confidence` in as `CONFIRMED`) — an absent entry stays absent, a partial entry is refused;
- writes ORM rows directly in one transaction, never through PATCH and never through `get_or_create_candidate_profile`.

Never turn `INFERRED` into `MANUAL_ENTRY`, `UNCONFIRMED` into `CONFIRMED`, or create trust metadata the source did not have.

## 4. Snapshot format and sensitivity

`format = "jobtriage.candidate_profile_transfer"`, `format_version = 1`, plus `exported_at`, `source.dialect`, `source.alembic_revision`, `semantic_digest`, `profile`. No credentials, URLs, environment values or paths are ever included.

- **Semantic digest:** SHA-256 of canonical JSON (sorted keys, compact separators, UTF-8, NaN forbidden) of `{format, format_version, profile}`. `exported_at` and file paths are excluded, so exporting the same facts twice yields the same digest.
- **Not semantic** (excluded): child row ids, `created_at`/`updated_at`, `profile_version`. Nested collections are sorted by each item's canonical JSON (the ORM relationships define no order); lists inside an item and `target_roles` keep their stored order.
- Import recomputes the digest — and re-validates every item through the application's domain models, duplicate normalized skill/language/project names, and destination column lengths — **before any database write**.
- **A snapshot is the candidate's personal data.** `export` refuses any path inside the repository, never overwrites, and creates the file owner-only (0600) where the OS honours POSIX modes (on Windows keep it in a user-private directory). Delete snapshots once the cutover is verified; never commit, email or upload them. Logs and CLI output carry counts, digests, revisions and categories — never candidate values.

## 5. Destination policy

- PostgreSQL only, **fresh**: exactly at the application head, no candidate profile, no profile child rows, and every other application table empty. There is no overwrite, merge, upsert or force mode — a second import is refused.
- One atomic transaction: insert the complete graph, flush, re-read it from the database, recompute its semantic digest; commit only if it equals the snapshot's. Any error rolls back everything.
- `profile_version = 1` on the destination. The old version number is not preserved on purpose: no historical review/match/approval row that referenced it is migrated, so there is nothing for it to stay consistent with.

## 6. Procedure (rehearsal and real cutover use the same steps)

Database URLs are passed **only** through `TRANSFER_SOURCE_DATABASE_URL` / `TRANSFER_TARGET_DATABASE_URL` (never CLI arguments); every subcommand exits non-zero on any invariant violation.

```bash
# 0. stop web/scheduler/anything using the SQLite pilot
# 1. archive + read-only validation
python -m scripts.candidate_profile_transfer_stage13b archive \
    --source stage11_pilot.db --archive-dir "$BACKUP_DIR"
# 2. fresh PostgreSQL at head
docker compose up -d db
docker compose run --rm web alembic upgrade head
docker compose run --rm web alembic heads      # exactly one "(head)"
docker compose run --rm web alembic current    # the same revision "(head)"
# 3. export from the ARCHIVE copy (read-only), verify, import, compare
export TRANSFER_SOURCE_DATABASE_URL="sqlite:///$ARCHIVE_FILE"
export TRANSFER_TARGET_DATABASE_URL="postgresql+psycopg://..."   # from your secret store
python -m scripts.candidate_profile_transfer_stage13b export  --out "$PRIVATE_DIR/profile-snapshot.json"
python -m scripts.candidate_profile_transfer_stage13b verify  --snapshot "$PRIVATE_DIR/profile-snapshot.json"
python -m scripts.candidate_profile_transfer_stage13b import  --snapshot "$PRIVATE_DIR/profile-snapshot.json"
python -m scripts.candidate_profile_transfer_stage13b compare
# 4. application smoke with every external integration disabled
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:8000/api/v1/ready
curl -fsS -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/api/v1/candidate-profile >/dev/null
```

`compare` must report `status: PASS`: equal semantic digests, empty typed mismatch list, equal scoring-skill projection (`get_candidate_skills_for_scoring`), equal generation-trust decisions (`is_top_level_fact_usable_for_generation` for every `TOP_LEVEL_TRUST_FIELDS` field; `source`/`confidence`/`is_usable_for_generation` for every nested fact), equal revisions, destination `profile_version = 1`, and **zero rows in every non-CandidateProfile table** (derived from the ORM metadata, not a hand-written list).

## 7. PostgreSQL backup and restore

The exact commands (bash and PowerShell) live in **one** place, `docs/RUNBOOK.md` → *Backup* and
*Restore*, so the rehearsal and the operational procedure cannot drift apart. Their invariants:

- `pg_dump -F c` runs inside the `db` container via a single-quoted `sh -c`, so `POSTGRES_USER` /
  `POSTGRES_DB` are expanded by the **container** (Compose sets them there from `.env`; it does not
  export them into the host shell). No password is passed or printed.
- The dump is written to the container's `/tmp`, copied by `docker compose cp` **directly** into
  the private `BACKUP_DIR` outside the repository (the commands refuse a `BACKUP_DIR` that resolves
  inside it), then the container's `/tmp` copy is removed. Record size + SHA-256.
- Restore copies the dump from that same external `BACKUP_DIR` into the container and runs
  `createdb` + `pg_restore --no-owner --exit-on-error` there, into a brand-new, empty
  `restore_check` database — never over a live one.

Then verify `restore_check`: `alembic current` == `alembic heads`, `/ready`, and

```bash
TRANSFER_SOURCE_DATABASE_URL=<archive sqlite> TRANSFER_TARGET_DATABASE_URL=<restore_check> \
    python -m scripts.candidate_profile_transfer_stage13b compare
```

A restored database must reproduce the same revision, the same semantic digest, the same trust decisions, `/ready` = 200 and still-empty operational tables.

## 8. Forbidden during Stage 13B

Sending email, creating Gmail drafts, reading/writing real Gmail, running the XING or Bundesagentur collectors, running the Telegram bot against the real API, approving any response/follow-up, setting `OUTBOUND_SENDING_ENABLED=true` or `TELEGRAM_GMAIL_DRAFT_ENABLED=true`, migrating historical send/approval/UNCERTAIN state, modifying the SQLite pilot, using the persistent `ai_agent-db-1` container for rehearsals, `pg_restore --clean` over a live database, and any `DROP`/`TRUNCATE` against a database that is not a disposable rehearsal database.

## 9. Rollback

- Before PostgreSQL goes live: discard the PostgreSQL database and re-run the procedure; the SQLite pilot and its archive are untouched.
- After go-live: restore the latest `pg_dump` into a fresh database (section 7). Returning to SQLite means pointing `DATABASE_URL` back at a **copy** of the archive (never the archive itself) — any PostgreSQL-era state is then lost by construction, which is why the cutover happens before Stage 13C (real Gmail draft validation) or Stage 13D (controlled live pilot) produces any new operational state.

## 10. Rehearsal evidence (2026-10-13, local, real pilot)

No candidate values are recorded here — only counts, digests and statuses. All PostgreSQL work used brand-new disposable `postgres:16` containers (never `ai_agent-db-1`), destroyed afterwards together with their volumes; the snapshot and dump were deleted after verification.

| Step | Result |
|---|---|
| Pilot | `stage11_pilot.db` (the file `DATABASE_URL` pointed to), rollback-journal mode, no sidecars, no process using it |
| Archive | `stage11_pilot.stage13b-archive.20261013T164519Z.90149e5de3d1402e.sqlite` in the private backup folder, 1,826,816 bytes, read-only |
| Source / archive SHA-256 | `90149e5de3d1402e0d1c070b0a16e192ef6adc498fe7dc6eb8f78d3939ebbf07` (identical; source hash/size/mtime unchanged; archive hash unchanged after validation and after export) |
| `integrity_check` / revision / profiles | `ok` / `a7c3e9f1b2d4` (application head) / 1 |
| Archived but not transferred | e.g. 227 `jobs`, 176 `job_reference_tokens`, 1 legacy `user_profiles` row |
| Profile shape | 11 skills, job preferences present, 4 `field_trust` entries; the 4 top-level fields without a trust entry are empty in the source and stay entry-less on PostgreSQL |
| Fresh PostgreSQL A | `alembic upgrade head`; `alembic heads` = `alembic current` = `a7c3e9f1b2d4 (head)`; readiness true before import |
| Snapshot | semantic digest `b8875f0af54afbb496db16448a4c7b43bb4b58167ed968a06225f17d2ccc1a83`; `verify` PASS |
| Import | PASS, `profile_version = 1`; a second import was refused (`destination_not_empty`, exit 2) |
| `compare` (archive vs A) | PASS: digests equal, no typed mismatch, scoring projection equal (11 skills), generation trust equal, revisions equal, 31 operational tables checked — all empty |
| App smoke on A (two separate processes) | `/health` 200, `/ready` 200, authenticated profile GET 200 and typed-valid — both runs |
| `pg_dump -F c` of A | 167,993 bytes, SHA-256 `ed46f253b53efaf3e481d891cd9c64c6bc32730e5c80296a772153f2484ad3e2` |
| Restore into fresh PostgreSQL B | `pg_restore --no-owner --exit-on-error` into an empty database (0 → 40 tables); `alembic current` = head |
| `compare` (archive vs B) | PASS, digest `b8875f0a…1a83`, operational tables empty; app smoke on B: 200 / 200 / typed-valid |
