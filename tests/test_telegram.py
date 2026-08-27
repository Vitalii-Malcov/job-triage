import pytest

from app.models.job import Job, JobScore
from app.services.telegram import TelegramNotifier


@pytest.mark.asyncio
async def test_disabled_telegram_is_non_fatal():
    notifier = TelegramNotifier("", "", max_retries=1)
    job = Job(source="test", title="Python", company="X", url="https://example.com")
    score = JobScore(score=90, recommendation="APPLY")
    assert await notifier.send_job(job, score) is False
