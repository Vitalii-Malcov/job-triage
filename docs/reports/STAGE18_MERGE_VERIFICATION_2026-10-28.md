# Stage 18 — Safe Merge & Production Readiness (28.10.2026)

**Итог: STAGE 18 DONE.** PR #12 слит в `main` (squash, merge-коммит `bdae3cd`) после зелёного GitHub CI.
Production SQLite не изменена, новых Bewerbungen и писем нет.

## 1. Состояние PR #12 до merge

| | |
|---|---|
| PR | https://github.com/Vitalii-Malcov/job-triage/pull/12 — open, не draft |
| base / head | `main` @ `b94f0e0` / `maintenance/night-audit-2026-10-27-pr` |
| Коммиты PR | `a00398a` (squash ночного аудита) + `4c693cd` (исправление по CI, см. §2) — других нет |
| Mergeability | `mergeable: true`, `mergeable_state: clean` (перед merge) |
| Файлы | 33, +5479/−121 |
| Старая история аудита | **не опубликована**: в PR только squash-коммит; 31 исходный коммит с названиями работодателей в сообщениях остался локально |
| Branch protection `main` | нет (`404 Branch not protected`, rulesets пусты) — обходить нечего |

## 2. GitHub CI

Workflow `CI` (`.github/workflows`): задачи `test` (Ruff + полный pytest, Linux), `scheduler-postgres` (PostgreSQL 16 + Alembic + 17 интеграционных модулей), `docker`.

| Коммит | test | scheduler-postgres | docker |
|---|---|---|---|
| `a00398a` | **failure**: 1 failed, 6536 passed | success | success |
| `4c693cd` | **success**: 6537 passed, 125 skipped, 0 failed; Ruff «All checks passed» | **success**: 119 passed | success |

**Найденный дефект (CI, Linux):** `test_cli_against_production_is_refused_inside_the_test_suite` ожидал отказ по роли, а CLI отвечал «database file does not exist»: на CI-раннере нет файла production-БД, и проверка существования срабатывала раньше проверки роли.

- Исправлено в коде, тест не менялся (`4c693cd`, `scripts/stage15_application_tracker.py`): проверка роли по пути выполняется до проверки существования файла. `classify_path` только сравнивает пути и ничего не открывает.
- Подтверждено локальной симуляцией отсутствующего файла. Старый код: «database file does not exist». Новый: «refusing the production runtime database under DATABASE_ROLE=test».
- Затронутые наборы локально: 322 passed.
- 125 skipped в задаче `test` — PostgreSQL-модули: они пропускаются без `TEST_POSTGRES_URL` и выполняются в `scheduler-postgres` (119 passed, 0 skipped).

Ссылки: https://github.com/Vitalii-Malcov/job-triage/actions/runs/40714831767 (pull_request) и https://github.com/Vitalii-Malcov/job-triage/actions/runs/40714819170 (push).

## 3. Проверка опубликованного diff

- Нет файлов БД / SQLite, PDF, DOCX, `.env`, логов, ключей; `git ls-files` не содержит `*.db`, `*.pdf`, `.env`, `job_triage_runtime*`.
- Секреты: поиск по шаблонам токенов GitHub, OpenAI, Slack и AWS, приватных ключей и токенов Telegram-ботов — совпадений нет.
- Персональные данные: email и телефоны только заглушки (`example.invalid`, `+49 151 00000000`); имени кандидата, личного email и домашнего адреса нет.
- Реальные названия работодателей, куда подавался кандидат, заменены вымышленными до публикации (коммит обезличивания вошёл в squash).
- Миграций нет: `alembic/` и `app/db/models.py` в PR не менялись.

## 4. Merge

- Способ: GitHub REST API `PUT /pulls/12/merge`, `merge_method=squash`, с `sha=4c693cd…` (сливается ровно проверенный CI коммит).
- **Merge-коммит: `bdae3cdeac27282e65a4ff9b7501335ad1644f26`.**
- Без force push, история `main` не переписывалась; другие ветки не удалялись (ветка PR тоже оставлена).

## 5. Обновлённый `main`

- Локальный `main` обновлён `git merge --ff-only origin/main`: `main` = `origin/main` = `bdae3cd`.
- **Дерево `bdae3cd` побайтово совпадает с деревом `4c693cd`**, на котором прошёл CI. Поэтому результаты CI (полный pytest, PostgreSQL) относятся к коду `main`.
- `git status`: отслеживаемые файлы без изменений, конфликтов нет.

## 6. Зависимости

- Окружение: `D:\AI_agent\.venv`, Python 3.14.3; установка `python -m pip install -e .` из `.venv`, без глобальных пакетов.
- Версии: pypdf 5.4.0 (новая зависимость `pypdf>=5.4`), SQLAlchemy 2.0.52, FastAPI 0.141.1, Alembic 1.19.1, httpx 0.28.1, pydantic 2.13.4, pydantic-settings 2.15.0, uvicorn 0.52.4, psycopg 3.3.5.
- `pip check`: «No broken requirements found». Импорт `app.main`, `pre_submit_validation`, `nominatim` — без ошибок.

## 7. Smoke tests (только безопасные операции)

**Веб-приложение** на изолированной новой SQLite в scratchpad, `DATABASE_ROLE=test`, пустой `TELEGRAM_BOT_TOKEN`, все флаги отправки выключены. Перед стартом проверено, что настройки указывают на эту базу.

- `alembic upgrade head` на пустой базе: один head `b6c1e9a4d2f7`.
- Запросы (только GET):
  - `GET /api/v1/health` → 200 `{"status":"ok"}`;
  - `GET /api/v1/ready` → 200 `{"status":"ready"}`;
  - `/docs` и `/openapi.json` → 200;
  - `GET /api/v1/jobs` без ключа → 401 «Invalid API key» (авторизация работает).
- Лог старта: `startup_safety_state`: outbound, Gmail draft, Telegram (все фичи), автоматизация, auto-upgrade — `false`; `telegram_bot_disabled reason=no_bot_token_configured`.

**CLI только на чтение** на копии резервной базы (не production):

| Проверка | Результат |
|---|---|
| Application tracker `list` | 15 заявок |
| Duplicate guard `check-job 2354` | `ALREADY_APPLIED`, `CLASSIFICATION: EXACT_DUPLICATE` |
| Candidate Profile | `profile_version=5` |
| Candidate Search Settings | регион, точка отсчёта, 100 км по дороге, `CAR_ROUTE` — на месте, подтверждены |
| Job Fit `assess 233` | все критерии посчитаны (язык, маршрут OSRM, режим работы, зарплата и т.д.) |
| Защита ролей | production под `DATABASE_ROLE=test` → «refusing the production runtime database» |
| Копия базы | SHA-256 после проверок совпадает с исходной резервной копией |

**Фикстуры и заглушки (pytest на `main`, Windows):** salary parser, job fit, language, duplicate guard, email classifier, response tracking, `presubmit_check.py` (синтетические пакеты), геокодинг (заглушки и кэш), изоляция SQLite и роли БД — **623 passed, 0 failed**.

Не запускались (намеренно): живой collector, Gmail ingestion с записью, Telegram, отправка через браузер, запросы к работодателям.

## 8. Ruff и pytest

- Ruff lint и format на `main`: 419 отслеживаемых Python-файлов, без замечаний (и в CI: «All checks passed»).
- Полный pytest: CI на дереве `main` — 6537 passed, 0 failed. Локально (Windows) на `6fe95e8` — 6539 passed, 0 failed, 4 skipped (symlink).
- PostgreSQL 16: CI на дереве `main` — 119 passed. Локально на `6fe95e8` — 119/0/0.

## 9. Production SQLite до и после

`D:\AI_agent\runtime\job_triage_runtime.db`, всегда только чтение (`mode=ro`).

| | До merge | После merge и smoke |
|---|---|---|
| Bewerbungen / события / вакансии | 15 / 27 / 2888 | 15 / 27 / 2888 |
| Статусы | 10 SUBMITTED, 4 RECEIVED, 1 REJECTED | без изменений |
| Candidate Profile | v5 | v5 |
| Alembic / integrity / FK | `b6c1e9a4d2f7` / ok / нарушений нет | без изменений |
| Таблицы относительно снимка до merge | — | **все 47 совпадают** (число строк и SHA-256 содержимого) |

- Резервная копия (SQLite backup API, источник открыт только на чтение): `C:\Users\BOSS\AppData\Local\job-triage-backups\stage18-merge-2026-10-28\job_triage_runtime_pre_stage18.db`. Рядом SHA-256 и логический снимок `pre_stage18_snapshot.json`.
- Процессов, способных менять production (веб-приложение, планировщик, бот, коллекторы), во время работы не было; задач в Планировщике Windows тоже нет.

## 10. Подтверждения

- Новых Bewerbungen нет, статусы заявок не менялись, профиль не менялся.
- Писем не отправлено, черновики Gmail не трогались, Telegram не запускался.
- Отправка заявок по-прежнему только вручную и с явного разрешения оператора:
  - `OUTBOUND_SENDING_ENABLED`, `TELEGRAM_GMAIL_DRAFT_ENABLED`, `TELEGRAM_OPERATOR_CONTROL_ENABLED` по умолчанию `false`;
  - `presubmit_check.py` обязателен перед финальным нажатием (`CLAUDE.md`, «Browser submission gate»).

## 11. Безопасность и остаточные риски

**Проверено:**
- `.playwright-mcp/`, `*.db`, `*.local.json` (кэш геокодера), `.env` игнорируются (`git check-ignore`).
- Production SQLite и оригинальные PDF вне Git: каталог пакетов `C:\Users\BOSS\AppData\Local\job-triage-packages` — не репозиторий.
- Защита ролей БД включена и в веб-приложении, и в CLI.

**Остаточные риски:**
1. **Персональные данные в старых публичных коммитах** (не исправлено, решение владельца). История не переписывалась.
   - Рабочий email кандидата есть в `main` с коммита 784ee2c (тестовая фикстура; в ночном аудите заменён).
   - Город и PLZ проживания есть в ранних коммитах PR #11 и в remote-ветке `feat/stage-17-candidate-search-config`.
   - Варианты: принять риск или удалить старую remote-ветку. Полная очистка истории PR возможна только через GitHub Support.
2. Локальная ветка `maintenance/night-audit-2026-10-27` (полная история) содержит названия работодателей в сообщениях коммитов. Её нельзя пушить; её содержимое уже в `main`.
3. Эвристики на немецком тексте (уровень языка, города, проверка PDF) — P3. Ручной просмотр PDF оператором обязателен.
4. Старые пакеты без `_manifest.json` дают `PROFILE_VERSION_UNRECORDED` (NEEDS_OPERATOR), пока не выполнен `presubmit_check.py --bind`.
5. P3 из backlog: диапазон зарплаты «zwischen X und Y» читается по верхней границе.

## 12. Следующий рекомендуемый этап

**Stage 19 — встроить проверки в ежедневную работу оператора** (без новой автоматизации отправки):
1. Для текущих пакетов выполнить `presubmit_check.py --bind` после просмотра PDF и использовать `presubmit_check.py` перед каждой отправкой. Результат записывать в `--evidence` в `record-submission`.
2. Пробный прогон `process-gmail` в ежедневной сводке: показывать письма `NEEDS_OPERATOR_REVIEW` (сейчас без автоматической записи).
3. Решить вопрос персональных данных в публичной истории (§11.1).
4. Закрыть P3 по зарплатным диапазонам «zwischen X und Y».
