# Ночной технический аудит JobTriage — 27/28.10.2026

Ветка `maintenance/night-audit-2026-10-27` (от `main` b94f0e0). 29 коммитов кода и отчёта (+ этот),
33 файла, +5442/−121 строк. Не слита в `main`, не запушена. Release Verification — 28.10.2026.

## Краткая сводка

| Показатель | Значение |
|---|---|
| Реестр дефектов | 20 пунктов (ниже) |
| Закрыто (исправлено, уже исправлено или задокументировано) | 15 из 20 = **75 %** |
| Исправлено багов (по реестру) | 12 |
| Исправлено замечаний Codex | 63 (21 в раундах 1–4, 42 P2 и 1 P3 в раундах 5–19) |
| Новых тестов | 323 (7 файлов `tests/test_night_audit_*.py`) |
| Полный pytest на HEAD 6fe95e8 | **PASS**: 6539 passed, 0 failed, 4 skipped (symlink в Windows) |
| PostgreSQL 16 gate на HEAD 6fe95e8 | **PASS**: 17 модулей, 119 тестов, 0 skipped, 0 failed |
| Ruff (lint + format, 419 отслеживаемых файлов) | **PASS** |
| Codex | **ACCEPT** в раунде 20 на 6fe95e8 (раунды 1–19 — REJECT, все находки исправлены) |
| Production SQLite | **не изменена** (47 таблиц совпадают с исходной копией) |
| Нерешённые P1 / P2 | 0 / 1 (персональные данные в публичной истории: нужно решение владельца) |
| Готовность к слиянию с `main` | **READY FOR MERGE** (код 6fe95e8; push/PR/merge — только с разрешения владельца) |

## 1. Исходное состояние

- Git: `main` = `origin/main` = b94f0e0, рабочее дерево без изменений в отслеживаемых файлах.
- Production SQLite `runtime/job_triage_runtime.db`:
  - 15 Bewerbungen, 27 событий, 2888 вакансий;
  - статусы: 10 SUBMITTED, 4 RECEIVED, 1 REJECTED;
  - Alembic `b6c1e9a4d2f7`, профиль v5;
  - `integrity_check` = ok, нарушений внешних ключей нет.
- Резервная копия (SQLite backup API): `C:\Users\BOSS\AppData\Local\job-triage-backups\night-audit-2026-10-27\job_triage_runtime_baseline.db`.
  - Логический снимок (число строк и хеш каждой таблицы) лежит там же: `baseline_snapshot.json`.
- На время работы файл production-БД был помечен «только чтение». Флаг снят в конце.

## 2. Реестр проблем

| # | Приоритет | Проблема | Статус | Коммиты |
|---|---|---|---|---|
| 1 | P1 | Отказ работодателя через портал (`anderweitig entschieden`) классифицирован как GENERAL_RECRUITER_SIGNAL → IGNORE | **Исправлено** (воспроизведено на реальном письме) | 86ad84b, 0e164c1, c931906 |
| 2 | P1 | Квитанция JOIN («vielen Dank für Deine Bewerbung») → AUTOMATED_SIGNAL / RECORD_ONLY вместо RECEIVED | **Исправлено** (на реальном письме теперь RECEIPT_CONFIRMATION, MEDIUM) | 86ad84b |
| 3 | P1 | Слабая корреляция без возможности разбора: письмо без названия вакансии молча игнорируется | **Исправлено**: флаг `operator_review=NEEDS_OPERATOR_REVIEW`, сигнал SENDER (только как доказательство, ничего не записывает) | 86ad84b, 0e164c1, c931906, 351425e |
| 4 | P1 | Риск отнести письмо не к той вакансии того же работодателя | **Исправлено и защищено тестами** (ALD, две вакансии одного работодателя, псевдоним IBM, филиал «Standort») | 0e164c1, c931906, 351425e |
| 5 | P1 | Дубликаты: агентства (Akkodis, Hays) с переписанным названием в том же городе не находились | **Исправлено**: SIMILAR_ROLE_VIA_INTERMEDIARY (проверить вручную, не блокирует) | 734f9a3 |
| 6 | P1 | Разные написания работодателя (`e.G.`/`eG`, ü/ue, `N.O.C`) | **Исправлено** (`company_identity`) | 734f9a3, 0e164c1 |
| 7 | P1 | Переписанный заголовок ALD (то же BA-Ref) | **Уже было исправлено** (SAME_REFERENCE). Доказательство: `check-job 2354` | — |
| 8 | P1 | Дубликаты: нет градации EXACT / PROBABLE / POSSIBLE / NOT / INSUFFICIENT_EVIDENCE | **Исправлено** (`classify_duplicate`, строка CLASSIFICATION) | 734f9a3 |
| 9 | P1/P2 | Зарплата: `IT-Zulage 1.500 € mtl.` прочитана как оклад; ещё 35 вакансий с бонусами или бюджетами как окладом; ×13 считалось как ×12 | **Исправлено** | c0a36cb |
| 10 | P1 | CLI открывали любой SQLite: `--apply` в бэкап или пилот, тестовая роль могла открыть production | **Исправлено** (бэкап, пилот, PostgreSQL-имена, жёсткая ссылка, таблица ролей) | a3e60c7, 0e164c1, a325f42 |
| 11 | P1/P2 | Геокодинг: координаты работодателя искались вручную; маршрут до центра города считался проверенным | **Исправлено**: адрес из объявления, Nominatim с кэшем и лимитом, OSRM_APPROX, кэш маршрутов, статусы близко к лимиту | 911b889, 0e164c1, c931906, 351425e, a325f42 |
| 12 | P2 | «sehr gute Deutschkenntnisse» приравнивалось к требованию C1 → MAJOR_GAP | **Исправлено**: `basis` EXPLICIT / INFERRED / PREFERRED; выведенный уровень даёт GAP_TO_VERIFY | badb4e6 |
| 13 | P2 | Происхождение полей профиля и их достоверность | **Проверено, проблем нет** (все строки от пользователя, CONFIRMED) | — |
| 14 | P2 | Документы: PDF другой вакансии в форме StepStone; ручная сверка копий загрузки | **Исправлено**: `scripts/presubmit_check.py` (текст самого PDF, SHA-256 копии загрузки, WRONG_PACKAGE, поля формы) | cb9a557, 75593c2, c931906, 351425e, a325f42 |
| 15 | P2 | Банковский портал CHECK-IN: кнопка «Vorschau» сбросила сессию формы (регрессия 3) | **Задокументировано** (DAILY_OPERATIONS 7a). Это особенность портала, кодом не исправляется | cb9a557 |
| 16 | P2 | Портал работодателя: форма требует Zeugnisnote и Notenauszug (регрессия 4) | **Закрыто процессом**: `--missing-field` даёт BLOCKED, просьба к кандидату, ничего не выдумывается | cb9a557 |
| 17 | P2 | Персональные данные в отслеживаемых фикстурах; `.playwright-mcp` не игнорировался | **Исправлено** (без переписывания истории) | b990dfb |
| 18 | P2 | Персональные данные в **публичной** истории (см. §10) | **Открыто**: нужно решение владельца | — |
| 19 | P3 | Диапазон зарплаты «zwischen 42.000 und 48.000 €» читается только как верхняя граница | Открыто (backlog) | — |
| 20 | P3 | Логин и пароль банковского портала есть в локальном журнале сессии Claude (не в репозитории и не в пакетах) | Открыто (информационно) | — |

## 3–6. Что воспроизведено, исправлено, уже работало, осталось

- **Воспроизведено до исправления:** 1, 2, 5, 6, 9, 10, 12, 14 (тестами и на реальных данных в режиме чтения), 11 (вручную, нет адреса и точности), 17.
- **Уже было исправлено (доказательство):** 7 — `check-job 2354` даёт SAME_REFERENCE и EXACT_DUPLICATE. 13 — запрос к профилю в режиме чтения.
- **Осталось:** 18 (P2, решение владельца), 19 (P3), 20 (P3). Гейты пройдены (§9–11).
- Пункты 11, 12, 14 дополнительно уточнялись в раундах Codex 5–19 (см. §11, §13).

## 7. Изменённые файлы

**Код приложения:**
- `app/agents/email_classifier.py`
- `app/services/application_correlation.py`
- `app/services/application_response_processor.py`
- `app/services/application_duplicates.py`
- `app/services/application_guard.py`
- `app/agents/job_fit_signals.py`
- `app/services/job_fit_assessment.py`
- `app/core/database_target.py`
- `app/db/job_commute_repository.py`

**Новые модули:**
- `app/agents/workplace_address.py`
- `app/providers/geocoding/nominatim.py`
- `app/services/pre_submit_validation.py`

**Скрипты:**
- `scripts/stage15_application_tracker.py`
- `scripts/stage15_import_ald_application.py`
- `scripts/stage16_telegram_operator.py`
- `scripts/stage17_candidate_config.py`
- `scripts/stage17_job_fit.py`
- новый `scripts/presubmit_check.py`

**Прочее:**
- `CLAUDE.md`: раздел «Browser submission gate» — `presubmit_check.py` обязателен для браузерного агента
- `docs/DAILY_OPERATIONS.md`: шаг 7.4 (обязательная проверка), §7a (коды выхода 0/3/4/2), §8 (результат проверки в evidence)
- `.gitignore`
- `pyproject.toml`: новая зависимость `pypdf>=5.4`, нужна для чтения текста PDF
- тестовые фикстуры: `tests/test_stage15_outbound_messages.py`, `tests/test_scheduler_poll_loop.py`

**Схема БД не менялась, миграций нет.**

## 8. Новые тесты (320)

| Файл | Тестов | Что проверяет |
|---|---|---|
| `test_night_audit_response_regressions.py` | 40 | реальный отказ через портал, квитанция JOIN, неформальные отказы, «решение ещё не принято», «keine Absage», SENDER, разные вакансии одного работодателя, псевдонимы |
| `test_night_audit_duplicates.py` | 29 | ALD, N.O.C, Jobriver, зеркала Akkodis/Hays, разные написания, отсутствие ложных блокировок |
| `test_night_audit_salary.py` | 34 | Zulage, Bonus, Weihnachts- и Urlaubsgeld, ×13/×14, «bis zu», почасовая оплата, TVöD, оценки площадок |
| `test_night_audit_cli_database_guard.py` | 23 | бэкап, пилот, тестовая роль, PostgreSQL-имена, жёсткая ссылка, таблица ролей |
| `test_night_audit_language_fit.py` | 52 | уровень немецкого по утверждениям: обязательный важнее желательного, «und»/запятые, диапазоны, «mind.», общие уровни «Deutsch und Englisch», Führerschein C1 |
| `test_night_audit_geo.py` | 75 | адрес из объявления, метки Arbeitsort/Sitz, Nominatim (улица, номер, индекс, город, кэш с версией), устаревший маршрут, город маршрута, CLI |
| `test_night_audit_presubmit.py` | 70 | чужой пакет, подмена PDF, страницы (в т.ч. object streams), привязка, контакты, завышение немецкого (по упоминанию, владелец строки, курсы), доверие к уровню профиля, обязательность шага |

Для каждого раунда исправлений новые тесты проверены на старом коде через `git stash`: падают до исправления, проходят после.

## 9. pytest и Ruff

- **Полный pytest на HEAD 6fe95e8: PASS.** 22 последовательные пачки по 8 файлов, отдельные каталоги `--basetemp`:
  **6539 passed, 0 failed, 4 skipped**. Пропуски — `test_stage12_human_review_gate` (symlink в Windows без прав администратора), к ветке не относятся.
  - Прогоны прерывал Claude Code из-за нехватки памяти; каждый раз продолжен с сохранённого `state.json`, пройденные пачки не повторялись.
- Ранее: 6077488 — 6536 passed / 0 failed / 4 skipped; ce26fc9 — 6444 / 0 / 4.
- **Ruff** lint и format: без замечаний на всех 419 отслеживаемых Python-файлах (`ruff check .`, `ruff format --check` по `git ls-files`).

## 10. PostgreSQL 16 gate

**PASS на HEAD 6fe95e8.** Новый контейнер `postgres:16` (16.15), отдельная тестовая БД на порту 55493, удалён после тестов.

- `alembic upgrade head` на пустой базе: один head `b6c1e9a4d2f7`.
- 17 модулей CI по порядку, каждый в своём процессе pytest: **119 тестов, 0 skipped, 0 failed**.
- Перед миграцией скрипт проверял, что `DATABASE_URL` указывает на тестовый контейнер, а не на production.
- Ранее на 6077488 и ce26fc9 тот же результат (119/0/0).

## 11. Codex Review

| Раунды | Вердикт | Найдено | Итог |
|---|---|---|---|
| 1–4 | REJECT | 21 (4 P1, 17 P2) | все исправлены (0e164c1 … a325f42) |
| 5 | REJECT | 3 P2 (страницы PDF, неполная привязка, устаревший маршрут) | ce26fc9 |
| 6–8 | REJECT | 7 P2 + 1 P3 (номер дома 5/50, вторая фраза отказа, уровень немецкого) | 46fd6f5, 63f5f75, 037b1d9 |
| 9–13 | REJECT | 19 P2 (общий уровень языков, метки адреса, геокодер: номер, улица, индекс, город, кэш; присвоение уровня в presubmit) | 7a41959 … 600b10d |
| 14–18 | REJECT | 12 P2 (завышение немецкого в PDF: слова между, владелец строки, описания; Frankfurt (Oder)) | 7396157 … 6077488 |
| 19 | REJECT | 1 P2: строки CV «Deutsch: B1» + «Muttersprache: Arabisch», склеенные извлечением текста, давали ложный BLOCKED | 6fe95e8 |
| 20 | **ACCEPT** | раунд 19 FIXED; новых P0/P1/P2 нет | — |

- Codex работал только на чтение; его песочница не запускала команды, тесты выполнял оператор.
- С раунда 9 находки — граничные формулировки трёх эвристик (уровень немецкого в вакансии, геокодер, проверка PDF). Каждая проверялась на реальных данных:
  2888 вакансий из копии runtime-БД (все изменения результатов просмотрены, это исправления) и 26 PDF реальных пакетов (ложных срабатываний 0;
  один из промежуточных вариантов давал 18 ложных блокировок на «B2-Kurs laufend» и был отброшен до коммита).
- Решение по ревью: маршрут, который оператор записал вручную (`record-commute`), остаётся утверждением оператора — это контракт Stage 17 (`test_cli_record_commute_and_json_assessment`), тест не менялся.

## 12. Production SQLite до и после

| | До | После (28.10, после всех гейтов) |
|---|---|---|
| Bewerbungen / события / вакансии | 15 / 27 / 2888 | 15 / 27 / 2888 |
| Статусы | 10 SUBMITTED, 4 RECEIVED, 1 REJECTED | без изменений |
| Alembic / integrity / FK | b6c1e9a4d2f7 / ok / нарушений нет | без изменений |
| Содержимое таблиц относительно исходной копии | — | **все 47 таблиц совпадают** (чтение `mode=ro`, хеш строк каждой таблицы) |

- Писем не отправлено, новых Bewerbungen нет, статусы и профиль не менялись, черновики Gmail не трогались.
- Пробные прогоны (`process-gmail`, сравнение 2888 вакансий) делались на копиях БД в scratchpad.

## 13. Коммиты и ветка

Ветка `maintenance/night-audit-2026-10-27` (локальная), от b94f0e0:

```
86ad84b fix(stage15): classify real rejection/receipt wordings, add SENDER correlation
734f9a3 fix(stage15): grade duplicates, catch agency re-wordings and spelling variants
c0a36cb fix(stage17): salary components, 13th salary, hourly rates, estimates
a3e60c7 fix(stage17a): database roles bind the CLI tools; document safe production start
badb4e6 fix(stage17): German requirement basis; inferred level is a gap to verify
911b889 feat(stage17): workplace address, geocoding with provenance, approximate routes
cb9a557 feat: read-only pre-submit package validation (wrong-package guard)
b990dfb chore(security): drop real addresses from test fixtures; ignore .playwright-mcp
0e164c1 fix: address Codex night-audit review round 1 (2 P1, 7 P2)
75593c2 fix(presubmit): bind each PDF to its source; stricter PDF structure (Codex P1)
c931906 fix: address Codex night-audit review round 2 (2 P1, 3 P2)
351425e fix: address Codex night-audit review round 3 (2 P1, 2 P2)
a325f42 fix: address Codex night-audit review round 4 (3 P2)
725061a docs: night audit report 2026-10-27
ce26fc9 fix: address Codex night-audit review round 5 (3 P2) + mandatory presubmit gate
46fd6f5 fix: address Codex night-audit review round 6 (3 P2, 1 P3)
63f5f75 fix: address Codex night-audit review round 7 (2 P2)
037b1d9 fix: address Codex night-audit review round 8 (2 P2): per-statement German levels
7a41959 fix: address Codex night-audit review round 9 (4 P2)
989d31a fix: address Codex night-audit review round 10 (3 P2): no false exact routes
cfa1ff5 fix: address Codex night-audit review round 11 (5 P2): another place is never a route
4750d75 fix: address Codex night-audit review round 12 (4 P2)
600b10d fix: address Codex night-audit review round 13 (3 P2)
7396157 fix: address Codex night-audit review round 14 (2 P2)
8373d13 fix: address Codex night-audit review round 15 (2 P2): German claims per mention
7d996a8 fix: address Codex night-audit review round 16 (3 P2)
a6960cb fix: address Codex night-audit review round 17 (3 P2)
6077488 fix: address Codex night-audit review round 18 (2 P2)
6fe95e8 fix: address Codex night-audit review round 19 (1 P2): CV rows joined by pdf_text
```

(плюс коммит с этим отчётом)

## 14. PR

**Не создан.**
- `gh` не авторизован (`gh auth login`).
- Ветка не запушена: push и PR — только с отдельного разрешения владельца.
- Команды после разрешения: `git push -u origin maintenance/night-audit-2026-10-27`, затем PR без merge.

## 15. Остаточные риски

1. **Персональные данные в публичной истории GitHub** (P2, решение владельца):
   - рабочий email кандидата в тестовой фикстуре коммита 784ee2c, уже в `main` (в ветке заменён);
   - город и PLZ проживания в ранних коммитах PR #11 и в remote-ветке `feat/stage-17-candidate-search-config`;
   - телефона, улицы, координат и паролей в истории нет. История не переписывалась (force push запрещён).
2. **Эвристики на открытом немецком тексте** (P3): уровень немецкого в вакансии, проверка завышения в PDF и сопоставление городов
   разбирают реальные формулировки, но не любые. Модель городов знает только уточнения «am Main»/«(Oder)» и префиксы Bad/Sankt/Neu/…
   Проверка перед отправкой — опора для обязательного ручного просмотра PDF оператором, а не замена ему.
3. Новая зависимость `pypdf`: нужен `pip install -e .` в рабочем окружении (в `.venv` уже установлена 5.4.0).
4. Nominatim и OSRM — публичные сервисы. Вызываются только по явной команде оператора, с кэшем и лимитом. Геокодер теперь отказывает,
   если ответ не подтверждает улицу, номер дома, индекс или город; тогда нужны координаты вручную (`--dest-lat/--dest-lon`).
5. SENDER ничего не записывает. Письма «только по отправителю» требуют ручного разбора (`NEEDS_OPERATOR_REVIEW`), это намеренно.
6. Старые пакеты документов не имеют `_manifest.json`, поэтому проверка показывает PROFILE_VERSION_UNRECORDED (NEEDS_OPERATOR), пока не выполнен `--bind`.
7. Диапазон зарплаты «zwischen X und Y» (P3) и данные входа банковского портала в локальном журнале сессии Claude (P3, вне репозитория) — открыты.

## 16. Заключение

**READY FOR MERGE** — для кода на `6fe95e8`; коммит с этим отчётом меняет только `docs/reports/`.

Доказательства (все на HEAD 6fe95e8):
- полный pytest: 22/22 пачки, **6539 passed, 0 failed**, 4 skipped (только symlink в Windows);
- PostgreSQL 16: новый контейнер, `alembic upgrade head` → один head `b6c1e9a4d2f7`, **119 тестов, 0 skipped, 0 failed**;
- Ruff lint + format: 419 отслеживаемых файлов без замечаний;
- Codex раунд 20: **VERDICT: ACCEPT**, без подтверждённых P0/P1/P2;
- production SQLite: 15 / 27 / 2888, integrity ok, FK ok, head `b6c1e9a4d2f7`, **все 47 таблиц совпадают** с исходной копией.

Условия и что не сделано:
1. Push, PR и merge не выполнялись — только с отдельного разрешения владельца (`gh auth login` для PR).
2. Открытый P2 вне кода ветки: персональные данные в уже опубликованной истории (§15.1) — решение владельца; merge этой ветки его не ухудшает (ветка убирает email из фикстур).
3. Эвристики на немецком тексте — остаточный риск P3 (§15.2); проверка перед отправкой не заменяет ручной просмотр PDF.
4. В рабочем окружении нужен `pip install -e .` (новая зависимость `pypdf`).

