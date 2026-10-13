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
# Stage 13A: --no-proxy-headers -- Uvicorn must never rewrite the client
# address from X-Forwarded-For (its default trusts 127.0.0.1 and whatever
# FORWARDED_ALLOW_IPS says, and compose passes the whole .env through). The
# application alone decides proxy trust: app/security/client_identity.py.
# --workers 1: the in-process rate limiter and the Telegram poller assume
# exactly one web worker (docs/DEPLOYMENT.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
