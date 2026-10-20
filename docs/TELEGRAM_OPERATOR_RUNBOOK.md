# Telegram Operator Control — Runbook (Stage 16)

Design and guarantees: [STAGE_16_TELEGRAM_OPERATOR_CONTROL.md](STAGE_16_TELEGRAM_OPERATOR_CONTROL.md).

> **APPROVE in Telegram never sends an application.** It records the
> operator's decision and moves the application `PACKAGE_READY → APPROVED`
> locally. No e-mail, no portal, no employer contact.

`<db>` is the runtime database (never `D:\AI_agent\stage14b_canonical_v12.db`,
which the CLI refuses), already at the Alembic head.

## 1. Check configuration (never prints secrets)

```
python scripts/stage16_telegram_operator.py --database <db> config
```

Prints `configured` / `missing` for the bot token, chat id and optional
operator user id, plus the flag state. Values are never printed.

## 2. Dry run (default — nothing sent, nothing written)

```
python scripts/stage15_application_tracker.py --database <db> list --status PACKAGE_READY
python scripts/stage16_telegram_operator.py --database <db> preview <application_id>
python scripts/stage16_telegram_operator.py --database <db> send <application_id>
```

Both open the database read-only and show: masked destination chat, company,
role, the exact message text, every button with its callback data and size,
`APPROVE OFFERED`, `PACKAGE_NOT_READY` reasons, and `DB WILL CHANGE: no`.

## 3. Live pilot (one card) — requires explicit operator authorization

Before the first real message, show the operator the dry-run output (masked
chat, company, role, text, buttons, "DB will change: card delivery row only",
"No employer-facing action occurs") and obtain explicit approval for ONE test
message. Prefer a scratch/prepared target, not a second real application.

```
set TELEGRAM_OPERATOR_CONTROL_ENABLED=true
python scripts/stage16_telegram_operator.py --database <db> send <application_id> --live
```

Result codes: `SENT`, `FAILED` (provably not delivered; rerun retries the same
card), `UNCERTAIN` (never retried automatically — check the chat),
`ALREADY_SENT`, `RETRIES_EXHAUSTED`, `DISABLED`, `NOT_CONFIGURED`,
`NOT_DECIDABLE`.

First live callback proof: press **DETAILS** or **LATER**. Use APPROVE on a
real vacancy only if the operator explicitly chooses it. The bot must be
running (the API process starts the polling bot when a token is configured)
with `TELEGRAM_OPERATOR_CONTROL_ENABLED=true` and the same database.

## 4. Inspect decisions

```
python scripts/stage16_telegram_operator.py --database <db> decisions <application_id>
python scripts/stage15_application_tracker.py --database <db> history <application_id>
```

An APPROVE appears in both: the decision row and the Stage 15 event
`PACKAGE_READY → APPROVED` (origin `OPERATOR`, actor `TELEGRAM_OPERATOR`).

## 5. Change a final decision (explicit override)

```
python scripts/stage16_telegram_operator.py --database <db> override <id> APPROVE --reason "..."
python scripts/stage16_telegram_operator.py --database <db> override <id> APPROVE --reason "..." --apply
```

Without `--apply` it only validates. SKIP → APPROVE needs a ready package;
APPROVE → SKIP is possible only while the application is `APPROVED` (it
returns to `PACKAGE_READY` via a Stage 15 operator override). Submitted
applications are never changed.

## 6. Lifecycle notifications

```
python scripts/stage16_telegram_operator.py --database <db> notifications [--since YYYY-MM-DD]
python scripts/stage16_telegram_operator.py --database <db> notifications --live
```

The dry run lists every pending notification with its exact text. `--live`
sends each at most once (ledger). Default window: the last 7 days. A
"possible rejection/offer" notification changes nothing — confirm a genuine
one with:

```
python scripts/stage15_application_tracker.py --database <db> transition <id> REJECTED \
    --actor operator --reason "rejection mail confirmed" --apply
```

## 7. Troubleshooting

| Symptom | Cause / action |
|---|---|
| Buttons do nothing, no answer | the press came from a chat other than `TELEGRAM_CHAT_ID`, or from a group without `TELEGRAM_OPERATOR_USER_ID` (refused silently by design); check `telegram_callback_refused reason=unauthorized` |
| "Operator control is disabled" | `TELEGRAM_OPERATOR_CONTROL_ENABLED` is false in the bot process |
| "Unknown or expired card" | token not found, or the card was never delivered (FAILED) |
| "This card is outdated" | the application now names a different review package — send a new card |
| `PACKAGE_NOT_READY: …` | see the reasons (`JOB_CHANGED`, `PROFILE_CHANGED`, `MANUAL_EDITS_UNACKNOWLEDGED`, `STATUS_…`, …); fix the package, then press again |
| "A final decision (…) is already recorded" | use the CLI override (section 5) |
| "operator decisions apply only before submission" | the application is already submitted or later |
| `UNCERTAIN` card or notification | never auto-retried; check the chat — a duplicate is worse than a manual resend |
| card shows old status after a press | the in-place edit failed (logged `telegram_operator_card_edit_failed`); the decision itself is committed — check `decisions` |
