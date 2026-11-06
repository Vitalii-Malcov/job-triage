"""HTTP routes: health (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter
from fastapi import status as http_status
from fastapi.responses import JSONResponse

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
    and its Alembic revision is exactly the application's single head; 503
    otherwise. Read-only and bounded -- see app.services.readiness. The body
    never carries the reason, revision, or any database detail."""
    if check_readiness():
        return JSONResponse(status_code=http_status.HTTP_200_OK, content={"status": "ready"})
    return JSONResponse(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "not_ready"}
    )
