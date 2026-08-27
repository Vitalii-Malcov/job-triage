# AI Job Search Control Center

AI-система для сбора, оценки и трекинга вакансий с Telegram-уведомлениями.

## Роли
- **Claude Code** — архитектура, реализация, тесты, документация.
- **Codex** — независимый reviewer: diff, баги, тесты, security и повторная проверка.

## MVP 0.2
- FastAPI API
- SQLite по умолчанию / SQLAlchemy 2.x, с возможностью перейти на PostgreSQL
- схема БД версионируется через Alembic-миграции (не `create_all()`)
- история вакансий и дедупликация по fingerprint
- persisted user profile вместо `PROFILE_SKILLS` в route
- weighted JobScorer: aliases, must-have, nice-to-have, description evidence
- Telegram best-effort notification: timeout, retry, logging; ошибка Telegram не ломает scoring API
- JSON application logs
- API-key protection и базовый rate limiting
- pytest
- Ruff + pre-commit
- GitHub Actions CI: lint + format + tests

## Следующие этапы
1. ~~PostgreSQL + Alembic migrations~~ — Alembic подключён (см. "Миграции" выше); PostgreSQL поддержан через `.[postgres]` extra, продовое использование ещё предстоит обкатать.
2. Job collectors: ~~XING alerts/email~~, ~~Bundesagentur für Arbeit~~, StepStone/Indeed where allowed, career pages — частично: Bundesagentur и XING реализованы (см. "Collectors" ниже), Indeed/StepStone/career pages ещё нет, поэтому пункт целиком не вычеркнут.
3. ~~Application status endpoints: SAVED/APPLIED/INTERVIEW/REJECTED/OFFER~~ — реализовано, см. "Application status" ниже.
4. ~~Telegram commands/control center~~ — реализовано, см. "Telegram control center" ниже.
5. Company Research Agent
6. CV/Bewerbung Agent
7. Gmail Response + Follow-up Agent

## Запуск
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Для защищённых endpoints передавайте:
```text
X-API-Key: <API_KEY from .env>
```

## Миграции

Схема БД версионируется через Alembic (`alembic/versions/`). Приложение больше
не создаёт таблицы через `Base.metadata.create_all()` — миграции обязательны.

```bash
# перед первым запуском и после pull с новыми миграциями
alembic upgrade head

# после изменения моделей в app/db/models.py
alembic revision --autogenerate -m "описание изменения"
# проверь сгенерированный файл в alembic/versions/ вручную перед коммитом

# откатить последнюю миграцию
alembic downgrade -1
```

`alembic/env.py` берёт `sqlalchemy.url` из `app.core.config.get_settings().database_url`
(переменная окружения `DATABASE_URL`) — сам `alembic.ini` не содержит connection string.

Для PostgreSQL нужен дополнительный драйвер:
```bash
pip install -e ".[postgres]"
```
и `DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname` в `.env`
(пример есть в `.env.example`).

**Прод:** перед стартом приложения обязательно выполните `alembic upgrade head`
вручную или как шаг CI/CD — приложение не мигрирует схему само по себе.

**Dev/тесты:** можно включить автоматический `alembic upgrade head` при старте
приложения, выставив `ALEMBIC_AUTO_UPGRADE=true` в `.env`. Это удобство только
для локальной разработки — в проде должно оставаться выключенным.

## Application status

Каждая вакансия (`JobRecord.status`) проходит через жизненный цикл отклика:

```
NEW -> SAVED | APPLIED | REJECTED | WITHDRAWN
SAVED -> APPLIED | REJECTED | WITHDRAWN
APPLIED -> INTERVIEW | REJECTED | WITHDRAWN
INTERVIEW -> OFFER | REJECTED | WITHDRAWN
OFFER -> REJECTED | WITHDRAWN
REJECTED -> (терминальный статус, переходов нет)
WITHDRAWN -> (терминальный статус, переходов нет)
```

Правила переходов вынесены в `app/domain/status_transitions.py` (unit-тесты —
`tests/test_status_transitions.py`, без БД и без FastAPI).

Список вакансий:
```bash
curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/api/v1/jobs?status=APPLIED&limit=20&offset=0"
```

Одна вакансия:
```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/jobs/1
```

Смена статуса:
```bash
curl -X PATCH -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"status": "APPLIED"}' \
  http://localhost:8000/api/v1/jobs/1/status
```

Недопустимый переход возвращает `409 Conflict` с описанием разрешённых
переходов из текущего статуса, например при попытке `NEW -> INTERVIEW`:
```json
{
  "detail": "Cannot transition job status from NEW to INTERVIEW. Allowed transitions from NEW: APPLIED, REJECTED, SAVED, WITHDRAWN."
}
```

Несуществующий `job_id` в `GET /jobs/{id}` и `PATCH /jobs/{id}/status`
возвращает `404 Not Found`.

## Collectors

Job collectors забирают вакансии из внешних источников и превращают их в
`Job` (`app/models/job.py`) — они **не пишут в БД и не скорят**, это делает
общий пайплайн (`JobScorer` + `upsert_job`, переиспользуемый и из
`POST /jobs/score`, и из ручного запуска коллектора).

Интерфейс — `app/collectors/base.py`:
```python
class JobCollector(ABC):
    source: str  # должен совпадать с Job.source для стабильной дедупликации

    async def fetch(self, since: datetime | None = None) -> list[Job]: ...
```
Новый источник (Indeed/StepStone/career pages — ещё не реализованы)
добавляется как отдельный класс, реализующий этот интерфейс, без изменений
в `app/collectors/bundesagentur.py` или `app/collectors/xing_email.py`.

### Bundesagentur für Arbeit (Jobsuche API)

`app/collectors/bundesagentur.py` использует публичный, но **неофициальный**
API Bundesagentur für Arbeit "Jobsuche"
(`https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs`,
референс: https://jobsuche.api.bund.dev/ и
https://github.com/bundesAPI/jobsuche-api).

**Про API-ключ:** официального self-service портала регистрации у
Bundesagentur für Arbeit нет. Сообщество использует общий client id
`jobboerse-jobsuche` как значение `X-API-Key` — это не персональный секрет,
а публично задокументированное значение (см. репозиторий выше). API
неофициальный и может измениться или перестать работать без предупреждения
— см. "Известные риски" ниже.

Переменные окружения (`.env`):
```bash
BUNDESAGENTUR_API_KEY=jobboerse-jobsuche
BUNDESAGENTUR_SEARCH_KEYWORDS=Python
BUNDESAGENTUR_SEARCH_LOCATION=Berlin
BUNDESAGENTUR_SEARCH_RADIUS_KM=25
```

Ручной запуск сбора:
```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/collectors/bundesagentur/run
```
Ответ:
```json
{"fetched": 12, "created": 9, "updated": 3, "skipped_invalid": 0, "failed": 0}
```
`failed` — сколько job'ов не прошли scoring/persist из-за неожиданной ошибки
(например сбой БД); это не то же самое, что `skipped_invalid`, которое про
сами данные, полученные от API.

Если `BUNDESAGENTUR_API_KEY` не задан — `503 Service Unavailable`. Если сам
Jobsuche API недоступен/отвечает ошибкой — `502 Bad Gateway`. Этот endpoint
защищён отдельным, более строгим rate limit (5 запросов / 5 минут на IP,
`enforce_collector_rate_limit`), а не общим лимитом `/jobs/score` — один
прогон делает несколько исходящих запросов к стороннему API и пишет в БД в
цикле, поэтому дефолтный лимит 60/60с для него слишком мягкий.

Автоматический периодический запуск (cron) — отдельная задача на будущее,
сейчас endpoint запускается только вручную.

### XING (email digest)

`app/collectors/xing_email.py` читает XING job-digest письма (отправитель
`jobs@mail.xing.com`) из почтового ящика по IMAP (read-only, App Password) —
у XING нет публичного API для поиска вакансий, поэтому единственный
практичный источник — email-дайджест, на который пользователь подписан.

**⚠️ Trecking-ссылки внутри писем НИКОГДА не открываются кодом.** Каждая
вакансия в письме — это персональный трекинг-редирект вида
`=> https://www.xing.com/m/xxxxx`. Переход по такой ссылке (GET/HEAD/любой
HTTP-запрос) отправляет рекрутёру уведомление "кандидат просмотрел вакансию"
— это реальное действие с побочным эффектом на третью сторону, а не
безобидное чтение. Коллектор сохраняет такую ссылку как есть, строкой, в
`Job.url` — только для справки/аудита человеком, который сам решит, открывать
её или нет. В `xing_email.py` нет ни одного HTTP-клиента (`httpx`, `requests`,
`aiohttp`, `urllib`) — это проверяется тестом
`tests/test_collectors_xing_email.py::test_module_never_imports_an_http_client`.
Подробности — в module docstring `app/collectors/xing_email.py` и в
`CLAUDE.md` (Implementation rules).

Обработанные письма отслеживаются по `Message-ID` в отдельной таблице
`processed_email_messages` (не по флагу "прочитано" — коллектор не мутирует
чужой почтовый ящик, `SELECT` всегда read-only).

Как получить App Password для Gmail: включите двухфакторную аутентификацию,
затем создайте пароль приложения на
https://support.google.com/accounts/answer/185833 — это НЕ основной пароль
от аккаунта, его можно отозвать отдельно в любой момент.

Переменные окружения (`.env`):
```bash
XING_MAILBOX_IMAP_HOST=imap.gmail.com
XING_MAILBOX_IMAP_PORT=993
XING_MAILBOX_USERNAME=you@gmail.com
XING_MAILBOX_APP_PASSWORD=xxxx xxxx xxxx xxxx
XING_LOOKBACK_DAYS=7
```

Ручной запуск сбора:
```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/collectors/xing/run
```
Ответ — тот же контракт, что у Bundesagentur endpoint'а:
```json
{"fetched": 5, "created": 4, "updated": 1, "skipped_invalid": 0, "failed": 0}
```

Если `XING_MAILBOX_USERNAME`/`XING_MAILBOX_APP_PASSWORD` не заданы —
`503 Service Unavailable`. Если IMAP-логин или соединение не удались —
`502 Bad Gateway`. Отдельный, более строгий rate limit (3 запроса / 10 минут
на IP, `enforce_xing_rate_limit`) — частые неудачные IMAP-логины рискуют
включить защиту от подозрительной активности у почтового провайдера
(например, временную блокировку аккаунта в Gmail), что хуже, чем просто
упереться в лимит обычного API.

**Известный компромисс v1:** используется полный read-доступ к
пользовательскому почтовому ящику через App Password (не изолированный
ящик только для job-алертов). Осознанное решение для первой итерации —
изоляция отдельным ящиком вынесена в техдолг на будущее.

## Telegram control center

Двусторонний бот-контроллер поверх `app/services/telegram_bot.py` — читает и
меняет те же вакансии, что и HTTP API, через те же репозитории и ту же
`app/domain/status_transitions.py` логику. Использует long polling
(`getUpdates`), а не webhook — у проекта нет публичного HTTPS-эндпоинта, а
поднимать его только ради вебхука несоразмерно личному проекту. Бот
встроен в тот же процесс FastAPI как asyncio-задача в lifespan
(`app/main.py`), отдельного процесса/скрипта не требует.

Использует те же переменные окружения, что и одностороннее
best-effort-уведомление (`app/services/telegram.py`) — `TELEGRAM_BOT_TOKEN`
и `TELEGRAM_CHAT_ID`, новых не нужно. Если `TELEGRAM_BOT_TOKEN` не задан —
бот просто не стартует (info-лог), остальной API продолжает работать как
обычно.

**Безопасность:** бот отвечает только чату с `chat_id`, совпадающим с
`TELEGRAM_CHAT_ID`. Сообщение от любого другого `chat_id` полностью
игнорируется — ни ответа, ни любого подтверждения, что бот вообще
существует — только `logger.warning` на сервере с этим `chat_id` и текстом
сообщения, для видимости попыток постороннего доступа. В БД такие попытки
не пишутся.

Команды:
```text
/start, /help              — список команд
/jobs [status]              — список вакансий, опционально фильтр по статусу
/job <id>                   — детали вакансии
/status <id> <new_status>   — сменить статус (валидация — та же, что у
                               PATCH /jobs/{id}/status; недопустимый переход
                               возвращает тот же текст ошибки, что и HTTP API)
/run bundesagentur           — запустить сбор Bundesagentur
/run xing                    — запустить сбор XING
```

`/run bundesagentur` и `/run xing` вызывают ту же внутреннюю функцию, что и
`POST /collectors/{name}/run` (`app/api/routes.py`'s `_run_bundesagentur` /
`_run_xing`) — логика сбора и персистентности вакансий существует в одном
месте, а не дублируется между HTTP API и ботом.

## Проверки
```bash
pytest -q
ruff check .
ruff format --check .
pre-commit install
```
