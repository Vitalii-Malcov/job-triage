"""Architecture 3: `app.db.models` and `app.api.routes` are split into domain modules
without changing their public surface."""

import subprocess
import sys
from pathlib import Path

from fastapi.routing import APIRoute

from app.db.base import Base

ROOT = Path(__file__).resolve().parents[1]
MAX_MODEL_MODULE_LINES = 700
MAX_ROUTER_MODULE_LINES = 450


def _flatten(router, prefix=""):
    for route in router.routes:
        if hasattr(route, "original_router"):
            ctx = getattr(route, "include_context", None)
            yield from _flatten(route.original_router, prefix + (getattr(ctx, "prefix", "") or ""))
        elif isinstance(route, APIRoute):
            yield prefix + route.path, route


class TestModelsPackage:
    def test_every_mapped_class_is_importable_from_the_package_root(self):
        import app.db.models as models

        missing = [
            mapper.class_.__name__
            for mapper in Base.registry.mappers
            if getattr(models, mapper.class_.__name__, None) is not mapper.class_
        ]
        assert missing == []

    def test_classes_live_in_domain_modules_not_in_the_package_root(self):
        import app.db.models as models

        for mapper in Base.registry.mappers:
            module = mapper.class_.__module__
            assert module.startswith("app.db.models."), (mapper.class_.__name__, module)
        assert models.__file__.endswith("__init__.py")

    def test_importing_only_the_package_registers_every_table(self):
        code = (
            "import os, tempfile;"
            "os.environ['DATABASE_URL']='sqlite:///'+tempfile.mkdtemp().replace(chr(92),'/')+'/x.db';"
            "os.environ['DATABASE_ROLE']='test';"
            "from app.db.base import Base; import app.db.models;"
            "print(len(Base.metadata.tables))"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert int(out) >= 47  # Alembic's env.py relies on this import registering all tables

    def test_historical_re_export_of_base_is_kept(self):
        from app.db.models import Base as exported

        assert exported is Base

    def test_no_model_module_regrows_into_a_god_file(self):
        too_big = {
            p.name: len(p.read_text(encoding="utf-8").splitlines())
            for p in (ROOT / "app" / "db" / "models").glob("*.py")
            if len(p.read_text(encoding="utf-8").splitlines()) > MAX_MODEL_MODULE_LINES
        }
        assert too_big == {}


class TestRoutersPackage:
    def test_router_keeps_its_import_path_and_serves_every_domain(self):
        from app.api.routes import router

        paths = {path for path, _ in _flatten(router)}
        for expected in (
            "/health",
            "/jobs/score",
            "/applications",
            "/candidate-profile",
            "/jobs/{job_id}/review-package",
            "/collectors/xing/run",
            "/automation/runs",
            "/gmail/sync",
            "/response-drafts/{draft_id}/send",
            "/follow-ups/{follow_up_id}/send",
        ):
            assert expected in paths, expected

    def test_router_routes_stay_a_flat_list_of_api_routes(self):
        from app.api.routes import router

        assert len(router.routes) >= 50
        assert all(isinstance(route, APIRoute) for route in router.routes)

    def test_every_route_but_the_probes_requires_the_api_key_and_a_rate_limit(self):
        from app.api.routes import router
        from app.security.auth import require_api_key

        unguarded = []
        for path, route in _flatten(router):
            calls = [dep.call for dep in route.dependant.dependencies]
            if path in ("/health", "/ready"):
                continue
            if require_api_key not in calls or not any(
                getattr(c, "__name__", "").startswith("enforce_") for c in calls
            ):
                unguarded.append(path)
        assert unguarded == []

    def test_handler_names_are_unique_across_routers(self):
        from app.api.routes import router

        names = [route.name for _path, route in _flatten(router)]
        assert len(names) == len(set(names))  # also keeps OpenAPI operation ids stable

    def test_no_router_module_regrows_into_a_god_file(self):
        too_big = {
            p.name: len(p.read_text(encoding="utf-8").splitlines())
            for p in (ROOT / "app" / "api" / "routers").glob("*.py")
            if len(p.read_text(encoding="utf-8").splitlines()) > MAX_ROUTER_MODULE_LINES
        }
        assert too_big == {}

    def test_settings_have_a_single_patch_point(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.core.config import Settings
        from app.main import app

        settings = Settings(_env_file=None, api_key="k", rate_limit_requests=1000)
        monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
        monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
        monkeypatch.setattr("app.api.routers.common.get_settings", lambda: settings)
        with TestClient(app) as client:
            assert client.get("/api/v1/health").json() == {"status": "ok"}
