# See docs/DOCKER.md for the design rationale behind every choice here.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Only what's needed to install and run the app — no tests/, docs/, or
# dev tooling. psycopg[binary] (the "postgres" extra) ships a prebuilt
# wheel, so no compiler or system libpq package is required here.
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --upgrade pip && pip install ".[postgres]"

COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
