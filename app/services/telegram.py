import asyncio
import logging
from enum import StrEnum

import httpx

from app.models.job import Job, JobScore

logger = logging.getLogger(__name__)

# Telegram's own hard cap on sendMessage's `text` field. Callers building
# longer content (e.g. app.services.telegram_digest) must truncate BEFORE
# calling send_telegram_text -- this module never truncates on a caller's
# behalf, since what's safe to drop is content-dependent.
TELEGRAM_MESSAGE_HARD_LIMIT = 4096


class TelegramSendOutcome(StrEnum):
    """The three outcomes a single `send_telegram_text` attempt can
    resolve to -- mirrors this project's existing outbound-email
    send-outcome vocabulary (app.providers.email.outbound_base's
    SENT/FAILED + EmailSendOutcomeUnknownError for "uncertain"), applied
    to the Telegram Bot API's `sendMessage` instead of SMTP.
    """

    SENT = "SENT"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


async def send_telegram_text(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    timeout_seconds: float = 5.0,
) -> TelegramSendOutcome:
    """One Telegram `sendMessage` attempt -- no internal retry loop (the
    caller decides whether/when to retry; see
    app.db.telegram_digest_repository for the daily digest's own
    once-per-date retry policy, and `TelegramNotifier.send_job` below
    for the existing best-effort job-alert retry loop).

    **Classification rule (Codex Stage 8E BLOCKER fix): if we cannot
    PROVE Telegram did NOT receive/process the request, the outcome is
    UNCERTAIN, never FAILED.** Only two situations are provably
    "never reached Telegram, safe to retry":

    - `httpx.HTTPStatusError` -- a response WAS received (Telegram
      itself rejected/errored the request, e.g. bad token or chat not
      found). The HTTP transaction completed; we know the outcome.
    - `httpx.ConnectError` / `httpx.ConnectTimeout` / `httpx.PoolTimeout`
      -- the connection itself could never be established (DNS failure,
      refused connection, or a timeout while still connecting/queued
      for a pool connection) -- no request bytes were ever transmitted.

    Every OTHER `httpx.HTTPError` -- `ReadTimeout`, `WriteTimeout`,
    `ReadError`, `WriteError`, `RemoteProtocolError`, or any other
    `RequestError`/`TransportError` -- happens AFTER a connection was
    already established, meaning the request may have been partially
    or fully transmitted (and possibly already processed by Telegram)
    before the failure. These are classified UNCERTAIN, exactly like a
    response timeout: delivery cannot be disproven, so it must not be
    treated as safe to retry.

    Callers that must not risk a duplicate message (the daily digest)
    treat UNCERTAIN as terminal and never automatically retry it;
    callers for whom an occasional duplicate is an acceptable
    trade-off against a missed alert (`TelegramNotifier.send_job`,
    unchanged from before Stage 8E) may still retry either outcome.

    **Hardening (Stage 8E):** never logs the bot token, the request URL
    (which embeds the token), the message text, or a raw exception
    message/traceback -- only the outcome and, on failure,
    `type(exc).__name__`.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": text})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # A response WAS received -- Telegram itself rejected the
        # request (e.g. bad token, chat not found). Provably not
        # delivered.
        logger.warning("telegram_send_failed error_type=%s", type(exc).__name__)
        return TelegramSendOutcome.FAILED
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        # The connection was never established (or timed out before it
        # was) -- no request bytes were ever transmitted. Provably not
        # delivered, safe to retry.
        logger.warning("telegram_send_failed error_type=%s", type(exc).__name__)
        return TelegramSendOutcome.FAILED
    except httpx.HTTPError as exc:
        # Everything else (ReadTimeout/WriteTimeout/ReadError/
        # WriteError/RemoteProtocolError/any other RequestError or
        # TransportError): a connection existed and the failure
        # happened during or after sending/receiving -- delivery cannot
        # be disproven. Never classified FAILED -- see this function's
        # docstring's classification rule.
        logger.warning("telegram_send_uncertain error_type=%s", type(exc).__name__)
        return TelegramSendOutcome.UNCERTAIN

    logger.info("telegram_send_sent")
    return TelegramSendOutcome.SENT


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float = 5.0,
        max_retries: int = 3,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send_job(self, job: Job, score: JobScore) -> bool:
        if not self.enabled:
            return False

        text = (
            "🔥 Новая вакансия\n"
            f"{job.title}\n"
            f"Компания: {job.company}\n"
            f"Локация: {job.location or 'не указана'}\n"
            f"Match: {score.score}/100\n"
            f"Рекомендация: {score.recommendation}\n"
            f"{job.url}"
        )

        for attempt in range(1, self.max_retries + 1):
            outcome = await send_telegram_text(
                self.bot_token, self.chat_id, text, timeout_seconds=self.timeout_seconds
            )
            if outcome is TelegramSendOutcome.SENT:
                logger.info("telegram_notification_sent attempt=%s", attempt)
                return True
            logger.warning(
                "telegram_notification_failed attempt=%s max_retries=%s outcome=%s",
                attempt,
                self.max_retries,
                outcome.value,
            )
            if attempt < self.max_retries:
                await asyncio.sleep(2 ** (attempt - 1))
        return False
