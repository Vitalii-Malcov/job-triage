import asyncio
import base64
import json
import logging
import time
from datetime import datetime
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.agents.target_role import is_search_target_title
from app.collectors.base import CollectorError, JobCollector
from app.models.job import Job
from app.utils.config_flags import is_configured

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


class RequestBudgetExhausted(Exception):
    """The run's ceiling on search requests is reached; nothing was sent."""


class RequestGate:
    """Pacing and ceilings for ONE run, shared by every collector the run builds and applied at
    the HTTP boundary: each attempt (search or detail, retries included) waits for the minimum
    interval since the previous one, search attempts are charged against `max_requests` and
    detail attempts against `max_detail`."""

    def __init__(
        self,
        interval_seconds: float = 0.0,
        max_requests: int | None = None,
        max_detail: int | None = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.max_requests = max_requests
        self.max_detail = max_detail
        self.search_attempts = 0
        self.detail_attempts = 0
        self.exhausted = False
        self._last: float | None = None

    def detail_exhausted(self) -> bool:
        return self.max_detail is not None and self.detail_attempts >= self.max_detail

    async def before_attempt(self, kind: str) -> None:
        if kind == "search" and (
            self.max_requests is not None and self.search_attempts >= self.max_requests
        ):
            self.exhausted = True
            raise RequestBudgetExhausted
        if kind == "detail" and self.detail_exhausted():
            self.exhausted = True
            raise RequestBudgetExhausted
        if self.interval_seconds > 0 and self._last is not None:
            pause = self.interval_seconds - (time.monotonic() - self._last)
            if pause > 0:
                await asyncio.sleep(pause)
        self._last = time.monotonic()
        if kind == "search":
            self.search_attempts += 1
        elif kind == "detail":
            self.detail_attempts += 1


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
    cross-collector app.utils.config_flags.is_configured — kept as a named
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


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if low <= value <= high else None


def _short_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= limit else None


def structured_facts(posting: dict) -> dict:
    """Stage 22 P1: the BA search result's own structured fields --
    workplace coordinates/postcode/region of the FIRST `stellenlokation`
    (with the number of locations), home-office flags and the
    `quereinstiegGeeignet` flag. Every value is sanitized; an unexpected
    shape becomes None (no signal), never a dropped posting. Observed on
    2026-10-30: all 3 073 search results carried coordinates; home-office
    flags were set on about half of them."""
    facts: dict = {}
    locations = posting.get("stellenlokationen")
    if isinstance(locations, list):
        facts["workplace_count"] = len(locations) if len(locations) <= 1000 else None
        first = locations[0] if locations and isinstance(locations[0], dict) else {}
        lat = _number(first.get("breite"), -90, 90)
        lon = _number(first.get("laenge"), -180, 180)
        if lat is not None and lon is not None:
            facts["workplace_lat"], facts["workplace_lon"] = lat, lon
        address = first.get("adresse") if isinstance(first.get("adresse"), dict) else {}
        facts["workplace_postal_code"] = _short_text(address.get("plz"), 10)
        facts["workplace_region"] = _short_text(address.get("region"), 40)
    if isinstance(posting.get("homeofficemoeglich"), bool):
        facts["home_office_possible"] = posting["homeofficemoeglich"]
    facts["home_office_type"] = _short_text(posting.get("homeofficetyp"), 40)
    percent = _number(posting.get("homeofficeprozent"), 0, 100)
    facts["home_office_percent"] = int(percent) if percent is not None else None
    if isinstance(posting.get("arbeitszeitVollzeit"), bool):
        facts["full_time_offered"] = posting["arbeitszeitVollzeit"]
    if isinstance(posting.get("quereinstiegGeeignet"), bool):
        facts["quereinstieg_suitable"] = posting["quereinstiegGeeignet"]
    return {key: value for key, value in facts.items() if value is not None}


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

    # "stellenangebotsart" (observed live on both the search-list and detail
    # endpoints, 2026-09-15) is the API's own controlled-vocabulary posting
    # type -- ARBEIT (real employment), SELBSTAENDIGKEIT (genuine
    # self-employed/freelance roles, e.g. commission-based sales reps --
    # confirmed live NOT exclusive to training providers, despite
    # alfatraining's course listings also using it), PRAKTIKUM_TRAINEE
    # (internship), AUSBILDUNG (apprenticeship/dual study). See
    # app.agents.posting_classifier for how this keeps non-target-employment
    # listings out of the normal APPLY pipeline (Stage 10 finding) --
    # deliberately NOT filtered here at the collector level, so every
    # posting still becomes a persisted JobRecord (dedup/ingestion history
    # preserved) and only the scoring step decides eligibility.
    posting_type = posting.get("stellenangebotsart")

    try:
        return Job(
            **structured_facts(posting),
            source=SOURCE_NAME,
            title=title,
            company=company,
            location=location,
            url=url,
            description="",
            source_reference=str(refnr),
            posting_type=posting_type if isinstance(posting_type, str) else None,
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
        working_time: str = "",
        home_office_only: bool = False,
        page_size: int = 100,
        max_pages: int = 20,
        offer_type: int | None = None,
        published_within_days: int | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        target_titles_only: bool = False,
        gate: RequestGate | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.keywords = keywords
        self.location = location
        self.radius_km = radius_km
        # Stage 17C: BA `arbeitszeit` filter ("ho" = Heim-/Telearbeit); empty = any.
        self.working_time = working_time
        # Stage 22 P1: keep only postings whose own `homeofficemoeglich` is
        # true (the BA's `arbeitszeit=ho` filter silently returns 0 results
        # on the v6 API, observed 2026-10-30). Filtered postings are counted
        # in `skipped_not_home_office`, never as invalid.
        self.home_office_only = home_office_only
        self.skipped_not_home_office = 0
        # Nationwide discovery: optionally drop postings whose title names no development role
        # (no detail request and no stored row for them), counted and never "invalid". The BA
        # home-office flag is NOT used as an exclusion here: it is optional and employer-set,
        # so the posting text decides.
        self.target_titles_only = target_titles_only
        self.skipped_not_target_title = 0
        # Rate limiting and request ceiling over HTTP attempts (retries included), shared by
        # every collector of a run. None = unpaced and unlimited, the legacy behaviour.
        self.gate = gate
        self.requests_made = 0
        # Set when the result list was longer than `max_pages` allowed.
        self.truncated = False
        self.page_size = page_size
        self.max_pages = max_pages
        # Stage 23: opt-in BA filters, off by default (behaviour unchanged).
        # `offer_type` is `angebotsart` (1 = employment only: drops apprenticeships,
        # internships and self-employment courses). `published_within_days` is
        # `veroeffentlichtseit`; measured live 2026-11-01, only 7 and 14 narrow
        # the result (other values returned the unfiltered list).
        self.offer_type = offer_type
        self.published_within_days = published_within_days
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
        # `maxErgebnisse` of the last fetch (None when the API did not say);
        # with `truncated` it tells how many results `max_pages` left out.
        self.total_results: int | None = None

    async def fetch(self, since: datetime | None = None) -> list[Job]:
        if not is_api_key_configured(self.api_key):
            raise BundesagenturAuthError("BUNDESAGENTUR_API_KEY is not configured")

        self.skipped_invalid_count = 0
        self.skipped_not_home_office = 0
        self.skipped_not_target_title = 0
        self.requests_made = 0
        self.truncated = False
        self.total_results = None
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
        if self.working_time:
            params["arbeitszeit"] = self.working_time
        if self.offer_type is not None:
            params["angebotsart"] = self.offer_type
        if self.published_within_days is not None:
            params["veroeffentlichtseit"] = self.published_within_days
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
            try:
                payload = await self._get_page(client, headers, page_params)
            except RequestBudgetExhausted:
                self.truncated = True
                break

            if total_results is None:
                total_results = _parse_int(payload.get("maxErgebnisse"))
                self.total_results = total_results

            postings = _extract_postings(payload)
            if not postings:
                break

            for posting in postings:
                job = _map_posting(posting)
                if job is None:
                    self.skipped_invalid_count += 1
                elif self.home_office_only and job.home_office_possible is not True:
                    self.skipped_not_home_office += 1
                elif self.target_titles_only and not is_search_target_title(job.title):
                    self.skipped_not_target_title += 1
                else:
                    jobs.append(job)

            if total_results is not None and page * self.page_size >= total_results:
                break
            page += 1
        else:
            self.truncated = (
                total_results is None or total_results > self.max_pages * self.page_size
            )

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
            if self.gate is not None:
                # before EVERY HTTP attempt, a retry included: pace it and charge it
                await self.gate.before_attempt(request_name)
            if request_name == "search":
                self.requests_made += 1
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
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    # A rejected request (bad parameter, 403, 404 on a page
                    # call, ...) is identical on every retry: fail fast
                    # instead of sleeping through the whole retry budget.
                    raise BundesagenturAPIError(
                        "Bundesagentur Jobsuche API rejected the request "
                        f"(status={response.status_code})"
                    )
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
                # results".
                #
                # BOUND-011b (api-boundaries hardening r1, privacy
                # second-pass): the raw response snippet used to be
                # embedded directly into THIS exception's own message --
                # which app/api/routes.py's collector-run endpoint
                # interpolates verbatim into a client-visible 502
                # `detail`, on the documented assumption (see that route's
                # own comments) that a CollectorError's message is always
                # already sanitized before it gets there. That assumption
                # was false for this one path: an upstream WAF/proxy error
                # page's own body text (untrusted, third-party-controlled
                # content) reached the API client. The snippet is now only
                # logged server-side (truncated, matching the sibling
                # 404-body log above), never placed in the exception
                # message a client can see.
                logger.warning(
                    "bundesagentur_non_json_response status=%s body=%s",
                    response.status_code,
                    response.text[:200],
                )
                raise BundesagenturAPIError(
                    "Bundesagentur Jobsuche API returned a non-JSON response body "
                    f"(status={response.status_code})"
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
