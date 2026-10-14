# Stage 13C — Real Gmail draft validation

## Purpose

Prove that JobTriage can put **one** Gmail draft of an approved Bewerbung into a **dedicated
non-production Gmail mailbox**, safely and exactly once, before the draft feature is ever enabled
against a real mailbox (Stage 13D).

**Stage 13C does NOT send email.** A Gmail draft is not an application: no recipient, no
attachment, no SMTP, no Gmail API, no OAuth, no `JobRecord.status` write. The draft path's only
mailbox mutation is one IMAP `APPEND` into the verified Drafts mailbox.

This document covers the implementation slice (merged code, tests, configuration) **and** the
operator checklist for the later live test. The live test is a separate step that needs
**explicit operator confirmation**. Nothing in the implementation step logs in to Gmail.

## What already existed (Stage 9D) and what Stage 13C adds

Stage 9D (`app/services/gmail_application_draft.py`, `app/providers/email/imap_draft.py`,
`app/providers/email/draft_base.py`, ledger table `gmail_application_drafts`) already provides
nearly every invariant Stage 13C needs:

| Invariant | Mechanism (existing) |
|---|---|
| Draft only, never send | APPEND-only `DraftProvider` Protocol (`create_draft`, `find_by_message_id`); no SMTP/HTTP/OAuth import; header allowlist |
| Human approval | Only on an explicit Telegram press for an exact Stage 9C approval capability; fresh full revalidation (profile → job → preparation → review → exact approved revision) at claim **and** at arm |
| Content integrity | Deterministic renderer `9d-v1` of the approved revision only; `content_sha256` frozen per attempt; frozen handoff ids (`review_id`, `approved_revision_id`, `package_token`, `input_identity`, …) — any change → `PACKAGE_REPLACED`/`PACKAGE_STALE`, no draft |
| Recipient safety | Structural: `DraftMessage` has **no recipient field**; MIME builder asserts no `To/Cc/Bcc/Reply-To/In-Reply-To/References` |
| Attachments | None, by design (single `text/plain` part). Excluded from the Stage 13C slice |
| Idempotency | One ledger row per approval link (`uq_gmail_application_drafts_link_id`); claim → acknowledged arm commit → exactly one APPEND; a duplicate press reports `IN_PROGRESS`/`ALREADY_CREATED` |
| Atomic state | `CREATED` only after the provider's tagged OK **and** the ledger CAS; provider definite failure → `FAILED` (retry allowed); a lost commit acknowledgment is recovered **DB-only**, never by a second APPEND |
| Uncertain outcome | Timeout/abort/parse error after APPEND was invoked → **sticky `UNCERTAIN`**. Never converted to `FAILED` or retryable by time or zero matches; only exactly one read-only Message-ID match (or this operation's retained tagged OK) turns it `CREATED` |
| Provider metadata | Ledger stores `account_key`, frozen mailbox + wire form, marker Message-ID, `uid_validity`, `draft_uid`, timestamps, `last_error` code. No password, no token, no content |
| Logging | Ids, attempt counts, states, fixed codes, exception types, durations only |
| Feature flag | `TELEGRAM_GMAIL_DRAFT_ENABLED` (default `false`, also pinned `false` in `compose.yaml`), plus both Stage 9B/9C flags; never consults `OUTBOUND_SENDING_ENABLED` |

**Gap narrowed by Stage 13C — an explicit mailbox pin.** Before Stage 13C the draft path wrote to
whatever `GMAIL_USERNAME` was configured — the same account the inbox sync uses, i.e. typically the
operator's personal mailbox. Stage 13C adds an explicit, fail-closed, **operator-configured** pin:

- `GMAIL_DRAFT_ALLOWED_ACCOUNT` (default empty). Draft create **and** reconcile lookup are refused
  with `ACCOUNT_NOT_ALLOWED` unless it is a single bare address equal to `GMAIL_USERNAME` under the
  project's account normalization (`strip().casefold()`).
- Checked in the service **before any ledger claim** (`_static_config`, `handle_reconcile`) and
  again in `GmailImapDraftProvider` **before any IMAP connection** (`_check_account`; the provider
  default `allowed_account=""` refuses). A refusal never changes an existing ledger row
  (an `UNCERTAIN` attempt stays `UNCERTAIN`).
- The provider logs in as exactly that username. A successful LOGIN only means the App Password
  authenticated to the configured account; it says nothing about what kind of account it is. The
  ledger's frozen `account_key` additionally blocks reconciling/re-creating an attempt under a
  different account (`ACCOUNT_CHANGED`).

### What the pin guarantees — and what it does not

**TECHNICAL GUARANTEE (enforced by code and tests):** no draft is created and no reconcile lookup
connects unless the configured Gmail login (`GMAIL_USERNAME`) equals the operator-pinned account
(`GMAIL_DRAFT_ALLOWED_ACCOUNT`), and an empty pin refuses everything. That is all the pin proves.

The application **cannot determine** whether the pinned mailbox is personal, production,
non-production, dedicated or isolated. If an operator sets both values to the same personal Gmail
account, the pin passes and a draft would land in that personal mailbox. The pin does **not**
prevent that; it only prevents the draft path from writing to an account nobody pinned.

**OPERATIONAL GUARANTEE (human, not code):** before any live Stage 13C validation, the operator
independently verifies that the pinned account is the intended **dedicated non-production
validation mailbox** (see the operator preflight below). If the operator cannot verify that:
**STOP — do not enable Gmail draft validation and do not perform any live write.**

No new feature flag and **no schema change / migration**: the ledger already records everything
Stage 13C needs. The pin is configuration, not a flag; it does not enable anything by itself.

## Threat / safety model

| Threat | Control |
|---|---|
| Draft lands in an account nobody pinned | `GMAIL_DRAFT_ALLOWED_ACCOUNT` pin (empty = refuse), double-checked in service and provider — technical |
| Draft lands in the personal/production mailbox | **Operator verification only** (preflight items 2–3): the pin cannot tell a personal account from a validation account — operational |
| Something gets sent | No send capability in the draft modules (AST-tested), no recipient field, flags independent of `OUTBOUND_SENDING_ENABLED` (which stays `false`) |
| Draft created without approval / automatically | Only `handle_create` creates; only the Telegram callback (authorized operator, private chat, all three flags) calls it; the scheduler never does |
| Content changed after approval | Full revalidation at claim and arm; frozen handoff + content hash; renderer reads only the approved revision |
| Duplicate drafts | Unique ledger row per approval; at most one APPEND per acknowledged arm; never a network retry |
| Network timeout after APPEND | Sticky `UNCERTAIN`; read-only reconcile only; no automatic re-create |
| Credential leakage | App Password only from env (`repr=False`); fixed-string errors; logs carry no address, subject, body, marker, mailbox or UID |

Accepted risk (unchanged, see `CLAUDE.md`): the Gmail App Password grants full mailbox access; the
draft path uses one write type (APPEND to the verified `\Drafts` mailbox). A dedicated
non-production mailbox — verified as such by the operator, not by the application — limits the
blast radius of the live test.

## Configuration

| Setting | Default | Stage 13C live test value |
|---|---|---|
| `OUTBOUND_SENDING_ENABLED` | `false` | **`false`** (unchanged) |
| `ALEMBIC_AUTO_UPGRADE` | `false` | **`false`** (unchanged) |
| `TELEGRAM_GMAIL_DRAFT_ENABLED` | `false` | `true` **only** in the isolated validation environment, only after confirmation |
| `TELEGRAM_BEWERBUNG_DRAFT_ENABLED`, `TELEGRAM_BEWERBUNG_APPROVAL_ENABLED` | `false` | `true` in the validation environment (required to reach an approval) |
| `GMAIL_USERNAME` | empty | the operator-verified dedicated non-production Gmail address |
| `GMAIL_APP_PASSWORD` | empty | that account's App Password (env only, never committed) |
| `GMAIL_DRAFT_ALLOWED_ACCOUNT` | empty | the **same** operator-verified address |
| `GMAIL_DRAFTS_MAILBOX` | `[Gmail]/Drafts` | the account's decoded Drafts name (e.g. `[Gmail]/Entwürfe` for a German UI) |

Tracked configuration (`compose.yaml`, `.env.example`, `.env.stage10.example`) keeps every flag
`false` and the pin empty. The live test never edits tracked files.

## Test matrix (implementation step)

| Case | Covered by |
|---|---|
| A happy path, metadata recorded | `tests/test_gmail_application_draft.py::TestCreate::test_first_create_is_one_exact_append_and_nothing_else`, `tests/test_stage13c_gmail_draft_validation.py::test_pin_is_an_identity_comparison` |
| B no approval | `TestExactBinding::test_pending_review_is_not_approved`, `test_rejected_review`, `test_unknown_capability` |
| C stale approval / artifact changed | `TestExactBinding::test_profile_change_is_stale`, `test_job_change_is_stale`, `test_replaced_generation`, `test_api_approval_of_another_revision_is_refused`, `test_package_replaced_between_claim_and_arm_releases_the_claim` |
| D/E/F recipient changed / missing / malformed | Structural — no recipient exists to change: `test_draft_message_has_no_recipient_field`, `test_entry_points_take_no_recipient_or_content_argument`, `test_created_mime_has_no_recipient_and_only_allowed_headers`, `TestCreate::test_mime_has_no_recipient_attachment_or_html` |
| G feature disabled | `tests/test_telegram_gmail_draft_bot.py` (flag matrix), `test_draft_flag_never_consults_outbound_sending` |
| H login account ≠ pinned account (technical guarantee only) | `tests/test_stage13c_gmail_draft_validation.py` (`test_unpinned_or_mismatched_account_refuses_before_any_claim`, `test_username_not_matching_the_pin_refuses`, `test_reconcile_lookup_requires_the_pin`, `test_provider_refuses_*_before_connecting`), `TestStickyUncertain::test_account_change_blocks_reconciliation_and_creation` |
| I duplicate invocation | `TestCreate::test_sequential_duplicate_is_one_append_with_historical_wording`, `test_in_flight_duplicate_press_reports_progress_without_a_second_append` |
| J concurrent duplicate | `tests/integration/test_gmail_application_draft_postgres.py::test_1_*`, `test_2_begin_append_race_grants_exactly_one_permit` (real PostgreSQL) |
| K provider failure before creation | `TestProviderOutcomes::test_definite_failures_are_failed_with_retry_button` |
| L timeout / uncertain | `TestProviderOutcomes::test_ambiguous_failures_are_uncertain_with_only_a_recheck_button`, `test_cancellation_leaves_the_attempt_uncertain`, `TestStickyUncertain::*` |
| M persistence failure after provider success | `TestCommitAcknowledgment::test_created_finalize_ack_lost_is_recovered_db_only`, `test_db_unavailable_during_recovery_has_no_network_authority` |
| N send path | `test_stage13c_modules_import_no_send_capability`, `test_draft_provider_surface_is_create_and_lookup_only`, `test_stage_9d_modules_import_no_send_status_or_generation_code` |
| O secrets in logs | `TestIsolationAndPrivacy::test_logs_never_contain_sensitive_values`, `test_pin_refusal_logs_no_identity_or_secret`, `test_provider_repr_hides_account_and_password` |
| P outbound flags | `test_safety_defaults_stay_closed`, `test_env_example_ships_the_pin_empty_and_drafts_disabled`, `tests/test_deployment_config.py::test_safety_flags_are_pinned_false_in_every_app_service` |

## Operator preflight (before the live test)

All of these must hold; any "no" is a STOP.

1. Codex has passed the Stage 13C implementation review, and the branch is merged with green CI.
2. A **dedicated non-production Gmail account** exists, with 2-step verification and its own App
   Password. The application cannot check any of the following; the operator verifies each one
   independently and confirms it explicitly (in writing, in the live-test confirmation):
   - this account was created or selected **specifically for Stage 13C validation**;
   - it is **not** the user's personal mailbox and holds no personal mail;
   - it is **not** a production or recruiter-facing mailbox (not the XING-alert mailbox, not the
     inbox-sync account of any production environment, never used to contact employers);
   - the operator has **visually/administratively verified the account identity** (signed in to
     that account in a browser and checked the address shown by Google);
   - `GMAIL_USERNAME` **and** `GMAIL_DRAFT_ALLOWED_ACCOUNT` both refer to exactly that verified
     dedicated validation mailbox.

   If any of these cannot be verified independently: **STOP. Do not enable Gmail draft validation.
   Do not perform a live write.** A passing pin is not a substitute for this check.
3. The validation runs in an **isolated environment**: a separate checkout/venv and a separate,
   disposable database (never the production PostgreSQL, never `ai_agent-db-1`, never the SQLite
   pilot). The production compose stack is not reconfigured; its pinned `false` flags stay.
4. The validation database holds only **synthetic** candidate/job data (no real CV content is
   placed into any mailbox in Stage 13C).
5. The environment file for the validation sets exactly the values in the configuration table,
   with `OUTBOUND_SENDING_ENABLED=false`, and `GMAIL_DRAFT_ALLOWED_ACCOUNT` = `GMAIL_USERNAME` =
   the non-production address. The file lives outside the repository.
6. The `startup_safety_state` log line shows `outbound_sending_enabled=false` and
   `alembic_auto_upgrade=false`.
7. The Drafts mailbox name matches the account's UI language (`GMAIL_DRAFTS_MAILBOX`).
8. The operator has the Telegram private chat with the (validation) bot open and is the only
   authorized user.

## Live validation checklist (later, separately approved step)

**No step below may run without explicit operator confirmation given for this live test.** The
confirmation names the non-production address, states that the operator has independently verified
it per preflight item 2, and names the single approval capability to be used.

1. Record: branch/main SHA, Alembic head, non-production address, timestamp.
2. Confirm in Gmail (web UI) that the non-production Drafts folder is empty or note its count.
3. Prepare one synthetic Bewerbung through the normal Stage 9B/9C flow and approve exactly one
   revision in Telegram.
4. Press **"📧 Gmail-Entwurf erstellen" once**. Expected notice: draft created, no recipient, no
   CV, NOT SENT.
5. In Gmail (web UI): exactly **one** new draft; subject and body equal the approved revision;
   **To/Cc/Bcc empty**; no attachment; it is in Drafts, not Sent.
6. In the validation DB: exactly one `gmail_application_drafts` row for the link, state
   `CREATED`, `attempt_count=1`, `account_key` = non-production address, `draft_uid` set.
7. Press the create button again: expected `ALREADY_CREATED`; Gmail still shows exactly one
   draft.
8. Confirm in Gmail **Sent** that nothing was sent.
9. Optionally, in Gmail, delete the test draft by hand (the application never deletes).
10. Set `TELEGRAM_GMAIL_DRAFT_ENABLED=false` again in the validation environment, revoke the
    non-production App Password if it will not be reused, and record the results.

A refusal (`ACCOUNT_NOT_ALLOWED`, `GMAIL_DRAFT_DISABLED`, `PACKAGE_STALE`, …) creates nothing; fix
the precondition and repeat from the relevant step.

## Rollback / reconciliation

- **Disable instantly:** `TELEGRAM_GMAIL_DRAFT_ENABLED=false` (or clear
  `GMAIL_DRAFT_ALLOWED_ACCOUNT`) and restart. Existing ledger rows are kept.
- **`UNCERTAIN`** (timeout after APPEND): do **not** press create again — the bot only offers
  "🔄 Gmail-Status prüfen". Wait for the frozen deadline/min age, then press it: exactly one
  Message-ID match → `CREATED (reconciled)`; anything else stays `UNCERTAIN`. If it stays
  uncertain, check the Drafts folder in Gmail by hand. Never edit the ledger to `FAILED` to get a
  retry button; UNCERTAIN is sticky by design.
- **`PERSISTENCE_UNCERTAIN` / `FINALIZE_UNSAVED`** (DB unavailable after the provider answered):
  restore DB availability, then use "🔄 Gmail-Status prüfen"; no network retry happens
  automatically.
- **Unexpected draft content:** delete the draft by hand in Gmail; keep the ledger row (historical
  evidence); file the finding before any further test.
- **Anything was sent** (must be impossible): STOP all work, keep logs and the DB, report.
