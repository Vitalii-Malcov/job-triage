# Controlled Gmail response workflow

How an incoming Gmail message becomes a human-approved reply or follow-up,
and the invariants that keep that process safe. This is a map over existing
modules, not a new subsystem: the stages below were built incrementally
(Stages 7A–8D) and hardened as a whole in Stage 12.

## Lifecycle

| Conceptual state | Where it is recorded | Produced by |
|---|---|---|
| DISCOVERED | `gmail_messages` (identity: account, canonical mailbox, UIDVALIDITY, UID) | read-only IMAP sync — `app/services/gmail_sync.py` |
| CLASSIFIED / MATCHED | `gmail_message_analyses` (`match_type` = `APPLICATION` / `JOB_ONLY` / `AMBIGUOUS` / `UNMATCHED`, evidence, confidence) | `app/services/gmail_message_analysis.py` |
| DRAFT_READY | `response_drafts` (`PROPOSED`, or an explicit `NO_RESPONSE_RECOMMENDED`) | `app/services/response_draft.py` |
| WAITING_FOR_HUMAN | a `PROPOSED` draft with no `response_draft_approvals` row — listed by `GET /api/v1/response-drafts/pending` | — |
| APPROVED / REJECTED | `response_draft_approvals` (permanent, pins the exact subject/body) | `POST /api/v1/response-drafts/{id}/decision` |
| SENT / FAILED / UNCERTAIN | `response_draft_sends` | `POST /api/v1/response-drafts/{id}/send` (explicit, separate action) |
| FOLLOWUP_READY | `follow_up_proposals` → `follow_up_approvals` → `follow_up_sends` | `app/services/follow_up.py`, `app/services/follow_up_send.py` |

The scheduled automation cycle (`app/services/automation_gmail.py`) only
runs sync → analysis → draft. It never approves or sends, and it never drafts
a reply to the user's own Sent mail.

## Association (message → job/application)

Association is deterministic and evidence-ranked: an explicit reference in
the current message wins first, then thread history (always `LOW`
confidence), then composite evidence. Near-ties produce `AMBIGUOUS` with no
`matched_job_id`. Nothing is guessed, and no LLM is involved. Every message
that is associated with a tracked job is flagged for human review, because
sender identity is not authenticated. A draft for an `AMBIGUOUS` or
`UNMATCHED` message never carries job facts.

## Invariants

1. **No approval, no send.** Generating, listing and approving never transmit
   anything. Sending is a separate request that requires an `APPROVED`
   decision for that exact draft revision.
2. **Outbound sending is disabled by default.** `OUTBOUND_SENDING_ENABLED`
   defaults to `false`. While it is false, the send endpoints return 503
   before any send state is written, and the SMTP provider also refuses to
   transmit. Nothing ever sends Gmail mail automatically.
3. **Only the current revision can be approved** (Stage 12). A new analysis,
   a candidate-profile edit or a generator upgrade supersedes a draft
   revision (`is_current_revision` on `GET .../state` and on the pending
   list). Approving a superseded revision returns 409; rejecting it is
   always allowed. An approval recorded while the revision was current keeps
   authorizing exactly its pinned text.
4. **At most one live reply per inbound message** (Stage 12). Across all
   draft revisions of one message, at most one send can be `PENDING`, `SENT`
   or `UNCERTAIN`. This is enforced by the partial unique index
   `uq_response_draft_sends_live_per_message`, so it holds under
   concurrency. A second revision's approval or send returns 409. A `FAILED`
   attempt (a definite pre-transmission failure) never blocks another
   revision.
5. **At most one live follow-up per correspondence anchor** (Stage 12). The
   same rule applies to follow-up proposals that share an anchor message,
   via `uq_follow_up_sends_live_per_anchor`.
6. **Ambiguous outcomes are terminal.** An `UNCERTAIN` send is never retried
   automatically, and it blocks every other revision for that target.
7. **Idempotent retries.** Analyses, drafts, proposals and decisions are
   insert-only with unique identities. Repeating any step on unchanged
   inputs returns the existing row.

## Deploy note (Stage 12)

Migration `a7c3e9f1b2d4` adds the two partial unique indexes. Before
creating them, it checks for existing targets with more than one
`PENDING`/`SENT`/`UNCERTAIN` send row. If any exist, it refuses to run and
reports only counts. Such duplicates would mean two replies were already
sent, so resolve them manually and re-run `alembic upgrade head`. The
downgrade only drops the indexes.

## Tests

```bash
pytest -q tests/test_gmail_response_workflow.py \
          tests/test_gmail_response_workflow_endpoints.py \
          tests/test_gmail_response_workflow_e2e.py \
          tests/test_live_send_uniqueness_migration.py
# Real-PostgreSQL concurrency proof (needs a throwaway database at head):
TEST_POSTGRES_URL=postgresql+psycopg://... pytest -q tests/integration/test_live_send_uniqueness_postgres.py
```

On Windows, if the repository is on a slow or scanned drive, pass
`--basetemp` pointing to a fast local temp directory. Each test creates its
own SQLite file.
