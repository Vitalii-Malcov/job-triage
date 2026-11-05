"""HTTP routes: automation (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.api.routers.common import MAX_OFFSET, _current_gmail_account_key
from app.db.automation_repository import (
    AUTOMATION_RUN_LIST_DEFAULT_LIMIT,
    AUTOMATION_RUN_LIST_MAX_LIMIT,
    get_run_by_id,
    list_runs,
    to_automation_run,
)
from app.db.session import get_db
from app.models.automation import AutomationRun
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_automation_run_rate_limit,
    enforce_rate_limit,
)
from app.services.automation import (
    AutomationRunAlreadyInProgressError,
    AutomationRunLeaseLostError,
    run_automation_cycle,
)
from app.services.offload import run_off_loop

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.post(
    "/automation/runs",
    response_model=AutomationRun,
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key), Depends(enforce_automation_run_rate_limit)],
)
async def start_automation_run(db: Session = Depends(get_db)) -> AutomationRun:
    """Stage 8A: start exactly one synchronous, manually-triggered
    job-search cycle — coordinates the EXISTING Bundesagentur + XING
    collector runs (see app.services.automation's module docstring for
    why no new collection/scoring/dedup logic exists here). Awaits the
    full cycle before responding; no background task, no scheduler.

    Fails closed (409) if a run for this account is already RUNNING —
    see `AutomationRunRecord`'s docstring for the DB-enforced partial
    unique index this relies on.
    """
    settings = common.get_settings()
    account_key = _current_gmail_account_key(settings)
    try:
        run = await run_off_loop(
            lambda: run_automation_cycle(db, account_key=account_key, settings=settings)
        )
    except AutomationRunAlreadyInProgressError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AutomationRunLeaseLostError as exc:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return to_automation_run(run)


@router.get(
    "/automation/runs/{run_id}",
    response_model=AutomationRun,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_automation_run(run_id: int, db: Session = Depends(get_db)) -> AutomationRun:
    """Pure read of an already-persisted run — never starts, retries, or
    otherwise mutates one (mirrors GET /follow-ups/{id} not triggering
    evaluation)."""
    account_key = _current_gmail_account_key(common.get_settings())
    record = get_run_by_id(db, account_key, run_id)
    if record is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Automation run not found"
        )
    return to_automation_run(record)


@router.get(
    "/automation/runs",
    response_model=list[AutomationRun],
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def list_automation_runs(
    limit: int = Query(
        default=AUTOMATION_RUN_LIST_DEFAULT_LIMIT, ge=1, le=AUTOMATION_RUN_LIST_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
) -> list[AutomationRun]:
    account_key = _current_gmail_account_key(common.get_settings())
    records = list_runs(db, account_key, limit=limit, offset=offset)
    return [to_automation_run(record) for record in records]
