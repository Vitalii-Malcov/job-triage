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
5. ~~Company Research Agent~~ — реализовано, см. "Company Research Agent" ниже.
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
/research <id>                — company research по вакансии (кэш или новый прогон)
```

`/run bundesagentur` и `/run xing` вызывают ту же внутреннюю функцию, что и
`POST /collectors/{name}/run` (`app/api/routes.py`'s `_run_bundesagentur` /
`_run_xing`) — логика сбора и персистентности вакансий существует в одном
месте, а не дублируется между HTTP API и ботом. `/research <id>` аналогично
переиспользует `_run_company_research` / `CompanyResearchService` — см. ниже.

## Company Research Agent

После того как вакансия сохранена, можно собрать структурированную,
evidence-first информацию о компании-работодателе — для будущего
CV/Bewerbung-агента, подготовки к интервью и решения пользователя.
Read-only: никаких Bewerbung, писем рекрутёрам или изменений статуса
вакансии от имени агента.

**v1 не делает НИ ОДНОГО исходящего сетевого запроса.** Более ранняя
версия включала опциональный website-fetch (SSRF-checked GET домашней
страницы компании). Независимый review (Codex) указал, что схема
`socket.getaddrinfo() → validate IP → httpx делает свой отдельный DNS
lookup` оставляет неустранимое DNS rebinding / TOCTOU окно — провалидированный
IP не гарантированно тот же, к которому реально подключится HTTP-клиент.
Вместо того чтобы городить pinned-IP TLS transport, website-fetch был
**полностью удалён** (`app/providers/url_safety.py` больше не существует).
Company Research v1 = только evidence из уже сохранённых данных вакансии.
Полноценный сетевой Company Web Research Provider — отдельный будущий этап,
требующий осознанно выбранной safe egress-архитектуры.

**Модель данных.** Отдельная таблица `company_research`
(`app/db/models.py`'s `CompanyResearchRecord`) — `jobs` не тронута и не
превращена в "большую таблицу метаданных о компании". Identity — единое,
**DB-enforced-unique** поле `identity_key` (`domain:<домен>`, если домен
известен, иначе `name:<нормализованное имя>`); разрешение (какая запись
соответствует новому job'у) делается в коде приложения
(`app/db/repositories.py`'s `get_company_research_by_identity`), но
уникальность — на уровне БД (`identity_key` UNIQUE), а не только
SELECT-then-INSERT. Гонки при конкурентном создании (`IntegrityError` →
rollback → reload canonical) и конкурентном refresh (optimistic `version`
column, конфликтующий UPDATE отбрасывает свой устаревший результат вместо
затирания более нового) обработаны явно — см. тесты
`tests/test_company_research_repository.py`.

**`Job.url` никогда не становится company identity.** URL вакансии — это
URL объявления (часто на job board/ATS: Lever, Greenhouse, сам источник),
а не сайт компании-работодателя. Более ранняя версия строила "domain hint"
из `Job.url` с blacklist'ом нескольких известных ATS-хостов; review
справедливо указал, что это ненадёжный identity-сигнал (риск объединить две
разные компании, использующие один и тот же ATS). Убрано полностью, не
пропатчено более длинным blacklist'ом — v1 использует только
`normalized_company_name` идентичность.

**Evidence-first, без company-level домыслов.** Данные одной вакансии
(локация, упомянутые в описании технологии) **не** повышаются до
company-level фактов (`headquarters`, `technologies`, ...) — локация одной
вакансии не обязательно штаб-квартира, а упомянутые в одном объявлении
навыки не обязательно весь техстек компании. Такие company-level поля
остаются `None`/`[]` с явным `UNKNOWN` evidence; вместо этого сохраняются
квалифицированные, привязанные к конкретной вакансии `relevant_facts`
("This vacancy is located in Frankfurt.", не "Company headquarters:
Frankfurt."). Каждый непустой факт в `evidence` помечен `FACT`
(подтверждено источником), `INFERENCE` (логический вывод) или `UNKNOWN`
(явно не определено) — см. `app/models/company_research.py`.

**Provider abstraction.** `app/providers/base.py`'s `CompanyResearchProvider`
— интерфейс, параллельный `app.collectors.base.JobCollector`. Единственная
реализация v1 — `JobDataCompanyResearchProvider`
(`app/providers/job_data_provider.py`): строит FACT/INFERENCE evidence
только из уже собранных данных вакансии, без единого сетевого запроса, и
поэтому не может честно вернуть `research_status=COMPLETE` — только
`PARTIAL` (нет в v1 такого статуса, как "полное" исследование компании по
одному объявлению) или `FAILED`.

**XING правило соблюдено и здесь:** вакансии с `source="xing"` никогда не
инициируют ничего сетевого, ни напрямую, ни через какую-либо эвристику —
см. `app/collectors/xing_email.py` про персональные tracking-редиректы
(v1 в любом случае не делает сетевых запросов вообще, но проверка осталась
как defense in depth, см. `tests/test_company_research_network_safety.py`).

**Кэш, TTL, attempt-метаданные.** `COMPANY_RESEARCH_TTL_HOURS` (по
умолчанию 720 = 30 дней) определяет, когда закэшированный research
считается свежим. `FAILED`-запись никогда не считается свежей — следующий
вызов автоматически повторит попытку без ручного `force_refresh`.
Неудачный refresh **не уничтожает** предыдущий хороший research: если он
уже есть, его содержимое (`research_status`/`researched_at`/`confidence`/
`evidence`/...) остаётся нетронутым, меняются только отдельные
attempt-поля (`last_attempt_at`, `last_attempt_status`,
`last_error` — короткое, санитизированное сообщение, не трейсбек) — см.
`app/db/repositories.py`'s `record_failed_attempt`.

**Явный refresh outcome.** `POST` возвращает не голый research-объект, а
`CompanyResearchRunResponse` — `research`, `refresh_attempted`,
`refresh_succeeded`, `served_stale`, `error`:
- cache hit: `refresh_attempted=false`.
- успешный refresh: `refresh_attempted=true`, `refresh_succeeded=true`.
- refresh не удался, но есть старый хороший результат: `HTTP 200`,
  `served_stale=true`, `research` = старые данные, `error` заполнен.
- первая попытка вообще без предыдущих данных: `HTTP 502`, `research=null` —
  притворяться успехом нечем.

**HTTP API:**
```bash
# Запустить (или переиспользовать кэш)
curl -X POST -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/jobs/{job_id}/research
# Форсировать обновление
curl -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"force_refresh": true}' \
  http://localhost:8000/api/v1/jobs/{job_id}/research
# Прочитать закэшированный результат (без сети, 404 если ещё не собран)
curl -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/jobs/{job_id}/research
```
`404` — вакансия не найдена (`POST`/`GET`) или research ещё не запускался
(`GET`). `422` — у job'а нет пригодного имени компании (пусто/только
пробелы после нормализации). `502` — первая попытка полностью провалилась
и показать нечего. `503` — provider требует конфигурацию, которой нет
(сегодня недостижимо для дефолтного provider, задел на будущие provider'ы
с платным API-ключом). Отдельный, более строгий rate limit (10 запросов /
10 минут на IP, `enforce_company_research_rate_limit`) на `POST`. `GET` —
чистое чтение кэша, никогда не вызывает provider.

**Конфигурация (`.env`):**
```bash
COMPANY_RESEARCH_TTL_HOURS=720
COMPANY_RESEARCH_AUTO_ENABLED=false
COMPANY_RESEARCH_AUTO_MAX_PER_RUN=20
```
`COMPANY_RESEARCH_AUTO_ENABLED` (по умолчанию `false`) — при `true` после
каждого сохранённого `APPLY`-рекомендованного job'а в
`_run_bundesagentur`/`_run_xing` автоматически (best-effort, не влияет на
`created`/`updated`/`failed`) запускается research — см.
`_maybe_auto_research` в `app/api/routes.py`. `COMPANY_RESEARCH_AUTO_MAX_PER_RUN`
ограничивает, сколько таких автоматических research'ей может запустить один
прогон коллектора, независимо от того, сколько `APPLY`-вакансий он выдал —
ручные вызовы (`POST`, Telegram `/research`) этим бюджетом не ограничены.

**Telegram:** `/research <id>` — компания, статус, confidence, короткие
relevant facts; если refresh не удался, но показан старый результат — явно
"Refresh failed — showing cached research."; при полном провале без данных —
контролируемое сообщение об ошибке, не падение. 4096-символьный лимit и
авторизация — как у остальных команд.

**Безопасность:** никакого произвольного URL-fetch endpoint'а в API нет и
не было; единственный HTTP-клиент, который вообще существует в проекте —
`app/collectors/bundesagentur.py` (внешний job source, не Company
Research); секреты не нужны для дефолтного provider'а, для будущих
провайдеров — только через `Settings`/env.

## Проверки
```bash
pytest -q
ruff check .
ruff format --check .
pre-commit install
```
