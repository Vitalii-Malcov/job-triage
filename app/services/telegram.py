import asyncio
import logging

import httpx

from app.models.job import Job, JobScore

logger = logging.getLogger(__name__)


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
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, json={"chat_id": self.chat_id, "text": text})
                    response.raise_for_status()
                logger.info("telegram_notification_sent attempt=%s", attempt)
                return True
            except (httpx.HTTPError, httpx.TimeoutException):
                logger.exception(
                    "telegram_notification_failed attempt=%s max_retries=%s",
                    attempt,
                    self.max_retries,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))
        return False
