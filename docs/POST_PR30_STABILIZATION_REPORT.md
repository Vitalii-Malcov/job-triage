# POST-PR30 STABILIZATION REPORT

Дата: 2026-11-06. База кода: `main` @ `d0f576e` (PR #30 влит). Рабочая SQLite: `runtime/job_triage_runtime.db`, Alembic `d6e9a2b4c8f1`.

**ИТОГ: `PRODUCTION CACHE UNCHANGED`. `READY FOR OPERATOR APPROVAL — NO PRODUCTION CACHE WRITE`** (при условии, что сначала влит PR #32).

Обозначения: `VERIFIED` — проверено и подтверждено; `PENDING OPERATOR APPROVAL` — ждёт вашего решения; `BLOCKED` — заблокировано.

## 1. Состояние рабочей базы — VERIFIED

| Показатель | Значение |
|---|---|
| HEAD `main` | `d0f576e` = `origin/main`; PR #30 MERGED, PR #31 OPEN (документация) |
| SHA-256 рабочей БД (префикс) | `aef7383ef194fd6a` — не менялся с момента миграции |
| `PRAGMA integrity_check` | ok (открытие `mode=ro`) |
| Alembic | `d6e9a2b4c8f1` |
| Вакансии / Bewerbungen / события | 3 234 / 25 / 54 — без изменений |
| `job_fit_assessments` | 3 234 строки, все устарели |
| Процессы JobTriage | не запущены |
| Автоматизация | `OUTBOUND_SENDING_ENABLED=false`, `AUTOMATION_SCHEDULER_ENABLED=false`, `AUTOMATION_AUTO_PREPARE_ENABLED=false`, Gmail/follow-up циклы выключены |

Все эксперименты проводились на копиях БД. Рабочая БД ни разу не открывалась на запись.

## 2. Причина изменения 16 (на выборке 400) / 154 (на всех 3 234) оценок — VERIFIED

- Старый код (`78a947b`, до PR #30) и новый (`d0f576e`) дают **одинаковые** `(score, recommendation)` для **всех 3 234** вакансий. Регрессии PR #30 нет.
- Различаются 154 сохранённых значения `jobs.score/recommendation` и свежий пересчёт. У всех 154 `last_seen_at` раньше 2026-10-27 14:49 — момента профиля кандидата v5 (подтверждены навыки Pydantic, Django REST Framework, AWS, Terraform, AI/LLM integrations).
- Контрфактический тест: без этих 5 навыков все 154 значения воспроизводятся точно; вакансии, увиденные после v5, совпадают с текущим профилем; строк, не совпавших ни с чем, нет.
- Причина: оценка пересчитывается при повторном сборе вакансии, а не при изменении профиля, поэтому нетронутые вакансии хранят «доv5» оценки.
- Классификация: ожидаемое следствие изменения профиля (устаревшие сохранённые данные), не ошибка кода.

## 3. Переход SKIP → APPLY — VERIFIED

Вакансия id 38 («Data Engineer», обязательные AWS/Python/SQL, последний раз видена 22.10): было 58 SKIP, при пересчёте 82 APPLY — потому что AWS стал навыком кандидата.
Оценка пригодности (`fit_tier`) у неё `NOT_SUITABLE` (требуется опыт 2+ года). Рекомендация в `jobs` в рабочей БД остаётся 58 SKIP, пока не будет отдельного решения.

Замечание: у навыков AWS/Terraform/AI-LLM в профиле есть примечание «learning/project experience only — not commercial». Скоринг учитывает только имена подтверждённых навыков и примечание игнорирует (политика проекта, не дефект реализации по оценке Codex). Из 17 вакансий с APPLY по баллам 4 опираются на AWS; 3 из них `NOT_SUITABLE`, 1 `STRETCH`.

## 4. Аудит всех 3 234 вакансий (без записи в рабочую БД) — VERIFIED

Матрица рекомендаций (сохранённая → свежая): SKIP→SKIP 2 717, MAYBE→MAYBE 225, NEEDS_ENRICHMENT→NEEDS_ENRICHMENT 268, APPLY→APPLY 16, **SKIP→MAYBE 7, SKIP→APPLY 1**. Переходов вниз (APPLY→SKIP и т. п.) нет.
Изменились баллы у 154 вакансий, рекомендация — у 8. Сегменты (по заголовку): Junior 270 / изм. баллов 6, Python 57 / 5, Backend 71 / 11, Django 3 / 1, Automation 172 / 8, Senior 574 / 55; рекомендаций, сменённых в этих сегментах, нет. Среди 17 APPLY ни одной вакансии со Senior-названием. Вакансии, связанные с Bewerbungen (17), — **без изменений**.

Кэш `job_fit_assessments` (пересчёт на копии): 169 изменений уровня, все в сторону `NOT_SUITABLE` (до фикса #32): 168 — критерий роли `OTHER/SECONDARY → OFF_ROLE` (правило «IT-поддержка/администрирование — не целевая роль», добавлено 2026-11-01 21:35, после заполнения кэша в 16:22), 1 — вакансия 2892 стала `ALREADY_APPLIED` (заявка №24, REJECTED 04.11).
Распределение уровней после пересчёта (с фиксом #32): NOT_SUITABLE 2 148, NEEDS_OPERATOR 935, STRETCH 140, PARTIAL_MATCH 11, GOOD_MATCH 0.

Почему устарели все 3 234: ключ кэша включает хеш исходников модулей правил (`fit_cache.compute_rules_hash`). Любая правка кода (в т. ч. комментарии и рефакторинг) инвалидирует всё; кэш был устаревшим и **до** PR #30 (старый код тоже показывает stale=3234 и строит байт-в-байт тот же результат).

## 5. Найденные ошибки и исправления — VERIFIED

**Дефект D1 (исправлен, PR #32, не влит):** заголовки «IT-Administratoren», «IT-Security Administratorinnen», «Systemadmin Linux», «Anwendungssupporter IT», «Mitarbeiter Client Support» не попадали в семейство «поддержка/администрирование» (множественное число, сокращения, составные слова) и получали роль `OTHER` (NEEDS_OPERATOR) вместо `OFF_ROLE`. Исправление — регулярные выражения в `app/agents/target_role.py`; на всех 3 234 заголовках флагуется ровно на 6 больше (268→274), все верные. Исключение для разработческих ролей не менялось. Codex: ACCEPT (линейная сложность подтверждена на 128 000 символов).

**Дизайн-замечание D2 (НЕ исправлено, решение оператора):** `app/services/shortlist_gate.py` (читает fit-tier) вызывается только из тестов. Автоматический шорт-лист (`automation_shortlist.py`) отбирает по `jobs.recommendation` (APPLY/MAYBE) и баллу, не учитывая `fit_tier`; docstring `FitAssessment._tier`: «A tier never blocks anything». При включённом `AUTOMATION_AUTO_PREPARE_ENABLED` (сейчас выключен) `NOT_SUITABLE` вакансия теоретически может попасть в подготовку черновика (отправки нет, нужно одобрение человека). Подключение — изменение поведения автоматизации, я его не делал.

**Наблюдение D3:** поле `decision` (`BLOCKED/NEEDS_VERIFICATION/CANDIDATE`) — устаревшая сущность, не согласованная с `fit_tier`: 36 вакансий имеют `decision=CANDIDATE` при `NOT_SUITABLE`. Единственная истина для фильтрации — `fit_tier`.

## 6. Изменённые файлы и коммиты

- PR #32 `fix/post-pr30-role-family-inflections` (2 коммита): `app/agents/target_role.py`, `tests/test_stage24_target_role.py`. Миграций нет.
- Этот отчёт: `docs/POST_PR30_STABILIZATION_REPORT.md` (ветка `docs/post-pr30-stabilization-report`).
- Никаких пользовательских файлов в коммитах нет (проверено списком файлов диффа).

## 7. Результаты тестов — VERIFIED (с оговорками)

| Прогон | Результат |
|---|---|
| Полный pytest, `d0f576e`, PostgreSQL 16 (docker) | **7 964 passed, 0 failed, 6 skipped** (48 мин 29 с) |
| Полный pytest, ветка #32 | 7 978 passed, **1 failed**, 6 skipped (59 мин) |
| Ruff check / format | чисто на обеих ветках |
| PostgreSQL | в обоих прогонах выполнено 198 тестов PG-модулей |

Skips (6): 4 — права на symlink в Windows, 1 — нет локальной канонической БД, 1 — alias symlink.
**Падение на ветке #32:** `test_write_document_never_overwrites_outside_a_temp_dry_run_dir` проверяет, что `<repo>/docs` не лежит в системном temp; мой worktree находился внутри `%TEMP%`. С `TEMP`/`TMP` вне worktree весь файл проходит (55/55). Файлы теста и экспорта в #32 не менялись. Это артефакт расположения, но полный прогон ветки #32 «зелёным» буквально не получен; после слияния #32 в `main` (в обычном каталоге) тест будет исполнен в штатных условиях.

Проверки Stage 2 (429, Telegram, Gmail, startup) закрыты **тестами пакета**: `test_auth_rate_limit_ordering.py` (429 после серии неверных ключей), `test_sender_authentication.py`, `test_response_draft_send_*.py`, `test_stage16_telegram_operator_control.py`, `test_telegram_bot.py`, `test_startup_diagnostics.py`, `test_shared_state.py` и тесты single-process lock. Отдельных ручных сценариев сверх пакета и предыдущего smoke-прогона 17/17 я **не запускал**.

## 8. Репетиция cache rebuild на копии — VERIFIED

Скрипт `scripts/stage21_assess_vacancies.py --apply` на свежей копии: записано 3 234 строки; хеши остальных 49 таблиц до и после идентичны (меняется только `job_fit_assessments`); повторный запуск пишет 0 строк (идемпотентно). `jobs.score` и `jobs.recommendation` **не затрагиваются**. Код #32 отличается от кода `main` ровно в 6 строках (id 1724, 1727, 2231, 2279, 2363, 2643: NEEDS_OPERATOR→NOT_SUITABLE).

## 9. Сохранность Bewerbungen — VERIFIED

25 заявок (RECEIVED 8, REJECTED 9, SUBMITTED 8) и 54 события не менялись ни в одном эксперименте; хеши таблиц `applications`/`application_events` совпадают до и после; внешних отправок не было; коллекторы не запускались.

## 10. Вердикт Codex

- Root cause (оценки и кэш): `EXPECTED_POLICY_CHANGE` по обоим явлениям.
- Код #32: `ACCEPT`.
- Финальный гейт: **`READY_FOR_CACHE_REFILL`** — после слияния #32. Codex проверил самостоятельно: состояние git, БД, репетиционные копии, JUnit, Ruff. **Не смог проверить:** повторный прогон 55/55, защищённую резервную копию (нет доступа), поведение запущенных процессов, живые Stage 2 сценарии сверх пакета тестов.

## 11. Что осталось сделать перед полноценным запуском

1. `PENDING OPERATOR APPROVAL`: ревью и слияние PR #32; затем (отдельно) разрешение на refill кэша.
2. `PENDING OPERATOR APPROVAL`: решение по D2 (подключить `shortlist_gate`/`fit_tier` к автошорт-листу) — до включения автоматизации.
3. `PENDING OPERATOR APPROVAL`: решение, нужно ли обновлять сохранённые `jobs.score/recommendation` для 154 вакансий (отдельная операция, не входит в refill; Codex рекомендует превью и точечное обновление).
4. Не выполнено: обычный запуск с реальными учётными данными; тест реального Gmail на отдельном ящике (gate перед включением отправки); реальный Telegram. `BLOCKED` до вашего разрешения.
5. Пересмотреть политику навыков «learning only» (AWS/Terraform/LLM) — вопрос политики скоринга.

## 12. Нужен ли пересчёт production-кэша и что изменится

Да, нужен: 3 234 из 3 234 записей устарели (UI показывает устаревшие уровни). Пересчёт изменит **только** таблицу `job_fit_assessments` (3 234 строки: fit_tier, decision, criteria_json, work_mode, applied, rules_hash, computed_at). Ожидаемо: уровни 169+6=175 вакансий сменятся на NOT_SUITABLE, 1 вакансия получит `ALREADY_APPLIED`; итоговое распределение — см. раздел 4. Таблицы `jobs`, `applications`, `application_events` и остальные не меняются.

План выполнения (после вашего подтверждения и слияния #32):

1. Остановить процессы JobTriage; обновить `main`.
2. Заново проверить резервную копию `C:\Users\V\JobTriage-Backups\job_triage_runtime.pre-cache-refill.20261106T124137Z.db` (SHA-256 `6d0b2be770f4da4f8c97bdea6b7dd4661496a7bfb34e8b4aecf8e1ce72e4f5a3`, integrity ok).
3. Сухой прогон: `python scripts/stage21_assess_vacancies.py --database runtime/job_triage_runtime.db` → ожидается `stale=3234`.
4. Выполнение: `python scripts/stage21_assess_vacancies.py --database runtime/job_triage_runtime.db --apply --snapshot C:\Users\V\JobTriage-Backups\refill-snapshots`.
5. Контроль: повторный сухой прогон `fresh=3234 stale=0`; хеши всех таблиц кроме `job_fit_assessments` равны сохранённым; 25 заявок / 54 события / 3 234 вакансии; `integrity_check ok`; Alembic `d6e9a2b4c8f1`; распределение уровней как в репетиции.
6. Откат: восстановить **только** таблицу `job_fit_assessments` из снимка/резервной копии (полное восстановление файла потеряло бы более поздние изменения).
