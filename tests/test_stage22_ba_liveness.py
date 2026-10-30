"""Stage 22 P0-2: BA liveness -- a 404 is CLOSED only for a valid full reference."""

import asyncio
import base64
import hashlib
import json
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.db.repositories import upsert_job
from app.models.job import Job, JobScore
from app.services.ba_liveness import Liveness, check_reference, classify_response, detail_url
from app.services.readiness import expected_alembic_heads
from scripts import ba_liveness as cli

BA = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
FULL = ["12265-444700_JB5270016-S", "13410-k13572.15943-S", "13319-896770/1_626382LS-S"]
# What the 2026-10-30 research queried instead (URL path tokens): the class of
# the 478 false "closed" results. They are never requested now.
TRUNCATED = ["12265-444700", "13410-k13572", "13319-896770", "P254924", "1_626382LS-S"]


def _no_sleep(_seconds):
    async def done():
        return None

    return done()


def _run(handler, reference):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_reference(client, "key", reference, sleep=_no_sleep)

    return asyncio.run(go())


def _served(reference):
    return lambda request: httpx.Response(200, json={"referenznummer": reference, "titel": "x"})


@pytest.mark.parametrize("reference", FULL)
def test_200_is_active_and_404_is_closed_for_a_full_reference(reference):
    assert _run(_served(reference), reference).state is Liveness.ACTIVE
    result = _run(lambda r: httpx.Response(404), reference)
    assert result.state is Liveness.CLOSED and result.http_status == 404


@pytest.mark.parametrize("reference", TRUNCATED + ["", None, "10001-12 34-S"])
def test_a_truncated_or_missing_reference_is_never_requested_never_closed(reference):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    result = _run(handler, reference)
    assert result.state is Liveness.INVALID_REFERENCE
    assert calls == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 408])
def test_temporary_errors_are_retried_once_then_source_error(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "120"})

    result = _run(handler, FULL[0])
    assert result.state is Liveness.SOURCE_ERROR and result.http_status == status
    assert len(calls) == 2


def test_a_temporary_error_followed_by_success_is_active():
    answers = iter([httpx.Response(503), httpx.Response(200, json={})])
    assert _run(lambda r: next(answers), FULL[0]).state is Liveness.ACTIVE


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.RemoteProtocolError("x")]
)
def test_network_failures_are_source_error_not_closed(error):
    def handler(request):
        raise error

    result = _run(handler, FULL[1])
    assert result.state is Liveness.SOURCE_ERROR and result.http_status is None


@pytest.mark.parametrize(
    ("response", "state"),
    [
        (httpx.Response(200, text="<html>WAF</html>"), Liveness.SOURCE_ERROR),
        (httpx.Response(200, json=[1, 2]), Liveness.SOURCE_ERROR),
        (httpx.Response(200, json={"referenznummer": "10001-9-S"}), Liveness.UNKNOWN),
        (httpx.Response(401), Liveness.SOURCE_ERROR),
        (httpx.Response(403), Liveness.SOURCE_ERROR),
        (httpx.Response(400), Liveness.UNKNOWN),
        (httpx.Response(410), Liveness.UNKNOWN),
    ],
)
def test_other_answers_never_claim_closed(response, state):
    assert classify_response(FULL[0], response).state is state


def test_the_request_uses_the_full_reference_base64_encoded():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    _run(handler, FULL[2])
    encoded = unquote(str(seen[0].url).rsplit("/", 1)[1])
    assert base64.b64decode(encoded).decode() == FULL[2]
    assert seen[0].headers["X-API-Key"] == "key"
    assert str(seen[0].url) == detail_url(FULL[2])


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    session = sessionmaker(engine)()
    score = JobScore(score=1, recommendation="SKIP")
    jobs = [
        (FULL[0], "https://www.get-in-it.de/p1"),  # stored reference, external URL
        (None, BA + FULL[2]),  # derivable from the BA URL
        (None, "https://www.yourfirm.de/j/2"),  # no reference at all
    ]
    for n, (reference, url) in enumerate(jobs):
        job = Job(source="bundesagentur", title=f"T{n}", company="C", url=url)
        job.source_reference = reference
        upsert_job(session, job, score)
    session.close()
    engine.dispose()
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_plan_selects_references_without_any_request(db_path, capsys):
    assert cli.run(["--database", str(db_path), "--plan"]) == 0
    out = capsys.readouterr().out
    assert FULL[0] in out and FULL[2] in out and "INVALID_REFERENCE" in out
    assert "PLAN: 3 jobs" in out


def test_check_all_writes_only_the_output_file_and_resumes(db_path, tmp_path):
    before = _sha(db_path)
    answers = {FULL[0]: 200, FULL[2]: 503}

    def handler(request):
        reference = base64.b64decode(unquote(str(request.url).rsplit("/", 1)[1])).decode()
        return httpx.Response(answers[reference], json={})

    async def go(output):
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        with sessionmaker(engine)() as session:
            jobs = cli.select_jobs(session, [], 10)
        engine.dispose()
        jobs = [j for j in jobs if j[0] not in cli.finished_job_ids(output)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cli.check_all(
                jobs, api_key="k", rate=1.0, output=output, client=client, sleep=_no_sleep
            )

    output = tmp_path / "live.jsonl"
    counts = asyncio.run(go(output))
    assert counts == {"ACTIVE": 1, "SOURCE_ERROR": 1, "INVALID_REFERENCE": 1}
    assert _sha(db_path) == before
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert {r["state"] for r in rows} == {"ACTIVE", "SOURCE_ERROR", "INVALID_REFERENCE"}

    answers[FULL[2]] = 404  # the source recovers: only the SOURCE_ERROR job is retried
    counts = asyncio.run(go(output))
    assert counts == {"CLOSED": 1}


@pytest.mark.parametrize(
    "args",
    [["--limit", "0"], ["--rate", "0.1"], ["--output", "x.db"], ["--limit", "99999"]],
)
def test_cli_refuses_unsafe_options(db_path, args):
    assert cli.run(["--database", str(db_path), "--plan", *args]) == 2


def test_cli_refuses_without_an_api_key(db_path, monkeypatch, capsys):
    monkeypatch.setenv("BUNDESAGENTUR_API_KEY", "")
    assert cli.run(["--database", str(db_path), "--limit", "1"]) == 2
    assert "BUNDESAGENTUR_API_KEY" in capsys.readouterr().err


# --- Codex round 1 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "answers",
    [
        [httpx.Response(200, text="<html>WAF</html>"), httpx.Response(200, json={})],
        [httpx.Response(200, json=[1]), httpx.Response(200, json={})],
    ],
)
def test_every_temporary_source_error_is_retried(answers):
    calls = []

    def handler(request):
        calls.append(request)
        return answers[len(calls) - 1]

    assert _run(handler, FULL[0]).state is Liveness.ACTIVE
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    result = _run(handler, FULL[0])
    assert result.state is Liveness.SOURCE_ERROR and result.reason == "key rejected"
    assert len(calls) == 1


class _FakeTime:
    def __init__(self):
        self.now = 100.0

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def test_pacing_covers_every_attempt_including_retries():
    fake = _FakeTime()
    times = []
    answers = iter(
        [httpx.Response(503), httpx.Response(200, json={}), httpx.Response(200, json={})]
    )

    def handler(request):
        times.append(fake.now)
        return next(answers)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cli.check_all(
                [(1, FULL[0]), (2, FULL[1])],
                api_key="k",
                rate=5.0,
                output=None,
                client=client,
                sleep=fake.sleep,
                clock=fake.clock,
            )

    counts = asyncio.run(go())
    assert counts == {"ACTIVE": 2}
    assert len(times) == 3
    assert all(later - earlier >= 5.0 for earlier, later in zip(times, times[1:], strict=False))


def test_a_rejected_key_stops_the_run(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cli.check_all(
                [(1, FULL[0]), (2, FULL[1]), (3, FULL[2])],
                api_key="k",
                rate=1.0,
                output=None,
                client=client,
                sleep=_no_sleep,
            )

    counts = asyncio.run(go())
    assert counts == {"SOURCE_ERROR": 1} and len(calls) == 1
    assert "STOPPED" in capsys.readouterr().out
