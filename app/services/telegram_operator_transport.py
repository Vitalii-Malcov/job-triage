"""Stage 16: outbound Telegram transport for operator control.

The ONLY outbound capability Stage 16 introduces: a message to the
configured operator chat (`settings.telegram_chat_id`). The destination is
fixed when the transport is built -- callers pass text and buttons, never a
chat id -- so no workflow path can address any other chat.

Retry policy (bounded): only a provably FAILED attempt (the request never
reached Telegram or was rejected without creating a message, see
`app.services.telegram.send_telegram_message`) is retried, at most
`max_attempts` times in total with a short backoff. An UNCERTAIN attempt
stops immediately: Telegram may already have delivered it, and a retry
could duplicate the message. Never logs the token, chat id or text.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.telegram import (
    TELEGRAM_MESSAGE_HARD_LIMIT,
    TelegramSendOutcome,
    TelegramSendResult,
    send_telegram_message,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3


class TelegramOperatorTransport(Protocol):
    async def send(self, text: str, reply_markup: dict | None = None) -> TelegramSendResult: ...


class OperatorTransportNotConfiguredError(RuntimeError):
    """The bot token or the operator chat id is missing."""


class OperatorMessageTooLongError(ValueError):
    """The rendered text exceeds Telegram's hard limit (never truncated here)."""


class HttpTelegramOperatorTransport:
    def __init__(
        self,
        settings,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sender: Callable[..., Awaitable[TelegramSendResult]] = send_telegram_message,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise OperatorTransportNotConfiguredError("telegram bot token / chat id missing")
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._timeout = settings.telegram_timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._sender = sender
        self._sleep = sleep

    async def send(self, text: str, reply_markup: dict | None = None) -> TelegramSendResult:
        if len(text) > TELEGRAM_MESSAGE_HARD_LIMIT:
            raise OperatorMessageTooLongError(f"{len(text)} > {TELEGRAM_MESSAGE_HARD_LIMIT}")
        result = TelegramSendResult(TelegramSendOutcome.FAILED)
        for attempt in range(1, self._max_attempts + 1):
            result = await self._sender(
                self._token,
                self._chat_id,
                text,
                reply_markup=reply_markup,
                timeout_seconds=self._timeout,
            )
            if result.outcome is not TelegramSendOutcome.FAILED:
                return result
            logger.warning(
                "telegram_transport_failed attempt=%s max_attempts=%s", attempt, self._max_attempts
            )
            if attempt < self._max_attempts:
                await self._sleep(float(2 ** (attempt - 1)))
        return result
