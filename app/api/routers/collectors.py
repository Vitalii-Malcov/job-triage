"""HTTP routes: collectors (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.db.session import get_db
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_collector_rate_limit,
    enforce_xing_rate_limit,
)
from app.services.collector_exclusion import (
    CollectorRunInProgressError,
    CollectorRunLeaseLostError,
    run_collector_exclusively,
)
from app.services.collector_runner import (
    CollectorError,
    CollectorNotConfiguredError,
    run_bundesagentur,
    run_xing,
)

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.post(
    "/collectors/bundesagentur/run",
    dependencies=[Depends(require_api_key), Depends(enforce_collector_rate_limit)],
)
async def run_bundesagentur_collector(db: Session = Depends(get_db)) -> dict[str, int]:
    """fetch + score + persist logic lives in
    `app.services.collector_runner.run_bundesagentur` — shared with the
    Telegram control center's `/run bundesagentur` command and Stage 8A's
    automation orchestrator. This endpoint only translates that
    function's exceptions into HTTP status codes.
    """
    settings = common.get_settings()
    try:
        return await run_collector_exclusively(db, settings, "bundesagentur", run_bundesagentur)
    except CollectorRunInProgressError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="A collector or automation run is already in progress",
        ) from exc
    except CollectorRunLeaseLostError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="The run lost its exclusivity lease and was not recorded as complete",
        ) from exc
    except CollectorNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CollectorError as exc:
        # this branch used to call
        # `logger.exception(...)`, which logs the FULL traceback
        # (including any chained upstream exception's own str()) --
        # exactly the leakage class already identified and fixed for the
        # XING branch below.
        # A raw httpx/requests connection error's `__cause__` can carry
        # host/response-body text; `exc`'s own message is already
        # sanitized by the collector before it reaches here, so only
        # `type(exc).__name__` is logged, matching the XING sibling and
        # every other CollectorError log site in this project.
        #
        # BOUND-011b (api-boundaries hardening r1, privacy second-pass):
        # the "exc's own message is already sanitized" assumption below
        # (for the `detail=f"...: {exc}"` line) was actually FALSE for
        # one path until this same pass: a non-JSON upstream response
        # (WAF/proxy error page) embedded up to 100 raw chars of that
        # page's body directly into `BundesagenturAPIError`'s message --
        # see app/collectors/bundesagentur.py's JSONDecodeError handler,
        # now fixed to log that snippet server-side only and keep the
        # client-visible exception message snippet-free.
        logger.warning("bundesagentur_collector_run_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"Bundesagentur Jobsuche API request failed: {exc}",
        ) from exc


@router.post(
    "/collectors/xing/run",
    dependencies=[Depends(require_api_key), Depends(enforce_xing_rate_limit)],
)
async def run_xing_collector(db: Session = Depends(get_db)) -> dict[str, int]:
    """see `run_bundesagentur_collector` above — same split,
    `app.services.collector_runner.run_xing` owns the actual logic.
    """
    settings = common.get_settings()
    try:
        return await run_collector_exclusively(db, settings, "xing", run_xing)
    except CollectorRunInProgressError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="A collector or automation run is already in progress",
        ) from exc
    except CollectorRunLeaseLostError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="The run lost its exclusivity lease and was not recorded as complete",
        ) from exc
    except CollectorNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CollectorError as exc:
        # Codex gate follow-up (Astra R4B, NEW-003: XING log leakage):
        # `logger.exception(...)` implies `exc_info=True` -- it logs the
        # FULL traceback, including "the above exception was the direct
        # cause of the following exception" and the ORIGINAL chained
        # exception's own str() (see app.collectors.xing_email's AUD-005
        # comments: a raw OSError/imaplib.IMAP4.error `__cause__` here can
        # carry host/port/server-controlled text). `exc`'s OWN message is
        # already deliberately sanitized by the collector before it ever
        # reaches here -- the `detail=f"...: {exc}"` below is safe -- but
        # `logger.exception` would print that PLUS everything the
        # sanitization was meant to keep out of logs. `type(exc).__name__`
        # alone matches every other CollectorError log site in this
        # project (see app.services.automation's S8A-004 convention).
        logger.warning("xing_collector_run_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"XING mailbox collector request failed: {exc}",
        ) from exc
