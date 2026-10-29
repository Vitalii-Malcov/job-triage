# Stage 20 — перенос базы на новый ноутбук и Docker (29.10.2026)

Ветка: локальная `local/new-laptop-setup` (от `main` `28a8b5f`), не опубликована.
Только проверка и настройка. Ни одна запись в production не изменялась, новых функций нет.

## 1. Итог

| Компонент | Статус |
|---|---|
| DATABASE | OK — найдена, целостна, совпадает с контрольными показателями |
| BACKUP | OK — две согласованные копии (SQLite backup API) вне репозитория |
| DOCKER | OK — Docker Desktop, Linux containers, WSL2 |
| POSTGRESQL | OK — изолированный PostgreSQL 16.15, 122 passed, 0 skipped |
| WEB UI | OK — реальные данные, только чтение |
| PRODUCTION UNCHANGED | YES — побайтно тот же файл до и после всех проверок |

## 2. База данных

| Показатель | Значение | Ожидалось |
|---|---|---|
| Путь | `E:\Projects\job-triage\runtime\job_triage_runtime.db` | — |
| Размер | 19 927 040 байт | — |
| SHA-256 файла | `383d518b…a731b` (совпадает со значением из отчёта Stage 19 со старого компьютера) | — |
| `PRAGMA integrity_check` | ok | ok |
| `PRAGMA foreign_key_check` | без нарушений | — |
| `PRAGMA user_version` | 0 (проект использует Alembic) | — |
| journal_mode | delete (WAL не используется; `-wal`/`-shm` нет) | — |
| Alembic | `b6c1e9a4d2f7` | `b6c1e9a4d2f7` |
| Таблиц | 47 | 47 |
| Вакансий (`jobs`) | 2888 | 2888 |
| Bewerbungen (`applications`) | 15 | 15 |
| Application Events | 27 | 27 |
| Candidate Profile | v5 | v5 |
| Настройки поиска | `candidate_job_preferences` 1, проверка маршрута 1 | есть |
| Сохранённые письма (`gmail_messages`) | 0 | — |

Других копий `job_triage_runtime*.db` на C: и E: нет, кроме старого резервного файла `runtime/job_triage_runtime.pre-profile-v4.2026-10-20.db` (профиль v4, не production).
`gmail_messages` пуст: Operator Inbox показывает 0 писем, пока не выполнена синхронизация Gmail (в этой работе не запускалась).

Диагностика выполнялась только через `sqlite3` с `?mode=ro`; запись не открывалась.

## 3. Резервные копии

- Папка: `%USERPROFILE%\JobTriage-Backups\` — внутренний диск, вне Git. Не `Documents`, потому что на ноутбуке работает OneDrive.
- `job_triage_runtime.pre-ui-20261029T082427Z.db` — SHA-256 файла `f4b8b59e…5015`; integrity ok, foreign keys ok, 47 таблиц, `b6c1e9a4d2f7`, 2888 / 15 / 27, профиль v5, логическое содержимое (хеш дампа `eac0cb1c…`) совпадает с оригиналом.
- `job_triage_runtime.pre-ui-20261029T082858Z.db` — снимок, сделанный запускателем перед первым запуском UI, тот же хеш содержимого.
- Байты копии отличаются от оригинала — так работает backup API (страницы перезаписываются); сравнение идёт по содержимому.

## 4. Git и Docker ignore

Найдено: побочные файлы SQLite (`*.db-wal`, `*.db-shm`), `*.sqlite*` (включая `-wal`/`-shm`/`-journal`), `*.bak` не исключались ни `.gitignore`, ни `.dockerignore`. WAL-файл содержит реальные данные; репозиторий публичный.
Исправлено (локальная ветка): шаблоны `*.db-*`, `*.sqlite*`, `*.bak` плюс `runtime/` и `job-triage-backups/` добавлены в `.gitignore`; в `.dockerignore` дополнительно `*.local.json` (контакты кандидата), `runtime`, `job-triage-backups`, `postman`, `.postman`.
В Docker-образ база и раньше не попадала: `Dockerfile` копирует только `pyproject.toml`, `app/`, `alembic/`, `alembic.ini` и entrypoint. Исправление `.dockerignore` не даёт этим файлам уходить даже в build context. Шаблоны там рекурсивные (`**/*.db`, `**/*.db-*`, `**/*.sqlite*`, `**/*.bak`, `**/*.local.json`): в `.dockerignore` шаблон без `**/` действует только в корне, поэтому прежний `*.db` не исключал, например, `app/x.db`. Проверено пробной сборкой с файлами-образцами (`a.db`, `app/sub/b.db`, `*.db-wal`, `*.sqlite3-shm`, `*.bak`, `*.local.json`, `runtime/g.db`): в контекст попал только `app/keep.py`; пробный образ удалён.

Codex: раунд 1 REJECT (P2: побочные файлы `*.sqlite*` не исключались) → шаблоны `*.db-*` и `*.sqlite*`; раунд 2 — **ACCEPT**.

## 5. Docker Desktop

| Проверка | Результат |
|---|---|
| Docker Engine | 29.9.2 (client и server), linux/amd64 |
| Контейнеры | Linux, ядро `6.18.54.1-microsoft-standard-WSL2` |
| WSL2 | `Ubuntu` и `docker-desktop` — Running, версия 2 |
| Docker Compose | v5.6.1; `docker compose config` — валиден |
| Ресурсы | 16 CPU, 15,5 ГиБ памяти для Docker; свободно C: 393 ГБ, E: 409 ГБ |
| Порты 8000, 8001, 5432, 55432 | свободны до начала работы |
| Чужие контейнеры/volumes | не было (0 контейнеров, 0 volumes, 1 образ `postgres:16`) |

Ничего не удалялось; `prune` и `down -v` не выполнялись.

## 6. PostgreSQL 16 (изолированный тест)

- Контейнер `jobtriage-pgtest` (`postgres:16`, 16.15), volume `jobtriage-pgtest-data`, порт только `127.0.0.1:55432`, собственный случайный пароль (не из `.env`), healthcheck `pg_isready` → healthy.
- `alembic upgrade head` с пустой базы → `b6c1e9a4d2f7`; `alembic heads` — один head. Alembic запускался из папки без `.env` с явным `DATABASE_URL`, чтобы никак не попасть в production SQLite.
- PostgreSQL integration tests (все 18 файлов в порядке CI-job `scheduler-postgres`): **122 passed, 0 skipped, 0 failed** — совпадает с контрольным значением Stage 20.
- Тесты на production не обращались: `tests/conftest.py` всегда использует временную БД и `DATABASE_ROLE=test`; SHA-256 production до и после — одинаковый.
- Сохранность volume: контейнер удалён и создан заново на том же volume — ревизия `b6c1e9a4d2f7` и 47 таблиц на месте.
- Контейнер остановлен; образ и тестовый volume оставлены для следующих прогонов.

Повторный запуск:
```powershell
docker start jobtriage-pgtest
# TEST_POSTGRES_URL = postgresql+psycopg://pgtest:<пароль контейнера>@127.0.0.1:55432/pgtest
docker stop jobtriage-pgtest
```

## 7. Web UI на реальных данных (только чтение)

Запуск через `scripts/run_operator_ui.py` с настоящим `.env`. Процесс сервера: Telegram-бот выключен (`telegram_bot_disabled`), `alembic_auto_upgrade=false`, все флаги отправки и планировщика `false`; коллекторы, планировщик, синхронизация Gmail не запускались. Только `127.0.0.1:8000`.

Проверено в Chrome (выводились только коды ответа и количества, без текста страниц и без скриншотов):

| Страница | Результат |
|---|---|
| Вход | без входа → `/ui/login`; неверный ключ отклонён; верный → dashboard |
| Dashboard | 200; 2888 вакансий, 15 Bewerbungen — данные именно перенесённой базы |
| Вакансии | 200; 25 строк на странице, вторая страница 25 строк |
| Фильтры | город, fit tier, applied, remote — 200 (вычисляемые фильтры работают с бюджетом сканирования) |
| Карточка вакансии | 200; fit tier и 10 критериев |
| Bewerbungen / история | 200; 15 строк; история событий открывается |
| Operator Inbox | 200; 0 сохранённых писем (таблица `gmail_messages` пуста) |
| Follow-up Center | 200; ближайшие 7 дней — 6, позже — 8, просроченных 0 |
| Выход | снова `/ui/login` |
| Внешние запросы / JS-диалоги | 0 / 0 |

## 8. Production unchanged

| Момент | SHA-256 файла | Размер, mtime | Содержимое |
|---|---|---|---|
| До всех проверок | `383d518b…` | 19 927 040, 27.10.2026 23:04:35 | — |
| После backup, Alembic и PG-тестов | `383d518b…` | без изменений | — |
| После работы UI | `383d518b…` | без изменений | запускатель: UNCHANGED (целостность, строки 5913, хеш содержимого `eac0cb1c…`); диагностика до/после идентична |

Запускатель завершился кодом 3 («SERVER FAILED»): сервер был остановлен принудительно, а не Ctrl+C. Это ожидаемо и на данные не влияет.

## 9. Docker или локально

**Выбрано: вариант A** — FastAPI локально через `scripts/run_operator_ui.py`, Docker только для изолированного PostgreSQL и тестов.

- Рабочие данные находятся в SQLite. Существующий `compose.yaml` — серверная схема: сервис `web` всегда подключается к PostgreSQL (`POSTGRES_HOST: db`) и берёт весь `.env`, включая токен Telegram. Запуск `docker compose up web` дал бы интерфейс на **пустой** PostgreSQL и запустил бы Telegram-бота.
- Вариант B (FastAPI в Docker с монтированием SQLite) потребовал бы новой конфигурации Compose и нового решения о том, как контейнер видит файл на Windows-диске. Это меньше стабильности, а не больше.
- Перенос production в PostgreSQL — отдельное согласованное решение; здесь он не выполнялся.
- Не запускать одновременно: локальный `run_operator_ui.py` и `docker compose up web` (оба хотят порт 8000).

## 10. Запуск JobTriage на ноутбуке

```powershell
cd E:\Projects\job-triage
.\.venv\Scripts\python.exe scripts\run_operator_ui.py `
    --database runtime/job_triage_runtime.db `
    --snapshot "$env:USERPROFILE\JobTriage-Backups"
```

Открыть <http://127.0.0.1:8000/ui/>, войти с `API_KEY` из `.env`. Остановка — Ctrl+C; в конце запускатель пишет `UNCHANGED` или `DATABASE CHANGED`.

## 11. Что не сделано и почему

- Операции записи (коллекторы, синхронизация Gmail, Telegram, планировщик) не запускались — вне задачи и без разрешения.
- Ветка `local/new-laptop-setup` не опубликована и не слита — нужна отдельная команда.
- Две заявки без места работы (Stage 19) не исправлялись — это изменение production.

## 12. Какую базу видит браузер (проверка 29.10.2026, после 09:37)

- Сервер в браузере — запущенный пользователем `scripts/run_operator_ui.py --database runtime/job_triage_runtime.db --snapshot "$env:USERPROFILE\JobTriage-Backups"` (uvicorn только на `127.0.0.1:8000`; `/api/v1/ready` = ready, то есть база на Alembic head). Запускатель передаёт серверу абсолютный путь к этому файлу и отказывается стартовать, если настройки ведут к другой базе.
- Файл `runtime/job_triage_runtime.db` — единственный с таким именем на C: и E:. Его SHA-256 `383d518b…` побайтно совпадает со значением, измеренным на старом компьютере в отчёте Stage 19: это та же production-база, а не копия с похожими числами. Повторный перенос не нужен.
- Сравнение работающего UI с файлом (read-only): 2888 вакансий, максимальный ID 2888 (ID 2889 → 404), новейшая от 27.10.2026, Bundesagentur 2770 + XING 118; 15 Bewerbungen (SUBMITTED 10, RECEIVED 4, REJECTED 1), 27 событий, профиль v5 — UI и файл совпадают во всех пунктах. Файл во время работы сервера не изменился.

## 13. Повседневный сценарий на реальных данных (только чтение)

| Шаг | Результат |
|---|---|
| Dashboard | 200, ~0,1 с; 2888 вакансий |
| Fit tiers на dashboard | `UNKNOWN`: за 14 дней больше вакансий, чем бюджет 200 оценок — честный отказ вместо неверных чисел |
| Поиск Python / Backend / Junior | по 25 на странице и дальше; «Junior Python» — 1, «Python Backend» — 2; ~0,6 с |
| Python + GOOD_MATCH / PARTIAL_MATCH | 0 на первой странице за ~4,2 с, ссылка «продолжить сканирование»: fit tier вычисляется на каждый запрос, 200 вакансий за раз |
| Карточка вакансии | fit tier, «Why this tier», 10 критериев, маршрут, дубликаты, описание |
| Bewerbungen и история | 15 заявок, 27 событий; у 5 есть запись со стороны работодателя |
| Follow-ups | 7 дней — 6, позже — 8, просрочено 0 |
| Inbox | работает, 0 сохранённых писем (Gmail не синхронизировался) |
| Телефон 390 px | найдено: описание с URL из 64 символов расширяло страницу до 398 px |

Исправление ширины — локальная ветка `fix/ui-long-words` (`657f1c4`): `overflow-wrap` для описания; проверено в Chrome (904 px → без переполнения), полный обход 3 размеров без изменений, тест падает на старом CSS; Codex ACCEPT. Не опубликовано.

Ограничения Stage 20 для ежедневной работы (предложение на следующий этап, не начато):
1. Сохраняемый кэш fit tier (пересчёт после сбора вакансий и смены профиля) — чтобы «подходящие вакансии» и счётчики на dashboard отвечали сразу, а не через сканирование по 200.
2. Команда экспорта пакета Bewerbung в PDF из базы — сейчас экспорт есть как функция, но без CLI.
3. Действия из браузера с подтверждением (CSRF, явный шаг, аудит) — пока решения только через CLI/Telegram.
4. Синхронизация Gmail для Inbox — существующая, но не запускавшаяся на ноутбуке (нужно отдельное разрешение).
