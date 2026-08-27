import httpx
import pytest

from app.collectors.bundesagentur import (
    BundesagenturAPIError,
    BundesagenturAuthError,
    BundesagenturCollector,
)


async def _no_sleep(_seconds: float) -> None:
    return None


def _page(postings: list[dict], max_ergebnisse: int | None = None) -> dict:
    # Field names match the live API response shape observed on 2026-08-26
    # (see app/collectors/bundesagentur.py module docstring for why this
    # differs from the community-documented OpenAPI spec).
    return {
        "ergebnisliste": postings,
        "maxErgebnisse": max_ergebnisse if max_ergebnisse is not None else len(postings),
        "page": 1,
        "size": 100,
    }


def _posting(**overrides) -> dict:
    data = {
        "referenznummer": "10000-1184867112-S",
        "stellenangebotsTitel": "Python Entwickler",
        "firma": "Example GmbH",
        "stellenlokationen": [{"adresse": {"ort": "Berlin", "plz": "10115", "region": "BERLIN"}}],
        "datumErsteVeroeffentlichung": "2026-08-20",
    }
    data.update(overrides)
    return data


def _legacy_posting(**overrides) -> dict:
    """Older community-documented field-name shape (refnr/titel/arbeitgeber/
    arbeitsort/externeUrl) — kept to prove the fallback mapping still works
    if the API reverts or a different endpoint version is used. Includes
    "beruf" alongside a real "titel" because real postings carry both
    simultaneously with different meanings (beruf = occupation
    classification, titel/stellenangebotsTitel = the actual posting title);
    "beruf" must be present-but-ignored here, not used as a title fallback
    (see the P0 dedup-instability fix in _map_posting).
    """
    data = {
        "refnr": "10000-1184867112-S",
        "titel": "Python Entwickler",
        "beruf": "Informatiker/in",
        "arbeitgeber": "Example GmbH",
        "arbeitsort": {"ort": "Berlin", "plz": 10115, "region": "Berlin"},
        "aktuelleVeroeffentlichungsdatum": "2026-08-20",
    }
    data.update(overrides)
    return data


def _make_collector(handler, **kwargs) -> BundesagenturCollector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return BundesagenturCollector(api_key="test-key", http_client=client, **kwargs)


@pytest.mark.asyncio
async def test_maps_a_valid_posting_including_generated_detail_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json=_page([_posting()]))

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "bundesagentur"
    assert job.title == "Python Entwickler"
    assert job.company == "Example GmbH"
    assert job.location == "Berlin"
    assert "10000-1184867112-S" in str(job.url)
    assert collector.skipped_invalid_count == 0


@pytest.mark.asyncio
async def test_uses_externe_url_when_present():
    posting = _posting(externeURL="https://careers.example.com/job/42")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([posting]))

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert str(jobs[0].url) == "https://careers.example.com/job/42"


@pytest.mark.asyncio
async def test_skips_posting_missing_required_fields_without_failing_the_batch():
    good = _posting()
    bad = _posting(referenznummer="10000-2", firma=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([good, bad]))

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert jobs[0].company == "Example GmbH"
    assert collector.skipped_invalid_count == 1


@pytest.mark.asyncio
async def test_maps_legacy_field_names_as_a_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        # Older community-documented shape: top-level "stellenangebote" and
        # refnr/beruf/arbeitgeber/arbeitsort/externeUrl field names.
        return httpx.Response(
            200,
            json={
                "stellenangebote": [_legacy_posting()],
                "maxErgebnisse": 1,
                "page": 1,
                "size": 100,
            },
        )

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Python Entwickler"
    assert job.company == "Example GmbH"
    assert job.location == "Berlin"
    assert "10000-1184867112-S" in str(job.url)


@pytest.mark.asyncio
async def test_skips_legacy_posting_when_only_occupation_field_is_present():
    """A legacy-shaped posting with "beruf" (occupation classification) but
    no real title field ("titel"/"stellenangebotsTitel") must be skipped,
    not mapped with the occupation as a fake title.
    """
    posting = _legacy_posting(titel=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stellenangebote": [posting],
                "maxErgebnisse": 1,
                "page": 1,
                "size": 100,
            },
        )

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert jobs == []
    assert collector.skipped_invalid_count == 1


@pytest.mark.asyncio
async def test_fingerprint_is_stable_when_optional_title_field_is_missing_across_runs():
    """Regression test for a confirmed dedup-breaking bug: hauptberuf/beruf
    describe the occupation classification (e.g. "Informatiker/in"), not the
    posting's actual title, and real postings carry both fields
    simultaneously with different values. The old fallback chain
    (stellenangebotsTitel -> titel -> beruf -> hauptberuf) meant that when
    stellenangebotsTitel was transiently missing, the mapped title silently
    changed to the occupation classification, changing the dedup fingerprint
    (source+company+title+url) and causing upsert_job to insert a duplicate
    JobRecord for the same real posting (referenznummer) instead of updating
    it. The fix removes the beruf/hauptberuf fallback entirely: a posting
    missing a real title field is now skipped rather than mapped with a
    wrong title, so it can never reach upsert_job with a corrupted
    fingerprint. Verified against a live 100-posting sample on 2026-08-26:
    stellenangebotsTitel was present on 100/100, so this does not cause mass
    skipping of real postings.
    """
    same_referenznummer = "X-1"
    posting_with_title = _posting(
        referenznummer=same_referenznummer,
        stellenangebotsTitel="Backend Engineer (m/w/d)",
        hauptberuf="Informatiker/in",
    )
    posting_missing_title = _posting(
        referenznummer=same_referenznummer,
        stellenangebotsTitel=None,
        hauptberuf="Informatiker/in",
    )

    def handler_run1(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([posting_with_title]))

    def handler_run2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([posting_missing_title]))

    collector_run1 = _make_collector(handler_run1)
    jobs_run1 = await collector_run1.fetch()
    assert len(jobs_run1) == 1
    assert jobs_run1[0].title == "Backend Engineer (m/w/d)"
    assert collector_run1.skipped_invalid_count == 0

    collector_run2 = _make_collector(handler_run2)
    jobs_run2 = await collector_run2.fetch()
    assert jobs_run2 == []
    assert collector_run2.skipped_invalid_count == 1


@pytest.mark.asyncio
async def test_skips_posting_with_non_dict_location_item():
    """stellenlokationen is a list but its first element is not a dict
    (e.g. null) — must degrade to an empty location, not crash the run.
    """
    posting = _posting(stellenlokationen=[None])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([posting]))

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert jobs[0].location == ""
    assert collector.skipped_invalid_count == 0


@pytest.mark.asyncio
async def test_skips_non_dict_items_in_ergebnisliste():
    """ergebnisliste containing non-dict items (e.g. plain strings) must
    skip just those items, not crash the whole page/run.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ergebnisliste": [_posting(), "not-a-dict"],
                "maxErgebnisse": 2,
                "page": 1,
                "size": 100,
            },
        )

    collector = _make_collector(handler)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert collector.skipped_invalid_count == 1


@pytest.mark.asyncio
async def test_raises_api_error_on_non_dict_top_level_payload_without_retrying():
    """A completely unexpected top-level JSON shape (e.g. a bare list
    instead of an object) means the response format itself is broken, not
    that one record is bad — must raise a clear CollectorError (so the
    endpoint maps it to 502, not a silent '0 results' or a bare
    AttributeError-turned-500), and must not waste retries on a shape that
    won't fix itself.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[1, 2, 3])

    collector = _make_collector(handler, max_retries=3)

    with pytest.raises(BundesagenturAPIError):
        await collector.fetch()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_raises_api_error_on_non_json_response_body():
    """A 200 response whose body isn't JSON at all (e.g. a WAF/proxy error
    page) must not crash with a bare json.JSONDecodeError — it must raise
    the same CollectorError as other malformed-envelope cases, and must not
    waste retries on a shape that won't fix itself.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"<html>Service temporarily unavailable</html>")

    collector = _make_collector(handler, max_retries=3)

    with pytest.raises(BundesagenturAPIError):
        await collector.fetch()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_raises_api_error_on_empty_response_body():
    """A 200 response with an empty body (truncated response) must also
    raise a clear CollectorError instead of a bare json.JSONDecodeError,
    and must not be retried.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"")

    collector = _make_collector(handler, max_retries=3)

    with pytest.raises(BundesagenturAPIError):
        await collector.fetch()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_paginates_until_max_ergebnisse_is_reached():
    page1 = _page([_posting(referenznummer="1"), _posting(referenznummer="2")], max_ergebnisse=3)
    page2 = _page([_posting(referenznummer="3")], max_ergebnisse=3)
    seen_pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        seen_pages.append(page)
        return httpx.Response(200, json=page1 if page == "1" else page2)

    collector = _make_collector(handler, page_size=2)
    jobs = await collector.fetch()

    assert len(jobs) == 3
    assert seen_pages == ["1", "2"]


@pytest.mark.asyncio
async def test_stops_when_a_page_comes_back_empty():
    page1 = _page([_posting(referenznummer="1")], max_ergebnisse=100)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json=_page([], max_ergebnisse=100))

    collector = _make_collector(handler, page_size=1)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_raises_auth_error_on_401_without_retrying():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"detail": "invalid key"})

    collector = _make_collector(handler, max_retries=3)

    with pytest.raises(BundesagenturAuthError):
        await collector.fetch()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_raises_auth_error_when_no_api_key_is_configured():
    collector = BundesagenturCollector(api_key="")

    with pytest.raises(BundesagenturAuthError):
        await collector.fetch()


@pytest.mark.asyncio
async def test_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", _no_sleep)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=_page([_posting()]))

    collector = _make_collector(handler, max_retries=3)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_raises_api_error_after_exhausting_retries_on_5xx(monkeypatch):
    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", _no_sleep)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    collector = _make_collector(handler, max_retries=2)

    with pytest.raises(BundesagenturAPIError):
        await collector.fetch()

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", _no_sleep)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, json=_page([_posting()]))

    collector = _make_collector(handler, max_retries=3)
    jobs = await collector.fetch()

    assert len(jobs) == 1
    assert attempts["count"] == 2
