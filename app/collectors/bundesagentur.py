import asyncio
import base64
import json
import logging
from datetime import datetime
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.collectors.base import CollectorError, JobCollector, is_configured
from app.models.job import Job

logger = logging.getLogger(__name__)

# Reverse-engineered public Bundesagentur fuer Arbeit "Jobsuche" API — see
# https://jobsuche.api.bund.dev/ and https://github.com/bundesAPI/jobsuche-api
# for the (unofficial, community-maintained) reference used while building
# this collector. There is no official self-service developer portal for a
# personal API key; the community uses the shared client id below.
#
# IMPORTANT: the community-maintained OpenAPI spec above documents an older
# response shape (`stellenangebote[]` with `refnr`/`beruf`/`arbeitgeber`/
# `arbeitsort`/`externeUrl`). A live GET against /pc/v6/jobs on 2026-08-26
# returned a different shape: `ergebnisliste[]` with `referenznummer`/
# `stellenangebotsTitel`/`firma`/`stellenlokationen[].adresse`/`externeURL`.
# This is an unofficial, undocumented government API that has evidently
# changed shape without the community docs catching up — the mapping below
# checks both field-name generations defensively rather than trusting either
# source blindly.
BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAIL_URL_TEMPLATE = (
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{encoded_refnr}"
)
PUBLIC_JOB_URL_TEMPLATE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
SOURCE_NAME = "bundesagentur"

# API-documented limit for the `veroeffentlichtseit` (published-since) filter.
MAX_VEROEFFENTLICHTSEIT_DAYS = 100


class BundesagenturAuthError(CollectorError):
    """Raised when the Jobsuche API rejects the configured X-API-Key (401),
    or when no key is configured at all. Not retried — retrying with the
    same key cannot succeed.
    """


class BundesagenturAPIError(CollectorError):
    """Raised when a Jobsuche API request ultimately fails: a 429/5xx
    response or a network error, after retries are exhausted.
    """


def is_api_key_configured(api_key: str) -> bool:
    """True if `api_key` is a real, usable key rather than empty/whitespace-only.

    Shared by BundesagenturCollector.fetch() and the
    POST /collectors/bundesagentur/run route so "is this thing configured"
    is defined in exactly one place rather than duplicated as two separate
    `if not ...:` checks that could drift out of sync. Thin wrapper over the
    cross-collector app.collectors.base.is_configured — kept as a named
    function here since existing call sites/tests import it from this module.
    """
    return is_configured(api_key)


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_postings(payload: dict) -> list[dict]:
    # Current API (observed live): "ergebnisliste". Older community-documented
    # shape: "stellenangebote". Support both. The value under either key is
    # untrusted external data too — if it isn't actually a list (e.g. a
    # single dict, or null), treat this page as having no postings rather
    # than crash; a single malformed page shouldn't lose the rest of the run.
    postings = payload.get("ergebnisliste")
    if postings is None:
        postings = payload.get("stellenangebote")
    if not isinstance(postings, list):
        if postings is not None:
            logger.warning(
                "bundesagentur_unexpected_postings_shape type=%s",
                type(postings).__name__,
            )
        return []
    return postings


def _extract_location(posting: dict) -> str:
    # Current API (observed live): "stellenlokationen" is a list of
    # {"adresse": {"ort", "region", ...}}. Older documented shape:
    # singular "arbeitsort" object with the same "ort"/"region" keys.
    # Both the list items and "adresse" are untrusted — guard every level
    # instead of assuming the nested shape matches expectations.
    locations = posting.get("stellenlokationen")
    if isinstance(locations, list) and locations:
        first = locations[0]
        adresse = first.get("adresse") if isinstance(first, dict) else None
        if isinstance(adresse, dict):
            return adresse.get("ort") or adresse.get("region") or ""
        logger.warning(
            "bundesagentur_unexpected_location_shape reason=stellenlokationen_item_not_dict "
            "referenznummer=%s",
            posting.get("referenznummer") or posting.get("refnr"),
        )
        # Fall through to the legacy singular-shape fallback below instead
        # of returning immediately — some old-shape postings still carry
        # "arbeitsort" even when "stellenlokationen" is present but broken.

    arbeitsort = posting.get("arbeitsort")
    if isinstance(arbeitsort, dict):
        return arbeitsort.get("ort") or arbeitsort.get("region") or ""
    return ""


def _map_posting(posting: object) -> Job | None:
    if not isinstance(posting, dict):
        logger.warning(
            "bundesagentur_skipped_invalid_posting reason=not_a_dict type=%s",
            type(posting).__name__,
        )
        return None

    refnr = posting.get("referenznummer") or posting.get("refnr")
    # NOTE: deliberately does NOT fall back to "beruf"/"hauptberuf" — those
    # describe the occupation classification (e.g. "Informatiker/in"), a
    # different field from the posting's actual title, and real API
    # responses can carry both simultaneously with different values. Falling
    # back to them here would make the mapped title (part of the dedup
    # fingerprint, see app.db.repositories._fingerprint) flip between runs
    # whenever "stellenangebotsTitel" is transiently absent, silently
    # creating duplicate JobRecords for the same real posting. "titel" is
    # kept as a fallback based on the older community-documented schema;
    # not confirmed against live search-list data (0/100 occurrence in the
    # 2026-08-26 sample below) — per that same old documentation, "titel"
    # actually belongs to the job-detail endpoint, not the search-list
    # endpoint used here, so it may describe a different endpoint version
    # rather than a genuine synonym for this field.
    # Verified against a live 100-posting sample on 2026-08-26: 100/100 had
    # "stellenangebotsTitel" populated, so this restriction does not cause
    # mass skipping of real postings.
    title = posting.get("stellenangebotsTitel") or posting.get("titel")
    company = posting.get("firma") or posting.get("arbeitgeber")

    if not refnr or not title or not company:
        logger.warning(
            "bundesagentur_skipped_invalid_posting reason=missing_required_field "
            "refnr=%s title=%s company=%s",
            refnr,
            title,
            company,
        )
        return None

    location = _extract_location(posting)
    # externeURL (observed live) vs externeUrl (older community docs).
    url = (
        posting.get("externeURL")
        or posting.get("externeUrl")
        or PUBLIC_JOB_URL_TEMPLATE.format(refnr=refnr)
    )

    try:
        return Job(
            source=SOURCE_NAME,
            title=title,
            company=company,
            location=location,
            url=url,
            description="",
            source_reference=str(refnr),
        )
    except ValidationError:
        logger.warning(
            "bundesagentur_skipped_invalid_posting reason=validation_error refnr=%s",
            refnr,
        )
        return None


class BundesagenturCollector(JobCollector):
    """Collector for the Bundesagentur fuer Arbeit "Jobsuche" API.

    Only fetches and maps postings into `Job` — it never writes to the
    database and never scores jobs (see app/api/routes.py for that).
    """

    source = SOURCE_NAME

    def __init__(
        self,
        api_key: str,
        keywords: str = "",
        location: str = "",
        radius_km: int = 25,
        page_size: int = 100,
        max_pages: int = 20,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.keywords = keywords
        self.location = location
        self.radius_km = radius_km
        self.page_size = page_size
        self.max_pages = max_pages
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        # Injected only by tests, to avoid real network calls; production
        # code always goes through the `async with httpx.AsyncClient(...)`
        # branch in fetch() so the connection is always closed.
        self._injected_client = http_client
        # Set on every fetch() call; read by callers after awaiting fetch()
        # to report how many postings were skipped (see POST
        # /collectors/bundesagentur/run's `skipped_invalid` field). Kept out
        # of the JobCollector interface itself since `fetch()` must keep
        # returning a plain list[Job].
        self.skipped_invalid_count = 0

    async def fetch(self, since: datetime | None = None) -> list[Job]:
        if not is_api_key_configured(self.api_key):
            raise BundesagenturAuthError("BUNDESAGENTUR_API_KEY is not configured")

        self.skipped_invalid_count = 0
        headers = {"X-API-Key": self.api_key}
        params = self._build_params(since)

        if self._injected_client is not None:
            return await self._collect_pages(self._injected_client, headers, params)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            return await self._collect_pages(client, headers, params)

    async def fetch_detail(self, referenznummer: str) -> str | None:
        """Fetch the live detail description, or return None for a permanent 404."""
        if not is_api_key_configured(self.api_key):
            raise BundesagenturAuthError("BUNDESAGENTUR_API_KEY is not configured")

        encoded_refnr = quote(
            base64.b64encode(referenznummer.encode("utf-8")).decode("ascii"),
            safe="",
        )
        url = DETAIL_URL_TEMPLATE.format(encoded_refnr=encoded_refnr)
        headers = {"X-API-Key": self.api_key}

        if self._injected_client is not None:
            payload = await self._request_json(
                self._injected_client,
                url,
                headers,
                params=None,
                request_name="detail",
                return_none_on_404=True,
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                payload = await self._request_json(
                    client,
                    url,
                    headers,
                    params=None,
                    request_name="detail",
                    return_none_on_404=True,
                )

        if payload is None:
            return None

        description = payload.get("stellenangebotsBeschreibung")
        if not isinstance(description, str) or not description.strip():
            logger.warning(
                "bundesagentur_detail_missing_description referenznummer=%s",
                referenznummer,
            )
            return None
        return description

    def _build_params(self, since: datetime | None) -> dict[str, str | int]:
        params: dict[str, str | int] = {"size": self.page_size}
        if self.keywords:
            params["was"] = self.keywords
        if self.location:
            params["wo"] = self.location
        if self.radius_km:
            params["umkreis"] = self.radius_km
        if since is not None:
            now = datetime.now(since.tzinfo) if since.tzinfo is not None else datetime.now()
            days_since = max(0, (now - since).days)
            params["veroeffentlichtseit"] = min(days_since, MAX_VEROEFFENTLICHTSEIT_DAYS)
        return params

    async def _collect_pages(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        base_params: dict[str, str | int],
    ) -> list[Job]:
        jobs: list[Job] = []
        total_results: int | None = None
        page = 1

        while page <= self.max_pages:
            page_params = {**base_params, "page": page}
            payload = await self._get_page(client, headers, page_params)

            if total_results is None:
                total_results = _parse_int(payload.get("maxErgebnisse"))

            postings = _extract_postings(payload)
            if not postings:
                break

            for posting in postings:
                job = _map_posting(posting)
                if job is None:
                    self.skipped_invalid_count += 1
                else:
                    jobs.append(job)

            if total_results is not None and page * self.page_size >= total_results:
                break
            page += 1

        return jobs

    async def _get_page(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        params: dict[str, str | int],
    ) -> dict:
        payload = await self._request_json(
            client,
            BASE_URL,
            headers,
            params=params,
            request_name="search",
        )
        if payload is None:  # pragma: no cover - search does not enable this outcome
            raise BundesagenturAPIError("Bundesagentur search unexpectedly returned no payload")
        return payload

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        params: dict[str, str | int] | None,
        request_name: str,
        return_none_on_404: bool = False,
    ) -> dict | None:
        """Run one API request through the collector's shared retry policy."""
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 401:
                    raise BundesagenturAuthError(
                        "Bundesagentur Jobsuche API rejected the configured X-API-Key (401)"
                    )
                if return_none_on_404 and response.status_code == 404:
                    logger.warning(
                        "bundesagentur_detail_not_found status=404 body=%s",
                        response.text[:200],
                    )
                    return None
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    # A malformed individual posting is skip-and-log (see
                    # _map_posting) since the rest of the page is likely
                    # fine. This is different: the whole response envelope
                    # isn't shaped like an API response at all, which means
                    # something is fundamentally wrong upstream (API
                    # incompatibility, proxy/WAF interference, etc.) rather
                    # than one bad record — not retryable (a malformed shape
                    # won't fix itself) and not silently treated as "0
                    # results", which would look identical to a real empty
                    # search and hide the failure from operators.
                    raise BundesagenturAPIError(
                        "Unexpected Jobsuche API response shape: expected a JSON object, "
                        f"got {type(payload).__name__}"
                    )
                return payload
            except BundesagenturAuthError:
                raise
            except BundesagenturAPIError:
                raise
            except json.JSONDecodeError:
                # response.json() is a thin wrapper over the stdlib json
                # module (verified against httpx 0.28.1), so this is what a
                # 200 response with a non-JSON or empty body raises — e.g. a
                # WAF/proxy error page or a truncated response. Same
                # rationale as the non-dict-payload case above: not
                # retryable, and must not be silently swallowed as "0
                # results". Truncate the body in the log/error message so a
                # large HTML error page doesn't flood the logs.
                snippet = response.text[:100]
                raise BundesagenturAPIError(
                    "Bundesagentur Jobsuche API returned a non-JSON response body "
                    f"(status={response.status_code}): {snippet!r}"
                ) from None
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_exc = exc
                logger.warning(
                    "bundesagentur_fetch_failed request=%s attempt=%s max_retries=%s "
                    "params=%s error=%s",
                    request_name,
                    attempt,
                    self.max_retries,
                    params,
                    exc,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))

        logger.error(
            "bundesagentur_fetch_exhausted_retries request=%s params=%s",
            request_name,
            params,
        )
        raise BundesagenturAPIError(
            f"Bundesagentur Jobsuche API request failed after {self.max_retries} attempts"
        ) from last_exc
