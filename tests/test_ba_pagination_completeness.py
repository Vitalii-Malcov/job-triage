"""Regression: remote BA searches were cut at 10 pages (1 000 hits) while the nationwide result
set holds about 3 000, and the report understated what was left out (found 2026-11-12: `Python`
announced 2 791 hits, 1 791 left out). The collector now pages to the API's own total, drops
identical repeats, stops on empty/repeated pages, and says why a fetch is partial."""

import asyncio
import json

import httpx
import pytest

from app.collectors import bundesagentur as ba
from app.collectors.bundesagentur import (
    ABSOLUTE_MAX_PAGES,
    COMPLETE,
    PARTIAL_API_LIMITATION,
    PARTIAL_PAGE_LIMIT,
    PARTIAL_REQUEST_BUDGET,
    BundesagenturAPIError,
    BundesagenturCollector,
    RequestGate,
)
from app.services.search_strategy import REMOTE_MAX_PAGES


def posting(n: int, *, home_office: bool = True, title: str | None = None) -> dict:
    return {
        "referenznummer": f"10001-{n:010d}-S",
        "stellenangebotsTitel": title or f"Python Entwickler {n}",
        "firma": "Firma GmbH",
        "stellenlokationen": [{"adresse": {"ort": "Berlin"}}],
        "homeofficemoeglich": home_office,
    }


class FakeApi:
    """BA search: `items` served in pages; hooks to break the paging."""

    def __init__(self, items, *, announced=None, serve_size=None, handler=None):
        self.items = items
        self.announced = len(items) if announced is None else announced
        self.serve_size = serve_size
        self.custom = handler
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.custom:
            return self.custom(request, self.requests)
        page = int(request.url.params["page"])
        size = self.serve_size or int(request.url.params["size"])
        chunk = self.items[(page - 1) * size : page * size]
        return httpx.Response(
            200,
            json={
                "ergebnisliste": chunk,
                "maxErgebnisse": self.announced,
                "page": page,
                "size": size,
            },
        )

    def collector(self, **kwargs) -> BundesagenturCollector:
        kwargs.setdefault("max_pages", REMOTE_MAX_PAGES)
        return BundesagenturCollector(
            api_key="test-key",
            keywords="Python",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            **kwargs,
        )


def fetch(collector):
    return asyncio.run(collector.fetch())


def test_remote_cap_covers_the_measured_nationwide_result_sets():
    assert REMOTE_MAX_PAGES * 100 >= 3200  # `Python` 2 791, `Softwareentwickler` ~3 100


def test_all_pages_are_fetched_up_to_the_announced_total():
    api = FakeApi([posting(n) for n in range(3150)])
    c = api.collector(home_office_only=True)
    jobs = fetch(c)
    assert len(jobs) == 3150 and len({j.source_reference for j in jobs}) == 3150
    assert (c.completeness, c.truncated, c.left_out) == (COMPLETE, False, 0)
    assert c.unique_results_seen == 3150 and c.pages_fetched == 32 and api.requests == 32


def test_home_office_filter_keeps_flagged_postings_and_counts_the_rest():
    items = [posting(n, home_office=n % 4 == 0) for n in range(400)]
    c = FakeApi(items).collector(home_office_only=True)
    jobs = fetch(c)
    assert len(jobs) == 100 and c.skipped_not_home_office == 300
    assert c.unique_results_seen == 400  # the filter does not hide how much the API delivered


def test_page_limit_is_reported_with_the_real_number_left_out():
    c = FakeApi([posting(n) for n in range(1000)]).collector(max_pages=3)
    jobs = fetch(c)
    assert len(jobs) == 300
    assert (c.completeness, c.truncated, c.left_out) == (PARTIAL_PAGE_LIMIT, True, 700)
    assert "PAGE LIMIT" in c.completeness_label


def test_empty_page_before_the_announced_total_is_an_api_limitation():
    c = FakeApi([posting(n) for n in range(250)], announced=900).collector()
    jobs = fetch(c)
    assert len(jobs) == 250
    assert (c.completeness, c.left_out) == (PARTIAL_API_LIMITATION, 650)
    assert c.completeness_label == "PARTIAL_RESULTS — API LIMITATION"


def test_a_repeated_page_stops_the_loop_instead_of_looping():
    first = [posting(n) for n in range(100)]

    def same_page_forever(request, count):
        return httpx.Response(
            200, json={"ergebnisliste": first, "maxErgebnisse": 5000, "size": 100}
        )

    api = FakeApi([], handler=same_page_forever)
    c = api.collector()
    jobs = fetch(c)
    assert len(jobs) == 100 and api.requests == 2  # page 2 repeated page 1: stop
    assert c.completeness == PARTIAL_API_LIMITATION and c.duplicates_skipped == 100


def test_postings_repeated_across_pages_are_dropped_and_counted():
    items = [posting(n) for n in range(150)]
    shifted = items[:100] + items[95:100] + items[100:]  # five postings come twice
    c = FakeApi(shifted, announced=150).collector()
    jobs = fetch(c)
    assert len(jobs) == 150 and c.duplicates_skipped == 5
    assert c.completeness == COMPLETE  # the announced 150 were all delivered


def test_two_different_postings_sharing_a_reference_both_stay_for_conflict_reporting():
    twin = posting(1, title="Another title")
    c = FakeApi([posting(1), twin]).collector()
    assert len(fetch(c)) == 2 and c.duplicates_skipped == 0


def test_api_serving_fewer_per_page_than_requested_is_followed():
    api = FakeApi([posting(n) for n in range(130)], serve_size=50)
    c = api.collector(page_size=100)
    assert len(fetch(c)) == 130 and c.completeness == COMPLETE and api.requests == 3


def test_max_pages_is_clamped_so_a_bad_setting_cannot_loop_forever():
    def endless(request, count):
        page = int(request.url.params["page"])
        chunk = [posting(page * 1000 + n) for n in range(100)]
        return httpx.Response(
            200, json={"ergebnisliste": chunk, "maxErgebnisse": 10**9, "size": 100}
        )

    api = FakeApi([], handler=endless)
    c = api.collector(max_pages=10_000)
    fetch(c)
    assert api.requests == ABSOLUTE_MAX_PAGES and c.completeness == PARTIAL_PAGE_LIMIT


def test_request_budget_of_the_run_is_reported_as_partial():
    api = FakeApi([posting(n) for n in range(1000)])
    c = api.collector(gate=RequestGate(max_requests=4))
    jobs = fetch(c)
    assert len(jobs) == 400 and api.requests == 4
    assert (c.completeness, c.left_out) == (PARTIAL_REQUEST_BUDGET, 600)


def test_no_announced_total_and_an_empty_page_is_a_complete_list():
    def no_total(request, count):
        page = int(request.url.params["page"])
        chunk = [posting(n) for n in range(3)] if page == 1 else []
        return httpx.Response(200, json={"ergebnisliste": chunk})

    c = FakeApi([], handler=no_total).collector()
    assert len(fetch(c)) == 3 and c.completeness == COMPLETE and c.left_out is None


def test_a_second_fetch_starts_from_zero():
    api = FakeApi([posting(n) for n in range(250)])
    c = api.collector(max_pages=1)
    fetch(c)
    assert c.truncated and c.unique_results_seen == 100
    c.max_pages = 5
    fetch(c)
    assert (c.completeness, c.unique_results_seen, c.pages_fetched) == (COMPLETE, 250, 3)


def test_retry_after_raises_the_backoff_but_is_capped():
    def response(status, **headers):
        request = httpx.Request("GET", "https://x")
        return httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(status, headers=headers, request=request)
        )

    assert ba._retry_delay(response(429, **{"Retry-After": "7"}), 1) == 7.0
    assert (
        ba._retry_delay(response(429, **{"Retry-After": "9999"}), 1) == ba.RETRY_AFTER_CAP_SECONDS
    )
    assert ba._retry_delay(response(429, **{"Retry-After": "soon"}), 3) == 4.0
    assert ba._retry_delay(response(500, **{"Retry-After": "7"}), 2) == 2.0
    assert ba._retry_delay(httpx.ReadTimeout("slow"), 3) == 4.0


def test_429_is_retried_with_backoff_then_succeeds(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ba.asyncio, "sleep", fake_sleep)

    def flaky(request, count):
        if count < 3:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json={"ergebnisliste": [posting(1)], "maxErgebnisse": 1})

    api = FakeApi([], handler=flaky)
    c = api.collector()
    assert len(fetch(c)) == 1
    assert sleeps == [5.0, 5.0] and api.requests == 3


def test_errors_after_the_retries_raise_and_do_not_report_a_complete_fetch(monkeypatch):
    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(ba.asyncio, "sleep", no_sleep)
    api = FakeApi([], handler=lambda request, count: httpx.Response(503))
    c = api.collector(max_retries=3)
    with pytest.raises(BundesagenturAPIError):
        fetch(c)
    assert api.requests == 3  # bounded, no endless retry


def test_timeout_is_retried_and_then_reported(monkeypatch):
    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(ba.asyncio, "sleep", no_sleep)

    def slow(request, count):
        raise httpx.ReadTimeout("slow", request=request)

    api = FakeApi([], handler=slow)
    with pytest.raises(BundesagenturAPIError):
        fetch(api.collector(max_retries=2))
    assert api.requests == 2


def test_zero_results_is_complete_and_a_one_off_shortfall_is_tolerated():
    c = FakeApi([], announced=0).collector()
    assert fetch(c) == [] and (c.completeness, c.left_out) == (COMPLETE, 0)
    c = FakeApi([posting(n) for n in range(100)], announced=101).collector()
    assert len(fetch(c)) == 100 and c.completeness == COMPLETE  # empty end, shortfall 1


def test_malformed_identity_fields_and_size_do_not_abort_the_search():
    def odd(request, count):
        bad = posting(1)
        bad["stellenangebotsTitel"] = ["not", "a", "string"]
        bad2 = posting(2)
        bad2["firma"] = {"x": 1}
        body = json.dumps({"ergebnisliste": [bad, bad2, posting(3)], "maxErgebnisse": 3})
        # a size no float can hold: json.loads turns 1e400 into inf
        return httpx.Response(200, content=body[:-1].encode() + b', "size": 1e400}')

    c = FakeApi([], handler=odd).collector()
    jobs = fetch(c)
    assert [j.source_reference for j in jobs] == ["10001-0000000003-S"]
    assert c.skipped_invalid_count == 2 and c.completeness == COMPLETE
