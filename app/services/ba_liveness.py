"""Is a Bundesagentur posting still online? (Stage 22, P0-2)

Read-only: one detail request per reference, nothing is written anywhere.
The 2026-10-30 research treated every 404 as "closed" and was wrong 478 of
644 times -- it queried truncated references (URL path tokens such as
`12265-444700` for `12265-444700_JB5270016-S`), which the BA answers with 404
just like a withdrawn posting. Here a 404 counts as CLOSED only for a
reference in the strict BA shape (`app.collectors.ba_reference`); anything
the source could not answer is SOURCE_ERROR, never CLOSED.

States:
- ACTIVE            200 with a JSON object (the posting is served);
- CLOSED            404 for a strictly valid reference;
- INVALID_REFERENCE no reference, or not in the strict BA shape (no request);
- SOURCE_ERROR      429, 5xx, timeout, network error, non-JSON body, or a
                    rejected key (401/403) -- temporary or ours, says nothing
                    about the posting (retried once, except a rejected key,
                    which stops a CLI run);
- UNKNOWN           any other answer (another 4xx, a 200 naming a different
                    reference).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

import httpx

from app.collectors.ba_reference import is_valid_ba_reference
from app.collectors.bundesagentur import DETAIL_URL_TEMPLATE

MAX_ATTEMPTS = 2
MAX_RETRY_AFTER_SECONDS = 30.0
KEY_REJECTED = "key rejected"


class Liveness(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    SOURCE_ERROR = "SOURCE_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class LivenessResult:
    reference: str | None
    state: Liveness
    http_status: int | None
    reason: str


def detail_url(reference: str) -> str:
    """The detail endpoint for `reference` (base64, then URL-encoded --
    the same encoding as `BundesagenturCollector.fetch_detail`)."""
    encoded = quote(base64.b64encode(reference.encode("utf-8")).decode("ascii"), safe="")
    return DETAIL_URL_TEMPLATE.format(encoded_refnr=encoded)


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(
            max(float(response.headers.get("Retry-After", "1")), 0.0), MAX_RETRY_AFTER_SECONDS
        )
    except ValueError:
        return 1.0


def classify_response(reference: str, response: httpx.Response) -> LivenessResult:
    status = response.status_code
    if status == 200:
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return LivenessResult(reference, Liveness.SOURCE_ERROR, status, "200 without JSON")
        if not isinstance(payload, dict):
            return LivenessResult(reference, Liveness.SOURCE_ERROR, status, "200 without object")
        served = payload.get("referenznummer") or payload.get("refnr")
        if isinstance(served, str) and served.strip() and served.strip() != reference:
            return LivenessResult(
                reference, Liveness.UNKNOWN, status, "200 for a different reference"
            )
        return LivenessResult(reference, Liveness.ACTIVE, status, "served")
    if status == 404:
        return LivenessResult(reference, Liveness.CLOSED, status, "not found")
    if status in (401, 403):
        return LivenessResult(reference, Liveness.SOURCE_ERROR, status, KEY_REJECTED)
    if status in (408, 429) or status >= 500:
        return LivenessResult(reference, Liveness.SOURCE_ERROR, status, "temporary source error")
    return LivenessResult(reference, Liveness.UNKNOWN, status, "unexpected status")


class Pacer:
    """At most one request per `interval` seconds -- awaited before EVERY
    HTTP attempt, retries included."""

    def __init__(self, interval: float, *, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    async def wait(self) -> None:
        if self._last is not None:
            remaining = self.interval - (self._clock() - self._last)
            if remaining > 0:
                await self._sleep(remaining)
        self._last = self._clock()


async def check_reference(
    client: httpx.AsyncClient,
    api_key: str,
    reference: str | None,
    *,
    pacer: Pacer | None = None,
    sleep=asyncio.sleep,
) -> LivenessResult:
    """One posting. INVALID_REFERENCE without any request. Every SOURCE_ERROR
    except a rejected key (retrying cannot help; the caller stops the run)
    is retried once after its back-off, then reported as SOURCE_ERROR."""
    if not reference or not is_valid_ba_reference(reference):
        reason = "no reference" if not reference else "not a BA reference"
        return LivenessResult(reference, Liveness.INVALID_REFERENCE, None, reason)
    result = LivenessResult(reference, Liveness.SOURCE_ERROR, None, "not requested")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if pacer is not None:
            await pacer.wait()
        delay = 1.0
        try:
            response = await client.get(detail_url(reference), headers={"X-API-Key": api_key})
        except httpx.TimeoutException:
            result = LivenessResult(reference, Liveness.SOURCE_ERROR, None, "timeout")
        except httpx.HTTPError as exc:
            result = LivenessResult(
                reference, Liveness.SOURCE_ERROR, None, f"network error ({type(exc).__name__})"
            )
        else:
            result = classify_response(reference, response)
            if result.state is not Liveness.SOURCE_ERROR or result.reason == KEY_REJECTED:
                return result
            delay = _retry_after(response)
        if attempt < MAX_ATTEMPTS:
            await sleep(delay)
    return result
