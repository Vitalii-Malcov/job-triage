import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.models.job import Job, JobScore

logger = logging.getLogger(__name__)

# Telegram's own hard cap on sendMessage's `text` field. Callers building
# longer content (e.g. app.services.telegram_digest) must truncate BEFORE
# calling send_telegram_text -- this module never truncates on a caller's
# behalf, since what's safe to drop is content-dependent.
TELEGRAM_MESSAGE_HARD_LIMIT = 4096

# HTTP statuses that are reliable evidence the sendMessage request was
# REJECTED without creating a message (Codex S9A-CODEX-001): the request was
# refused for its content/auth/target (400 bad request, 401 bad token, 403
# bot blocked / not in chat, 404 unknown token path) or throttled before
# processing (429 Too Many Requests). Every other non-2xx status -- notably
# 500/502/503/504, which an intermediary can return AFTER forwarding the
# POST to Telegram, plus statuses Telegram does not document for sendMessage
# (e.g. 409, 408) -- cannot disprove delivery and is UNCERTAIN.
DEFINITIVE_REJECTION_STATUS_CODES = frozenset({400, 401, 403, 404, 429})


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


@dataclass(frozen=True)
class TelegramSendResult:
    """`send_telegram_message`'s result: the outcome plus, when SENT and
    Telegram's response carried one, the delivered message's id."""

    outcome: TelegramSendOutcome
    message_id: int | None = None


def _extract_message_id(response: httpx.Response) -> int | None:
    """Best-effort read of `result.message_id` from a 2xx sendMessage
    response. A body that can't be parsed never turns a confirmed SENT into
    anything else -- the message_id is bookkeeping, not proof of delivery."""
    try:
        message_id = response.json()["result"]["message_id"]
    except (ValueError, KeyError, TypeError):
        return None
    return message_id if isinstance(message_id, int) else None


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

    - `httpx.HTTPStatusError` with a status in
      `DEFINITIVE_REJECTION_STATUS_CODES` (400/401/403/404/429) -- the
      request was rejected (e.g. bad token, chat not found, rate-limited)
      without a message being created. Any OTHER non-2xx status (5xx
      gateway/server errors, 409, ...) is UNCERTAIN (Codex S9A-CODEX-001):
      a gateway can return 502/504 after Telegram already accepted the
      message, so the error response does not prove non-delivery.
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
    result = await send_telegram_message(bot_token, chat_id, text, timeout_seconds=timeout_seconds)
    return result.outcome


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    timeout_seconds: float = 5.0,
) -> TelegramSendResult:
    """`send_telegram_text`'s implementation, additionally accepting an
    optional `reply_markup` (e.g. an inline keyboard, Stage 9A vacancy
    cards) and returning the delivered `message_id`. Same single-attempt,
    no-retry contract and the SAME outcome classification and logging
    hardening documented on `send_telegram_text`. Plain text only -- no
    `parse_mode`, so untrusted job content can never be interpreted as
    markup.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in DEFINITIVE_REJECTION_STATUS_CODES:
            # The request was rejected (bad token, chat not found,
            # rate-limited, ...) -- provably not delivered, safe to retry.
            logger.warning(
                "telegram_send_failed error_type=%s status_code=%s",
                type(exc).__name__,
                status_code,
            )
            return TelegramSendResult(TelegramSendOutcome.FAILED)
        # 5xx gateway/server errors and undocumented statuses: the POST may
        # already have been accepted upstream before this error response was
        # produced -- delivery cannot be disproven (S9A-CODEX-001).
        logger.warning(
            "telegram_send_uncertain error_type=%s status_code=%s",
            type(exc).__name__,
            status_code,
        )
        return TelegramSendResult(TelegramSendOutcome.UNCERTAIN)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        # The connection was never established (or timed out before it
        # was) -- no request bytes were ever transmitted. Provably not
        # delivered, safe to retry.
        logger.warning("telegram_send_failed error_type=%s", type(exc).__name__)
        return TelegramSendResult(TelegramSendOutcome.FAILED)
    except httpx.HTTPError as exc:
        # Everything else (ReadTimeout/WriteTimeout/ReadError/
        # WriteError/RemoteProtocolError/any other RequestError or
        # TransportError): a connection existed and the failure
        # happened during or after sending/receiving -- delivery cannot
        # be disproven. Never classified FAILED -- see this function's
        # docstring's classification rule.
        logger.warning("telegram_send_uncertain error_type=%s", type(exc).__name__)
        return TelegramSendResult(TelegramSendOutcome.UNCERTAIN)

    logger.info("telegram_send_sent")
    return TelegramSendResult(TelegramSendOutcome.SENT, _extract_message_id(response))


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
