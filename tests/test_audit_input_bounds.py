"""Audit API-01 / API-02 / API-03 + sibling quadratic regexes: bounded input.

Hostile-input timing runs in a child process with a kill timeout (see
tests/test_html_text_redos.py for the rationale).
"""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.job import Job, JobIngest
from app.security import rate_limit as rate_limit_module
from app.security.id_bounds import _out_of_range, reject_out_of_range_ids

REPO = Path(__file__).resolve().parents[1]
API_KEY = "test-api-key"
HUGE_ID = 2**63  # first value SQLite/PostgreSQL bigint cannot hold


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bounds.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    settings = Settings(api_key=API_KEY, rate_limit_requests=10_000, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
    rate_limit_module._requests.clear()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


def _headers():
    return {"X-API-Key": API_KEY}


# ---- API-01 -----------------------------------------------------------------


def _id_routes():
    """Every GET operation of the API with a `{..._id}` path parameter (from OpenAPI, since
    include_router nests the sub-routers lazily)."""
    schema = app.openapi()["paths"]
    return sorted(
        path
        for path, operations in schema.items()
        if path.startswith("/api/v1/") and "get" in operations and "_id}" in path
    )


def test_the_route_scan_is_not_vacuous():
    assert len(_id_routes()) >= 10


def _fill(template: str, value: str) -> str:
    path = template
    for part in [p for p in template.split("/") if p.startswith("{")]:
        path = path.replace(part, value)
    return path


@pytest.mark.parametrize(
    "huge", [str(HUGE_ID), str(10**20), str(-(10**20)), "%20" + str(HUGE_ID) + "%20"]
)
def test_every_get_id_route_answers_422_json_for_an_unrepresentable_id(client, huge):
    for template in _id_routes():
        path = _fill(template, huge)
        response = client.get(path, headers=_headers())
        assert response.status_code == 422, (path, response.status_code, response.text[:120])
        assert response.headers["content-type"].startswith("application/json"), path


def test_decimal_spelling_of_an_unrepresentable_id_is_422_too(client):
    response = client.get(f"/api/v1/jobs/{HUGE_ID}.0", headers=_headers())
    assert response.status_code == 422


def test_unauthenticated_callers_still_get_401_not_a_range_error(client):
    assert client.get(f"/api/v1/jobs/{HUGE_ID}").status_code == 401
    assert client.get(f"/api/v1/jobs/{HUGE_ID}", headers={"X-API-Key": "wrong"}).status_code == 401


def test_in_range_ids_are_untouched(client):
    for value in (str(2**63 - 1), str(-(2**63)), "0000000000000000000000000001", "1"):
        status = client.get(f"/api/v1/jobs/{value}", headers=_headers()).status_code
        assert status in (404, 422), value
        if value in ("1", "0000000000000000000000000001", str(2**63 - 1)):
            assert status == 404, value  # a representable id is just "not found"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", False),
        (str(2**63 - 1), False),
        (str(2**63), True),
        (str(-(2**63)), False),
        (str(-(2**63) - 1), True),
        ("+5", False),
        ("007", False),
        (" 9223372036854775808 ", True),
        ("9223372036854775808.0", True),
        ("9_223_372_036_854_775_808", True),
        ("-9_223_372_036_854_775_809", True),
        ("9_223_372_036_854_775_807", False),
        ("1__0", False),
        ("_1", False),
        ("9223372036854775807.0", False),
        ("abc", False),
        ("1e30", False),  # not an integer literal for the endpoints either: normal 422
        ("9" * 5_000, True),
    ],
)
def test_out_of_range_classifier(value, expected):
    assert _out_of_range(value) is expected


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/jobs/{job_id}/cv-draft", {"match_id": 2**70}),
        ("/api/v1/jobs/{job_id}/bewerbung-draft", {"cv_draft_id": 2**70}),
        ("/api/v1/jobs/{job_id}/review-package", {"cv_draft_id": 2**70, "bewerbung_draft_id": 1}),
    ],
)
def test_oversized_ids_in_request_bodies_are_422(client, path, body):
    assert path in app.openapi()["paths"], path  # the route exists: no silent 404 pass
    response = client.post(_fill(path, "1"), json=body, headers=_headers())
    assert response.status_code == 422, (path, response.status_code, response.text[:120])


def test_profile_patch_with_an_oversized_expected_version_is_422(client):
    response = client.patch(
        "/api/v1/candidate-profile",
        json={"expected_profile_version": 2**70},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_dependency_only_looks_at_id_named_parameters():
    class _Request:
        path_params = {"job_id": "7"}
        query_params = {"q": "123456789012345678901234567890", "before_id": "5"}

    reject_out_of_range_ids(_Request())  # a long free-text number is not an id
    _Request.query_params = {"before_id": str(HUGE_ID)}
    with pytest.raises(Exception) as excinfo:
        reject_out_of_range_ids(_Request())
    assert getattr(excinfo.value, "status_code", None) == 422


# ---- API-03 -----------------------------------------------------------------


def _job_kwargs(**overrides):
    base = {
        "source": "bundesagentur",
        "title": "Backend Engineer",
        "company": "BoundCo",
        "url": "https://example.com/jobs/1",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "T" * 301),
        ("company", "C" * 301),
        ("location", "L" * 301),
        ("source", "S" * 101),
        ("source_reference", "R" * 65),
        ("description", "D" * 100_001),
        ("skills", ["s"] * 201),
        ("must_have_skills", ["x" * 201]),
    ],
)
def test_ingest_model_rejects_over_long_fields(field, value):
    with pytest.raises(ValidationError):
        JobIngest(**_job_kwargs(**{field: value}))


def test_the_shared_job_model_stays_unbounded_so_collectors_and_stored_rows_are_unchanged():
    assert Job(**_job_kwargs(title="T" * 5_000, description="D" * 200_000)).title == "T" * 5_000


def test_ingest_accepts_values_at_the_limit_and_long_real_descriptions():
    job = JobIngest(
        **_job_kwargs(title="T" * 300, description="word " * 19_000, skills=["s"] * 200)
    )
    assert len(job.title) == 300


def test_api_returns_422_for_an_over_long_title(client):
    response = client.post(
        "/api/v1/jobs/score", json=_job_kwargs(title="T" * 5_000), headers=_headers()
    )
    assert response.status_code == 422


def test_review_paragraph_patch_is_bounded():
    from app.models.review_package import BewerbungParagraphPatch

    BewerbungParagraphPatch(index=0, text="x" * 20_000)
    with pytest.raises(ValidationError):
        BewerbungParagraphPatch(index=0, text="x" * 20_001)


# ---- API-02 and sibling regexes: linear on hostile input ---------------------

_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from app.services.email_matching import extract_reference_tokens
from app.collectors.xing_email import _SALARY_RE
from app.agents import application_response_classifier as c
case = sys.argv[2]
if case == "url_dotless":
    extract_reference_tokens("a" * 200_000)
elif case == "url_dotted_chain":
    extract_reference_tokens("a." * 100_000)
elif case == "url_long_labels":
    extract_reference_tokens(("a" * 70 + ".") * 3_000)
elif case == "salary_digits":
    _SALARY_RE.search("1" * 200_000)
elif case == "salary_dotted_digits":
    _SALARY_RE.search("1." * 100_000)
elif case == "subject_prefix":
    c._SUBJECT_PREFIX.match("Re" + " " * 100_000 + "!")
elif case == "negation_one_clause":
    c._affirmative_match(c._TECHNICAL_MARKER, "nicht " + "technisches interview " * 20_000)
elif case == "negation_many_clauses":
    c._affirmative_match(
        c._TECHNICAL_MARKER, ("nicht " * 3 + "technisches interview. ") * 20_000
    )
"""

_CASES = [
    "url_dotless",
    "url_dotted_chain",
    "url_long_labels",
    "salary_digits",
    "salary_dotted_digits",
    "subject_prefix",
    "negation_one_clause",
    "negation_many_clauses",
]


@pytest.mark.parametrize("case", _CASES)
def test_hostile_text_is_linear_enough(case):
    subprocess.run([sys.executable, "-c", _CHILD, str(REPO), case], check=True, timeout=60)


def test_url_pattern_keeps_realistic_matches():
    from app.services.email_matching import _extract_urls

    text = (
        "Link https://www.example.co.uk/jobs/AB-123?x=1 oder karriere.firma.de/stelle/99. "
        "Bewerbung ...karriere.firma.de/jobs/AB-123 und (www.beispiel.de)."
    )
    urls = _extract_urls(text)
    assert any(u.startswith("https://www.example.co.uk/jobs/AB-123") for u in urls)
    assert any(u.startswith("karriere.firma.de/stelle/99") for u in urls)
    assert any(u.startswith("karriere.firma.de/jobs/AB-123") for u in urls)
    assert any(u.startswith("www.beispiel.de") for u in urls)
    assert _extract_urls("kein link 2026 60311 +49 170 1234567") == []


def test_salary_regex_keeps_realistic_matches():
    from app.collectors.xing_email import _SALARY_RE

    for line in ("60.000 € - 80.000 €", "Gehalt ca.60.000 € - 80.000 €", "3.500,50 €-4.200 €"):
        assert _SALARY_RE.search(line), line
    assert _SALARY_RE.search("1" * 5000) is None


def test_affirmative_match_semantics_unchanged():
    from app.agents import application_response_classifier as c

    marker = c._TECHNICAL_MARKER
    assert c._affirmative_match(marker, "Wir laden Sie zum technischen Interview ein.")
    assert c._affirmative_match(marker, "Es gibt kein technisches Interview.") is None
    assert c._affirmative_match(marker, "Kein Test. Aber ein technisches Interview folgt.")
    # first marker negated, a later marker in another clause is affirmative
    second = c._affirmative_match(
        marker, "Kein technisches Interview. Danach: technisches Interview am Montag."
    )
    assert second is not None and second.start() > 20


def test_subject_prefix_still_strips_reply_prefixes():
    from app.agents import application_response_classifier as c

    assert c._SUBJECT_PREFIX.sub("", "AW: Re[2]: WG: Ihre Bewerbung") == "Ihre Bewerbung"
    assert c._SUBJECT_PREFIX.sub("", "Re [3] : Ihre Bewerbung") == "Ihre Bewerbung"


def test_very_long_real_hostnames_still_yield_the_reference_token():
    from app.services.email_matching import extract_reference_tokens

    url = "https://eu.prod.careers.portal.jobs.tenant.region.example.co.uk/jobs/ABC123"
    assert "ABC123" in extract_reference_tokens(f"Ihre Bewerbung: {url}")


def test_salary_regex_keeps_wide_whitespace():
    from app.collectors.xing_email import _SALARY_RE

    assert _SALARY_RE.search("60.000      €  -  80.000      €")


def test_underscore_spelling_is_422_not_500(client):
    for value in ("9_223_372_036_854_775_808", "-9_223_372_036_854_775_809"):
        response = client.get(f"/api/v1/jobs/{value}", headers=_headers())
        assert response.status_code == 422, (value, response.status_code)


def test_oversized_ids_are_charged_to_the_generic_rate_limit(client, monkeypatch):
    settings = Settings(api_key=API_KEY, rate_limit_requests=2, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
    rate_limit_module._requests.clear()
    statuses = [
        client.get(f"/api/v1/jobs/{HUGE_ID}", headers=_headers()).status_code for _ in range(4)
    ]
    assert statuses[:2] == [422, 422]
    assert statuses[2:] == [429, 429]
