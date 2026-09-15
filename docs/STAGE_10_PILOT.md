# Stage 10 — Real-world Pilot / Shadow Mode

Branch: `feat/stage-10-shadow-mode`. Goal: run the system against real
external inputs (real job postings, real recruiter mailbox, real Gmail
inbox) with every outbound-transmission path disabled, and record what
breaks or misbehaves before any real send is ever considered.

## ABSOLUTE RULE

**No real email, Bewerbung, or recruiter-facing message may be sent during
this pilot.** `OUTBOUND_SENDING_ENABLED` stays `false` for the entire
pilot — this is enforced by the Stage 9 fail-closed kill switch at both the
route level (`app/api/routes.py`) and the provider level
(`app/providers/email/smtp.py::GmailSmtpProvider.send`), independently of
each other. Do not flip it to `true` at any point in Stage 10.

## Status: blocked on real credentials / real candidate data

This pilot cannot run against real external services without secrets and
personal data this repository does not contain and this agent must not
invent (per `CLAUDE.md`: "Never invent user skills, job history, or
application facts"). What's prepared so far is the scaffolding; running it
live requires the operator to supply the following, locally, never
committed:

| Needed | Where | Required for |
|---|---|---|
| `BUNDESAGENTUR_API_KEY` (+ search keywords/location) | `.env` | Real job ingestion via Bundesagentur |
| `XING_MAILBOX_USERNAME` / `XING_MAILBOX_APP_PASSWORD` | `.env` | Real job ingestion via XING digest emails |
| `GMAIL_USERNAME` / `GMAIL_APP_PASSWORD` | `.env` | Real inbox read/sync, response-draft/follow-up generation |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `.env` | Real notifications |
| Real candidate profile (`PATCH /api/v1/candidate-profile`) | DB, via API | Meaningful scoring/matching/CV drafts (defaults to a placeholder skill set otherwise — see `DEFAULT_PROFILE_SKILLS` in `app/db/repositories.py`) |

Copy `.env.stage10.example` to `.env` and fill in the blanks to proceed.
Any subset of the three collectors/read-paths (Bundesagentur, XING, Gmail)
can be exercised independently — leaving a credential blank makes the
corresponding endpoint fail closed (503) rather than partially run.

## What's enabled in this config

- Real collectors: Bundesagentur (API) and XING (email digest, read-only
  IMAP), whichever have credentials set.
- Scoring, skill extraction, candidate-job matching: run automatically as
  part of every collector run and `/candidate-job-matches` calls — no
  separate flag.
- Company Research: auto-triggered for APPLY-scored jobs, capped at 5/run.
- CV/Bewerbung draft generation: auto-triggered for the top shortlisted
  matches, capped at 5/run. Drafts only — no `Job.status` transition, no
  send, no approval.
- Gmail read/sync: read-only, only if `GMAIL_*` credentials are set. Never
  marks read/deletes/moves/sends.
- Automated Gmail response-draft cycle + follow-up proposal cycle: draft
  and proposal generation only, never a send/approval/status change.
- Scheduler: enabled, 30-minute cadence (`AUTOMATION_SCHEDULER_INTERVAL_SECONDS=1800`).
- Telegram: score-threshold notifications if a bot token/chat id is set;
  daily digest left off by default (short observation window doesn't need
  it — flip on for a multi-day pilot).

## What stays off

- `OUTBOUND_SENDING_ENABLED` — always `false`.
- Any actual `POST /response-drafts/{id}/send` or `POST
  /follow-ups/{id}/send` call — these require human approval *and* the
  kill switch; neither should be exercised during this pilot regardless of
  the kill switch's own state.
- `TELEGRAM_DAILY_DIGEST_ENABLED` — off by default (see above).

## How to run

```bash
cp .env.stage10.example .env
# fill in real credentials, per the table above
alembic upgrade head
uvicorn app.main:app --reload           # API + Telegram bot poller
python -m app.scheduler                 # separate process, if AUTOMATION_SCHEDULER_ENABLED=true
```

Manual triggers (useful for a single observed pass instead of waiting on
the scheduler):

```bash
curl -X POST http://localhost:8000/api/v1/collectors/bundesagentur/run -H "X-API-Key: $API_KEY"
curl -X POST http://localhost:8000/api/v1/collectors/xing/run -H "X-API-Key: $API_KEY"
curl -X POST http://localhost:8000/api/v1/gmail/sync -H "X-API-Key: $API_KEY"
```

## Observation checklist

Record findings here as the pilot runs (append dated entries, do not
overwrite prior ones):

- [ ] Duplicate jobs (same real posting persisted as two `JobRecord`s)
- [ ] Bad scoring (score/recommendation visibly wrong for a real posting)
- [ ] Wrong company matching (Company Research attached to the wrong
      company identity)
- [ ] Collector failures (auth, timeout, malformed response, rate limit)
- [ ] Draft quality problems (CV/Bewerbung draft factually wrong, or
      references skills/history not in the candidate profile)
- [ ] Gmail/thread matching issues (message matched to wrong job, or not
      matched to a job it should be)
- [ ] Scheduler/follow-up issues (lease/lock contention, missed cadence,
      follow-up proposed too early/late)
- [ ] Unexpected exceptions (anything not already a known/handled error
      path)
- [ ] Performance/retry problems (slow collector runs, excessive retries,
      timeouts)

## Findings log

_(empty — populate during the actual pilot run)_

## Fixes applied during this pilot

_(empty — only blockers found during the pilot itself; no new features)_
