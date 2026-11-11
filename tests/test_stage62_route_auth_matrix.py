"""Stage 62: no route answers without credentials except the explicit public allowlist."""

import re
import secrets

import pytest
from fastapi.testclient import TestClient

PUBLIC = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("GET", "/ui/login"),
    ("POST", "/ui/login"),  # the login form itself: wrong key -> 401, cross-origin -> 403
    ("GET", "/ui/static/style.css"),
}


def _flat(routes, prefix=""):
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            ctx = route.include_context
            yield from _flat(
                route.original_router.routes, prefix + (getattr(ctx, "prefix", "") or "")
            )
        elif hasattr(route, "path"):
            yield prefix, route


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("UI_ENABLED", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app, base_url="http://localhost") as c:
        yield app, c
    get_settings.cache_clear()


def test_every_route_requires_credentials_except_allowlist(client):
    app, c = client
    leaks = []
    for prefix, route in _flat(app.routes):
        methods = (getattr(route, "methods", None) or {"GET"}) - {"HEAD", "OPTIONS"}
        path = re.sub(r"\{[^}]+\}", "1", prefix + route.path)
        for method in sorted(methods):
            if (method, prefix + route.path) in PUBLIC:
                continue
            from app.security import rate_limit

            rate_limit._auth_failure_requests.clear()
            resp = c.request(
                method, path, follow_redirects=False, json={} if method != "GET" else None
            )
            location = resp.headers.get("location", "")
            if (prefix + route.path).startswith("/api/"):
                ok = resp.status_code == 401
            else:  # /ui: every page redirects to the login form
                ok = resp.status_code == 303 and location.endswith("/ui/login")
            if not ok:
                leaks.append((resp.status_code, method, path))
    assert leaks == []


def test_schema_pages_are_not_public_by_default():
    from app.main import app

    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
