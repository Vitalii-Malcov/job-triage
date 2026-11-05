"""HTTP routes: applications (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers.common import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, MAX_OFFSET
from app.db.session import get_db
from app.models.application_lifecycle import (
    Application,
    ApplicationEvent,
    ApplicationLifecycleStatus,
)
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_rate_limit,
)
from app.services.application_tracker import ApplicationNotFoundError, ApplicationTracker

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.get(
    "/applications",
    response_model=list[Application],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_applications(
    status: ApplicationLifecycleStatus | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    after_id: int | None = Query(default=None, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[Application]:
    """Stage 15: read-only application lifecycle list, one keyset page in id
    order (`after_id` = last id of the previous page; a full page means more
    may exist). No mutation endpoint exists; status changes go through the
    operator CLI."""
    return ApplicationTracker().list_applications(
        db, (status,) if status else None, limit=limit, after_id=after_id
    )


@router.get(
    "/applications/{application_id}",
    response_model=Application,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_application(application_id: int, db: Session = Depends(get_db)) -> Application:
    try:
        return ApplicationTracker().get_application(db, application_id)
    except ApplicationNotFoundError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Application not found"
        ) from None


@router.get(
    "/applications/{application_id}/history",
    response_model=list[ApplicationEvent],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_application_history(
    application_id: int, db: Session = Depends(get_db)
) -> list[ApplicationEvent]:
    try:
        return ApplicationTracker().get_application_history(db, application_id)
    except ApplicationNotFoundError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Application not found"
        ) from None
