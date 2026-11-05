"""Architecture 3: every fixed-quota limiter, table-driven (one behaviour, 17 instances)."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.security import rate_limit as rl

# name of the enforce_* dependency, its limiter, bucket alias, and the two module constants
QUOTAS = [
    ("collector", "COLLECTOR_RATE_LIMIT"),
    ("xing", "XING_RATE_LIMIT"),
    ("company_research", "COMPANY_RESEARCH_RATE_LIMIT"),
    ("match", "MATCH_RATE_LIMIT"),
    ("cv_draft", "CV_DRAFT_RATE_LIMIT"),
    ("bewerbung", "BEWERBUNG_RATE_LIMIT"),
    ("review_write", "REVIEW_WRITE_RATE_LIMIT"),
    ("gmail", "GMAIL_RATE_LIMIT"),
    ("gmail_analysis", "GMAIL_ANALYSIS_RATE_LIMIT"),
    ("response_draft", "RESPONSE_DRAFT_RATE_LIMIT"),
    ("response_draft_decision", "RESPONSE_DRAFT_DECISION_RATE_LIMIT"),
    ("response_draft_send", "RESPONSE_DRAFT_SEND_RATE_LIMIT"),
    ("follow_up_evaluate", "FOLLOW_UP_EVALUATE_RATE_LIMIT"),
    ("follow_up_decision", "FOLLOW_UP_DECISION_RATE_LIMIT"),
    ("follow_up_send", "FOLLOW_UP_SEND_RATE_LIMIT"),
    ("automation_run", "AUTOMATION_RUN_RATE_LIMIT"),
    ("ui_login", "UI_LOGIN_RATE_LIMIT"),
]


def _request(host: str = "10.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={})


@pytest.fixture(autouse=True)
def _clean_buckets():
    limiters = [getattr(rl, f"_{name}_limiter") for name, _ in QUOTAS]
    limiters.append(rl._generic_limiter)
    for limiter in limiters:
        limiter.buckets.clear()
    yield
    for limiter in limiters:
        limiter.buckets.clear()


@pytest.mark.parametrize(("name", "prefix"), QUOTAS)
class TestEachQuota:
    def _parts(self, name, prefix):
        enforce = getattr(rl, f"enforce_{name}_rate_limit")
        return (
            enforce,
            getattr(rl, f"_{name}_limiter"),
            getattr(rl, f"{prefix}_REQUESTS"),
            getattr(rl, f"{prefix}_WINDOW_SECONDS"),
        )

    def test_allows_exactly_the_quota_then_refuses_with_429_and_its_own_detail(self, name, prefix):
        enforce, limiter, limit, _window = self._parts(name, prefix)
        for _ in range(limit):
            enforce(_request())
        with pytest.raises(HTTPException) as exc:
            enforce(_request())
        assert exc.value.status_code == 429
        assert exc.value.detail == limiter._detail
        assert exc.value.detail != "Rate limit exceeded"  # not the generic bucket's text

    def test_quota_is_per_client(self, name, prefix):
        enforce, _limiter, limit, _window = self._parts(name, prefix)
        for _ in range(limit):
            enforce(_request("10.0.0.1"))
        enforce(_request("10.0.0.2"))  # another client is unaffected

    def test_the_limit_constants_are_read_at_call_time(self, name, prefix, monkeypatch):
        enforce, _limiter, _limit, _window = self._parts(name, prefix)
        monkeypatch.setattr(rl, f"{prefix}_REQUESTS", 1)
        enforce(_request())
        with pytest.raises(HTTPException):
            enforce(_request())

    def test_window_expiry_frees_the_quota(self, name, prefix, monkeypatch):
        enforce, _limiter, limit, window = self._parts(name, prefix)
        for _ in range(limit):
            enforce(_request())
        real = rl.time.monotonic
        monkeypatch.setattr(rl.time, "monotonic", lambda: real() + window + 1)
        enforce(_request())

    def test_dependency_keeps_a_stable_enforce_name(self, name, prefix):
        enforce, *_ = self._parts(name, prefix)
        assert enforce.__name__ == f"enforce_{name}_rate_limit"


def test_buckets_are_independent_between_quotas():
    rl.enforce_collector_rate_limit(_request())
    for _ in range(rl.XING_RATE_LIMIT_REQUESTS):
        rl.enforce_xing_rate_limit(_request())
    with pytest.raises(HTTPException):
        rl.enforce_xing_rate_limit(_request())
    rl.enforce_collector_rate_limit(_request())  # untouched by the xing bucket


def test_every_bucket_alias_is_the_limiters_own_dict():
    for name, _prefix in QUOTAS:
        if name == "ui_login":
            continue  # no historical alias for the login bucket
        assert getattr(rl, f"_{name}_requests") is getattr(rl, f"_{name}_limiter").buckets


def test_trusted_proxy_identity_is_still_honoured_by_a_fixed_quota(monkeypatch):
    from app.core.config import Settings

    settings = Settings(_env_file=None, trusted_proxy_ips="10.0.0.9")
    monkeypatch.setattr(rl, "get_settings", lambda: settings)

    def via_proxy(real_client: str):
        return SimpleNamespace(
            client=SimpleNamespace(host="10.0.0.9"), headers={"x-real-ip": real_client}
        )

    class _Headers(dict):
        def getlist(self, key):
            return [self[key]] if key in self else []

    for _ in range(rl.XING_RATE_LIMIT_REQUESTS):
        request = via_proxy("203.0.113.5")
        request.headers = _Headers(request.headers)
        rl.enforce_xing_rate_limit(request)
    other = via_proxy("203.0.113.6")
    other.headers = _Headers(other.headers)
    rl.enforce_xing_rate_limit(other)  # a different real client behind the proxy
    again = via_proxy("203.0.113.5")
    again.headers = _Headers(again.headers)
    with pytest.raises(HTTPException):
        rl.enforce_xing_rate_limit(again)
