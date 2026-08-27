from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.logging import configure_logging
from app.db.session import run_migrations_if_enabled


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    run_migrations_if_enabled()
    yield


app = FastAPI(
    title="AI Job Search Control Center",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api/v1")
