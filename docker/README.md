# Docker quick reference

Full design rationale: `docs/DOCKER.md`. Full deployment architecture: `docs/DEPLOYMENT.md`. Operator procedures: `docs/RUNBOOK.md`.

## First-time setup

```bash
cp .env.example .env
# edit .env: set API_KEY, POSTGRES_PASSWORD, and any collector/provider
# credentials you actually want to enable. Never commit .env.

docker compose build
docker compose up -d db
docker compose run --rm web alembic upgrade head   # explicit, always required before first boot
docker compose up -d web
```

## Optional scheduler (automation cycle / Telegram digest)

Only needed if `AUTOMATION_SCHEDULER_ENABLED=true` or `TELEGRAM_DAILY_DIGEST_ENABLED=true` in `.env`:

```bash
docker compose --profile scheduler up -d scheduler
```

Never run more than one `scheduler` replica (see `docs/DEPLOYMENT.md`).

## After pulling new migrations

```bash
docker compose run --rm web alembic upgrade head
docker compose up -d web
```

## Logs

```bash
docker compose logs -f web
docker compose logs -f scheduler
```

## Stop

```bash
docker compose stop        # keeps the pgdata volume
docker compose down        # same, removes containers but not the volume
docker compose down -v     # DESTROYS the database volume -- only for a full reset
```

## Config validation without starting anything

```bash
docker compose config
```
