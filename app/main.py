from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.startup_diagnostics import log_startup_safety_state
from app.db.session import run_migrations_if_enabled
from app.services.telegram_bot import start_bot, stop_bot


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    settings = get_settings()
    log_startup_safety_state(settings, process="web")
    run_migrations_if_enabled()
    telegram_application = await start_bot(settings)
    try:
        yield
    finally:
        await stop_bot(telegram_application)


app = FastAPI(
    title="AI Job Search Control Center",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api/v1")
