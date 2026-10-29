# Stage 20 — ночной прогресс (29.10.2026)

Ветка: `stage20/operator-dashboard-foundation` (от `b745aa4` = `main`). Не публикуется, в `main` не сливается.

## 0. Восстановление после перезагрузки

| Проверка | Результат |
|---|---|
| Причина перезагрузки | Событие 1074 в 01:14:35: `shutdown.exe`, локальная учётная запись оператора, тип «neu starten», код `0x800000ff`. Событий Kernel-Power 41 и 6008 (неожиданное выключение) нет. Это **штатный перезапуск по команде**, а не нехватка памяти и не сбой питания. Какой процесс вызвал `shutdown.exe`, журнал не показывает. |
| Память после старта | 17,3 ГБ свободно из 32,5 ГБ |
| Stage 19 | PR #13 смёржен (`b745aa4`), CI зелёный, отчёт: STAGE19 RELEASE COMPLETE |
| PR #14 (docs, отчёт о релизе Stage 19) | OPEN, CI зелёный (test / scheduler-postgres / docker), MERGEABLE. **Не сливаю:** в согласованных пределах релиза этого шага нет. Решение за оператором |
| Состояние Stage 20 до перезагрузки | Не закоммичено: `docs/STAGE20_UI_ARCHITECTURE.md`, `app/ui/auth.py`, `app/ui/readonly.py`, флаги `ui_*` в `config.py`, лимитер входа, зависимость `jinja2`. Нет queries, routes, templates и тестов. Checkpoint-файлов не было |
| Незавершённые операции | stash пуст, rebase/merge нет, временных серверов не найдено |

## Журнал блоков

(дополняется после каждого блока)

### Блок A — read-only Operator UI (dashboard, вакансии, Bewerbungen, inbox, follow-ups)

- `app/ui/`: вход по `API_KEY` (cookie с HMAC, без хранения состояния), read-only сессия БД (SQLite `query_only`, PG `default_transaction_read_only`, запрет flush), `queries.py` поверх существующих сервисов, 9 шаблонов Jinja2 с autoescape, CSS, без JavaScript. Флаг `UI_ENABLED=false` по умолчанию, все `/ui` → 404.
- Страницы: `/ui/` (dashboard), `/ui/vacancies` (поиск, город, источник, scorer, fit tier, applied, remote/hybrid, keyset-пагинация, scan budget 200), `/ui/vacancies/{id}` (критерии, evidence, маршрут, дубликаты), `/ui/applications[/{id}]`, `/ui/inbox` (очередь NEEDS_OPERATOR_REVIEW, dry run процессора Stage 15 по сохранённым письмам), `/ui/follow-ups` (просрочено / сегодня / 7 дней / позже / без даты, в часовом поясе оператора).
- Рефакторинг без изменения поведения: `applicant_addresses` и выборка сохранённых писем перенесены из `scripts/stage15_application_tracker.py` в `app/services/application_response_processor.py`. CLI вызывает их и передаёт прежний путь к identity-файлу.
- Упаковка: `app/ui/__init__.py` и package-data для шаблонов/CSS (проверено по составу wheel).
- Тесты: `tests/test_stage20_ui.py` — 27 passed. Регрессии: Stage 15 / response tracking / reply mailboxes / night audit / Stage 16 import graph — 163 passed; Stage 15 lifecycle/remediation/outbound, Stage 19 E2E/regressions, health/readiness/startup — 191 passed, 1 skipped; gmail analysis endpoints — 16 passed; deployment config / env example — 44 passed. Ruff check + format — чисто.
- Codex: раунд 1 REJECT (3×P2: Origin за TLS-прокси, 500 на кривом cookie, буферизация тела логина) → исправлено; раунд 2 REJECT (2×P2: проверка размера до добавления chunk, `ValueError` на `Origin: https://[`) → исправлено; раунд 3 **ACCEPT**.
- Попутно: в тесте кривых cookie первоначальный вариант не отправлял cookie (тест был пустым). Найдено с помощью позитивного контроля, тест переписан на сырой заголовок.

### Смоук-тест блока A (реальный lifespan приложения)

Изолированная SQLite в scratchpad (`DATABASE_ROLE=test`, Telegram-токен пуст, все флаги отправки `false`), 40 синтетических вакансий, `alembic upgrade head`. Вход 303 с `Secure` cookie. Все 8 страниц и CSS отвечают 200 за 8–140 мс. `/api/v1/jobs` без ключа — 401. Файл БД не изменился (сравнение mtime).

### Блок B — безопасная генерация PDF из подтверждённых данных

- `app/services/pdf_document.py`: собственный генератор PDF на stdlib для тех же блоков, что и DOCX: `build_cv_document` (только доверенные факты черновика CV и подтверждённая identity) и `letter_blocks` (проверенное оператором письмо). Шрифты Helvetica/Helvetica-Bold, WinAnsiEncoding, точный перенос строк по ширинам Adobe Core14 (`pdf_font_metrics.py`, уведомление Adobe сохранено). Новых зависимостей нет.
- Безопасность содержимого: символ вне cp1252 → `UnrenderableTextError`; ничего не заменяется и файлы не пишутся. Каждый PDF перечитывается через pypdf (то же извлечение текста, что в `presubmit_check.py`); при расхождении с исходными блоками → `PdfVerificationError`. Байты детерминированы: нет дат, ID и сжатия, поэтому хеши `_manifest.json` стабильны.
- `export_package_documents(..., document_format="pdf")`, DOCX остаётся по умолчанию. Оба документа рендерятся до записи первого файла.
- Найдено при проверке: письмо без контактов блокируется pre-submit check (`profile phone not in document`). Добавлен блок отправителя (имя, адрес, телефон, e-mail) из подтверждённой identity, только если проверки контактов CV прошли. Применяется к DOCX и PDF одинаково.
- Сгенерированный пакет (CV + письмо в PDF) проходит `validate_package` с результатом **READY**. При подтверждённом A2 pre-submit ловит B1 в PDF (`CV_GERMAN_OVERCLAIM`): текст реально читается.
- Визуальная проверка **не выполнена**: растеризатора нет, headless Chrome не рендерит PDF-viewer в скриншот. Геометрия проверена тестом: каждая строка внутри полей. Просмотр каждого PDF оператором и presubmit остаются обязательными.
- Тесты: `tests/test_stage20_pdf.py` — 15 passed. Вместе со Stage 20 UI, Stage 14D, package isolation, night-audit presubmit — 168 passed. Ruff по всему репозиторию — чисто.
- Codex: раунд 1 REJECT (P2: кубическое время разбиения длинного слова; P3: keep-with-next не учитывал отступы и цепочки заголовков). Исправлено; регрессионный тест подтверждён на старом коде (заголовок на стр. 1, текст на стр. 2). Раунд 2 **ACCEPT**.

### Полный прогон и PostgreSQL

- Полный pytest на `bc7d03f` (последовательно, без Codex и Docker параллельно): **6614 passed, 124 skipped, 0 failed** за 29:42. Skip-ы — PostgreSQL-тесты без `TEST_POSTGRES_URL`, как в CI-job `test`. Первая попытка не стартовала: `--timeout` требует отсутствующий `pytest-timeout`, тесты не выполнялись.
- Новый `tests/integration/test_stage20_ui_readonly_postgres.py` (3 теста): на настоящем PostgreSQL сессия UI read-only (`default_transaction_read_only`, в том числе после rollback и на повторно выданном из пула соединении); ORM- и Core-записи падают; запросы UI (экранирование LIKE, keyset-пагинация, dashboard, follow-ups) работают внутри read-only транзакций. Добавлен в zero-skip gate `scheduler-postgres` в `.github/workflows/ci.yml`.
- Одноразовый контейнер `postgres:16` (16.15) на `127.0.0.1:55432`, `alembic upgrade head` с нуля, все 18 PG-файлов в порядке CI: **122 passed, 0 skipped**. Контейнер остановлен и удалён. Рабочий `ai_agent-db-1` не трогался: в этот момент он не был запущен.

## Финальная проверка и релиз (29.10.2026, утро)

### Phase A/B — состояние и упаковка
- HEAD `e43b1fb`, ветка на 3 коммита впереди `origin/main` (`b745aa4`); PR #13 MERGED, PR #14 OPEN (не трогаю), других открытых PR нет.
- Полный pytest (6614 passed) относится к `bc7d03f`; `e43b1fb` менял только PG-тест и `ci.yml` — повтор не нужен.
- Wheel собран из чистой копии дерева (в репозитории `build/` не создавался), установлен в отдельный venv; запуск из пакета вне репозитория: все страницы 200, шаблоны и CSS на месте, `jinja2`/`pypdf` в зависимостях.

### Phase C — настоящий браузер (Chrome 156, Playwright)
- Синтетическая SQLite (60 вакансий, 6 заявок с историей, 5 писем), сервер из установленного wheel, без `.env`, все флаги отправки выкл.
- **Найдено и исправлено (P1):** вход в браузере не работал вообще. `Referrer-Policy: no-referrer` заставляет Chrome слать `Origin: null` в POST формы входа, проверка Origin его справедливо отклоняет. Тесты TestClient всегда ставили Origin сами. Политика теперь `same-origin`; `Origin: null` по-прежнему 403 (тест).
- **Найдено и исправлено:** на 390 px таблицы прокручивались вбок, причины fit tier были скрыты. Теперь ниже 760 px строки — карточки с подписями (`data-label`, только CSS).
- Итог: 12 проверок × 3 разрешения пройдены; внешних запросов 0, диалогов (XSS) 0, горизонтального переполнения 0. Cookie: `Secure; HttpOnly; SameSite=Strict; Path=/ui` — Chrome принимает его на `http://127.0.0.1`. Скриншоты — только в scratchpad.
- Коммит `9667789`.

### Phase D — PDF
- PDFium (движок Chrome) отрисовал CV, письмо, 3-страничный CV и stress-документ: всё A4, текст внутри полей 62 pt, умлауты/ß/€/„“ верны, ничего не обрезано. SHA-256 повторяется, текст совпадает, `Łódź` отклоняется.
- Настоящий `presubmit_check.py`: все проверки текста и контактов OK, но итог **NEEDS_OPERATOR**, не READY — `--bind` связывает только PDF из `_quelle_html`. Безопасное направление; задокументировано, привязка экспортированных PDF — задача на потом.

### Phase E — запуск одной командой
- `scripts/run_operator_ui.py`: 127.0.0.1, Telegram-бот/миграции/все флаги отправки выкл. для своего процесса, отказ на отсутствующий файл БД и пустой `API_KEY`, `--snapshot` (backup API), отпечаток БД до/после → UNCHANGED/CHANGED. 7 тестов. Проверен вживую на синтетической БД: UNCHANGED.
- Коммит `5b4b9d1`.

### Phase F — read-only пилот на production: **ЗАБЛОКИРОВАН**
- Production `D:\AI_agent\runtime\job_triage_runtime.db` лежит на диске D:, которого после перезагрузки нет в системе (тома только C: и E:). Поиск по E:\ и профилю пользователя на C: — файла нет. Копию не подменял, ничего другого не открывал. Как только D: будет подключён — команда из `docs/DAILY_OPERATIONS.md` §1a со `--snapshot`.

### Финальные исправления и релиз
- Codex, финальное ревью всей ветки: 3×P2 в запускателе (POSTGRES_HOST, WAL, код выхода uvicorn) + P2 в тесте WAL → исправлено, ACCEPT. Коммит `f22cb46`. Полный pytest: 6626 passed, 127 skipped, 0 failed.
- Привязка экспортированных PDF в `presubmit_check.py --bind` (источник `_quelle_html` с хешем текста). Codex: P2 (испорченный UTF-8 источник) → исправлено, ACCEPT. Коммит `2e076a4`. Полный pytest: 6633 passed, 127 skipped, 0 failed.
- Диск D: — USB-флешка, не подключена: production pilot BLOCKED.
- Публикация — отдельной веткой `stage20/operator-ui-release` одним коммитом (в промежуточной истории этой ветки были имя компьютера и путь профиля). Отчёт: `docs/reports/STAGE20_RELEASE_VERIFICATION_2026-10-29.md`.
