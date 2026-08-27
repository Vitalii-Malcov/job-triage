"""Telegram control center: interactive bot commands over the job database.

This is the two-way counterpart to app/services/telegram.py's one-way
best-effort notifications. It reuses the same TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID settings — Telegram doesn't distinguish "sending" and
"receiving" bots at the API level, so one BotFather bot does both jobs.

Architecture:
- Polling (getUpdates), not a webhook: the project has no public HTTPS
  endpoint, and standing one up (domain, reverse proxy, TLS) is separate
  infrastructure work out of scope for a personal project. Polling needs
  nothing beyond outbound internet access.
- The bot runs embedded in the same FastAPI process as an asyncio task
  wired into app/main.py's lifespan, not a separate process/script. It uses
  python-telegram-bot's low-level Application lifecycle
  (initialize/start/updater.start_polling, mirrored by stop/shutdown on
  shutdown) rather than Application.run_polling(), which installs its own
  signal handlers and owns the event loop — both would conflict with
  uvicorn managing the same process.
- If TELEGRAM_BOT_TOKEN is unset, start_bot() logs and returns None; the
  rest of the API keeps working normally.

Security: every command handler is wrapped with `@require_authorized`
(applied at the function definition, not at registration — see that
decorator's docstring for why). A message from any chat_id other than
settings.telegram_chat_id gets no reply at all — not an error, not any
acknowledgement that a bot is listening — only a server-side log entry
(logged once, inside `_is_authorized` itself). This is deliberate: real
silence hides the difference between "no bot exists here" and "rejected"
from a stranger probing the bot.
"""

import functools
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from app.api.routes import _run_bundesagentur, _run_xing
from app.collectors.base import CollectorError, CollectorNotConfiguredError
from app.core.config import Settings, get_settings
from app.db.repositories import get_job_by_id, list_jobs, update_job_status
from app.db.session import SessionLocal
from app.domain.status_transitions import InvalidStatusTransitionError
from app.models.application_status import ApplicationStatus

logger = logging.getLogger(__name__)

# Telegram messages are capped at 4096 characters. JOBS_LIST_LIMIT and the
# per-field truncation in _format_job_line keep a *typical* /jobs reply well
# under that, but neither is an airtight guarantee against arbitrarily long
# upstream titles/company names (Job.title/.company have no max_length —
# see app/models/job.py) — JOBS_REPLY_SOFT_LIMIT below is the actual
# enforcement, applied in _build_jobs_reply.
JOBS_LIST_LIMIT = 20
JOBS_TITLE_MAX_LEN = 120
JOBS_COMPANY_MAX_LEN = 80
JOBS_REPLY_SOFT_LIMIT = 4000

HELP_TEXT = (
    "Available commands:\n"
    "/jobs [status] - list jobs, optionally filtered by status\n"
    "/job <id> - show job details\n"
    "/status <id> <new_status> - update a job's status\n"
    "/run bundesagentur - run the Bundesagentur collector\n"
    "/run xing - run the XING mailbox collector\n"
)

_VALID_STATUSES = ", ".join(status.value for status in ApplicationStatus)


def _is_authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    chat_id = str(chat.id) if chat is not None else ""
    if settings.telegram_chat_id and chat_id == settings.telegram_chat_id:
        return True

    text = update.message.text if update.message is not None else ""
    logger.warning("telegram_bot_unauthorized_message chat_id=%s text=%s", chat_id, text)
    return False


def require_authorized(handler):
    """Gate a command handler behind `_is_authorized`, structurally.

    Applied as `@require_authorized` directly above every `cmd_*` definition
    below rather than at CommandHandler registration in build_application().
    Decorating the function itself means the name importers actually get
    (including tests, which call `bot.cmd_start` etc. directly) is always
    the guarded version — there is no separate "raw" handler object anyone
    could accidentally wire up or call unguarded. It also means a future
    command only needs to remember one line (this decorator) instead of
    reproducing the `settings = get_settings(); if not _is_authorized(...):
    return` snippet inline, which was previously copy-pasted into all 6
    handlers with no structural guarantee a 7th wouldn't omit it.

    Does not re-log on the unauthorized path — `_is_authorized` already
    does that once; logging here too would just duplicate the line.
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, get_settings()):
            return
        await handler(update, context)

    return wrapper


@require_authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


@require_authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


def _format_job_line(record) -> str:
    title = record.title[:JOBS_TITLE_MAX_LEN]
    company = record.company[:JOBS_COMPANY_MAX_LEN]
    return f"#{record.id} [{record.status}] {title} @ {company} (score {record.score})"


def _build_jobs_reply(records) -> str:
    """Render /jobs records into one message, enforcing Telegram's 4096-char cap.

    Per-field truncation in _format_job_line handles the common case, but
    isn't a guarantee on its own — JOBS_LIST_LIMIT (20) records at
    JOBS_TITLE_MAX_LEN/JOBS_COMPANY_MAX_LEN each can still add up past the
    soft limit. This is the actual enforcement: shrink the list itself
    (with a visible "...and N more" marker) rather than risk reply_text()
    raising on an oversized message and the user getting silence.
    """
    lines = [_format_job_line(record) for record in records]
    text = "\n".join(lines)
    if len(text) <= JOBS_REPLY_SOFT_LIMIT:
        return text

    while lines:
        lines.pop()
        remaining = len(records) - len(lines)
        suffix = f"\n...and {remaining} more (use /jobs <status> to narrow down)"
        text = "\n".join(lines) + suffix
        if len(text) <= JOBS_REPLY_SOFT_LIMIT:
            return text

    return f"...{len(records)} jobs found (use /jobs <status> to narrow down)"


@require_authorized
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status: ApplicationStatus | None = None
    if context.args:
        try:
            status = ApplicationStatus(context.args[0].upper())
        except ValueError:
            await update.message.reply_text(
                f"Unknown status '{context.args[0]}'. Valid: {_VALID_STATUSES}."
            )
            return

    db = SessionLocal()
    try:
        records = list_jobs(db, status=status, limit=JOBS_LIST_LIMIT)
    finally:
        db.close()

    if not records:
        await update.message.reply_text("No jobs found.")
        return

    await update.message.reply_text(_build_jobs_reply(records))


@require_authorized
async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /job <id>")
        return
    try:
        job_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /job <id> (id must be a number)")
        return

    db = SessionLocal()
    try:
        record = get_job_by_id(db, job_id)
    finally:
        db.close()

    if record is None:
        await update.message.reply_text(f"Job #{job_id} not found.")
        return

    text = (
        f"#{record.id} {record.title}\n"
        f"Company: {record.company}\n"
        f"Location: {record.location or 'n/a'}\n"
        f"Status: {record.status}\n"
        f"Score: {record.score} ({record.recommendation})\n"
        f"URL: {record.url}"
    )
    await update.message.reply_text(text)


@require_authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /status <id> <new_status>")
        return
    try:
        job_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /status <id> <new_status> (id must be a number)")
        return
    try:
        new_status = ApplicationStatus(context.args[1].upper())
    except ValueError:
        await update.message.reply_text(
            f"Unknown status '{context.args[1]}'. Valid: {_VALID_STATUSES}."
        )
        return

    db = SessionLocal()
    try:
        try:
            record = update_job_status(db, job_id, new_status)
        except InvalidStatusTransitionError as exc:
            await update.message.reply_text(str(exc))
            return
    finally:
        db.close()

    if record is None:
        await update.message.reply_text(f"Job #{job_id} not found.")
        return

    await update.message.reply_text(f"Job #{record.id} status updated to {record.status}.")


@require_authorized
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    if not context.args or context.args[0] not in ("bundesagentur", "xing"):
        await update.message.reply_text("Usage: /run bundesagentur|xing")
        return

    collector_name = context.args[0]
    db = SessionLocal()
    try:
        try:
            if collector_name == "bundesagentur":
                stats = await _run_bundesagentur(db, settings)
            else:
                stats = await _run_xing(db, settings)
        except CollectorNotConfiguredError as exc:
            await update.message.reply_text(str(exc))
            return
        except CollectorError as exc:
            await update.message.reply_text(f"{collector_name} collector run failed: {exc}")
            return
    finally:
        db.close()

    await update.message.reply_text(
        f"{collector_name} collector run complete: "
        f"fetched={stats['fetched']} created={stats['created']} "
        f"updated={stats['updated']} skipped_invalid={stats['skipped_invalid']} "
        f"failed={stats['failed']}"
    )


async def _handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for exceptions raised by any command handler.

    Without this registered, PTB's own default behavior is to log via its
    own logger ("No error handlers are registered, logging exception.") and
    otherwise swallow the exception — the user gets silence, and the log
    doesn't go through this project's logger. This surfaces it both ways:
    our own structured log, plus a best-effort chat notification so the
    human operator doesn't have to be tailing server logs to notice a
    command silently failed.
    """
    logger.error("telegram_bot_unhandled_error", exc_info=context.error)

    settings = get_settings()
    if not settings.telegram_chat_id:
        return
    try:
        await context.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text="Internal error — check server logs.",
        )
    except Exception:
        # Best-effort only: if even the failure notification fails (e.g.
        # Telegram itself is unreachable), log and stop — must not raise
        # from inside an error handler and risk masking the original error
        # or looping.
        logger.warning("telegram_bot_error_notification_failed", exc_info=True)


def build_application(settings: Settings) -> Application:
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("jobs", cmd_jobs))
    application.add_handler(CommandHandler("job", cmd_job))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_error_handler(_handle_error)
    return application


async def start_bot(settings: Settings) -> Application | None:
    """Start the Telegram control center bot, or do nothing if unconfigured
    or unable to start.

    Uses the low-level embedding pattern (initialize/start/updater.start_polling)
    instead of Application.run_polling() — see this module's docstring for why.

    Failures here (bad/revoked token, no network yet at container boot, etc.)
    are caught and logged rather than left to propagate into app/main.py's
    lifespan: a broken Telegram integration must not take down the whole
    API's startup, the same best-effort principle
    app/services/telegram.py's TelegramNotifier already applies to outbound
    notifications. Caught here (not in main.py) so start_bot's contract is
    self-contained: it always either returns a running Application or None,
    never raises for a runtime start failure — callers don't need to know
    how it can fail.
    """
    if not settings.telegram_bot_token:
        logger.info("telegram_bot_disabled reason=no_bot_token_configured")
        return None

    application = build_application(settings)
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
    except Exception:
        logger.exception("telegram_bot_start_failed")
        try:
            # Best-effort cleanup of whatever partially started (e.g. the
            # Bot's HTTP client) so a failed start doesn't leak resources
            # nobody will ever call stop_bot() on. Safe even if initialize()
            # itself never completed — Application.shutdown() no-ops when
            # not yet initialized.
            await application.shutdown()
        except Exception:
            logger.warning("telegram_bot_cleanup_after_failed_start_failed", exc_info=True)
        return None

    logger.info("telegram_bot_started")
    return application


async def stop_bot(application: Application | None) -> None:
    if application is None:
        return
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    logger.info("telegram_bot_stopped")
