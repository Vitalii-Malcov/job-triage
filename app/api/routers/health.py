"""HTTP routes: health (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter
from fastapi import status as http_status
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.component_health import components_report, telegram_ready
from app.services.readiness import check_readiness

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Process liveness only -- never touches the database (see /ready)."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> JSONResponse:
    """Stage 13A operational readiness: 200 only when the database answers
    and its Alembic revision is exactly the application's single head, and
    the Telegram bot is running whenever a bot token is configured (a
    deliberately disabled bot never blocks readiness); 503 otherwise.
    Read-only and bounded -- see app.services.readiness. The body never
    carries the reason, revision, or any database detail."""
    if check_readiness() and telegram_ready():
        return JSONResponse(status_code=http_status.HTTP_200_OK, content={"status": "ready"})
    return JSONResponse(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "not_ready"}
    )


@router.get("/health/components")
def health_components() -> JSONResponse:
    """Coarse per-component states for an operator (OPS-02). Always 200 -- readiness
    stays the pass/fail signal. States only: no exception text, keys or data."""
    database_ready = check_readiness()
    db = SessionLocal() if database_ready else None
    try:
        report = components_report(db, get_settings(), database_ready)
    finally:
        if db is not None:
            db.close()
    return JSONResponse(status_code=http_status.HTTP_200_OK, content=report)
