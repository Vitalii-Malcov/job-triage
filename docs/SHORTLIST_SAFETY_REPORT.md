# JobTriage — Shortlist Safety и строгий фильтр вакансий разработчика

Дата: 2026-11-06. Ветка: `feat/shortlist-fit-safety` (поверх PR #32). Production-БД и кэш **не менялись**.

## 1. Главное

**`NOT_SUITABLE` больше не попадает в автоматический shortlist, в автоматические CV/Bewerbung-черновики и в автоматические предложения в Telegram.** Раньше автоматический отбор смотрел только на `jobs.recommendation` (APPLY/MAYBE) и баллы. Оценка пригодности `fit_tier` в рабочем пути не использовалась: код `shortlist_gate.py` вызывался только из тестов.

## 2. Какие вакансии теперь исключаются по названию

Правила `app/agents/target_role.py` (PR #32 плюс дополнение этой ветки) относят к «не разработка» такие названия:

- IT-Administrator / IT-Administratoren / Administratorinnen, Systemadmin(istrator), Netzwerkadministrator, Windows-/Linux-Administrator, IT-Infrastruktur-Administration.
- IT-Support, Application Support, Client Support, Anwendungssupporter, Technischer Support.
- First-/Second-/Third-Level, 1st/2nd Level.
- Helpdesk, Service Desk, Hotline.
- IT-Systemelektroniker/in (новое в этой ветке; только профессия, не слово «Systemelektronik»).
- Fachinformatiker Systemintegration, Systemintegrator.

Не исключаются: Junior Python / Backend / Software Developer, FastAPI / Django, Python Automation, Trainee Softwareentwicklung, Junior API Developer и «Backend Entwickler mit Docker und Linux». Linux, Docker, AWS и поддержка как побочная обязанность вакансию не исключают: проверяется название роли. Всё закреплено тестами `tests/test_stage24_target_role.py`.

Известное ограничение: гибридные названия вроде «IT-Administrator mit Entwicklungsaufgaben» и «Fachinformatiker Systemintegration / Anwendungsentwicklung» исключаются, как и до этой работы. Исключение для разработчика действует только при явном слове «Entwickler/Developer» в роли.

## 3. Правила для оценки пригодности

| Уровень `fit_tier` | Автоматический shortlist и черновики | Автоматические предложения в Telegram |
|---|---|---|
| `NOT_SUITABLE` | запрещено | запрещено |
| `NEEDS_OPERATOR` | запрещено до решения оператора | разрешено: оператор как раз должен посмотреть |
| `STRETCH` | запрещено | разрешено |
| `PARTIAL_MATCH`, `GOOD_MATCH` | разрешено при остальных условиях отбора | разрешено |
| неизвестный уровень, ошибка оценки | запрещено (fail-closed) | запрещено |

В системе нет уровня `SUITABLE`; ему соответствуют `GOOD_MATCH` и `PARTIAL_MATCH`. Разрешение не означает одобрение отправки: Bewerbung по-прежнему отправляется только по явному решению оператора.

Единый источник правила: `app/services/shortlist_gate.py` (`AUTO_PREPARE_TIERS`, `automatic_block_reason`, `automatic_preparation_block`, `confirmed_barrier`).

Свойства проверки:

- **Свежая оценка.** `assess_job` считается заново, не из кэша `job_fit_assessments`: устаревший или пустой кэш не может ни допустить, ни заблокировать вакансию.
- **Отдельное соединение.** Оценка идёт в своей короткой сессии на отдельном соединении. Её откат не затрагивает транзакцию вызывающего кода. Если соединение разделяемое (`StaticPool` в памяти или голый `Connection`), проверка блокирует вакансию.
- **Ничего не пишет.** Нет записей в БД, нет смены статусов, нет отправки.
- **Влияние `jobs.score`.** Высокий `jobs.score` и `APPLY` обойти проверку не могут.

## 4. Где проверка встроена

1. **Автоматический shortlist** (`automation_shortlist.py`):
   - проверка после подбора и до ранжирования и лимита: заблокированные вакансии не занимают места;
   - повторная проверка непосредственно перед созданием CV;
   - ещё одна проверка перед использованием существующей или созданием новой Bewerbung;
   - заблокированная вакансия не считается ошибкой: счётчик `blocked_by_fit` и запись `status="blocked"`, `phase="fit"` в результатах, с id уже созданного CV, если он был.
2. **Telegram-лента**:
   - при постановке в очередь;
   - после паузы между сообщениями, непосредственно перед отправкой: заблокированная вакансия снимается с очереди, а не отправляется;
   - ручная команда оператора `/review` остаётся доступной, но ответ содержит предупреждение `NOT_SUITABLE`.
3. **Прямое уведомление коллектора** (лента отключена, режим по умолчанию): проверка перед отправкой, и ещё раз перед **каждой** повторной попыткой после backoff (`TelegramNotifier.guard`).
4. **`POST /jobs/score`**: та же проверка и тот же guard на повторах.

Сознательно не затронуто:

- Явные действия оператора: ручной `/review`, кнопка «Bewerbung erstellen» в Telegram, ручные API-вызовы.
- Follow-up и черновики ответов Gmail. Они работают с вакансиями, на которые заявка уже есть, а `ALREADY_APPLIED` по дизайну даёт `NOT_SUITABLE`. Проверка заблокировала бы ровно легитимный случай. Они создают текст для оператора, не выбирают вакансии и не готовят CV/Bewerbung. Codex подтвердил этот вывод.

Статусы заявок, события, отправленные Bewerbungen и история не изменяются никаким новым кодом.

## 5. Результат на копии рабочей БД (3 234 вакансии)

Копия сделана через SQLite backup API с источника в режиме `mode=ro`. Хеш файла production-БД до и после: `aef7383ef194fd6a`. Оценка выполнена по действующему коду, без записи.

- **Уровни пригодности (новый код):** `NOT_SUITABLE` 2 148, `NEEDS_OPERATOR` 935, `STRETCH` 140, `PARTIAL_MATCH` 11, `GOOD_MATCH` 0. Ошибок оценки нет.
- **Названия с признаком поддержки или администрирования:** 274 (268 до PR #32). Правило IT-Systemelektroniker на этих данных ничего нового не добавило.
- **Изменение уровня по сравнению с `main`:** ровно 6 вакансий, все с `NEEDS_OPERATOR` на `NOT_SUITABLE`, и все верно (IT-Administratoren, Systemadmin Linux, IT-Security Administratorinnen ×2, Client Support, Anwendungssupporter).
- **Подходящие по статусу и рекомендации под автоотбор:** 241 вакансия (статус NEW/SAVED, рекомендация APPLY/MAYBE). Из них **136 с `NOT_SUITABLE`** могли раньше попасть в автоматический отбор.
  - Остальные: `NEEDS_OPERATOR` 89, `STRETCH` 13, `PARTIAL_MATCH` 3.
  - Проверку проходят 3; 238 из 241 удерживаются. Число для `NEEDS_OPERATOR` завышено: в копии у многих нет свежего match-анализа, а при реальном запуске match считается раньше проверки.
- **Кандидаты на Telegram-карточку** (APPLY, статус NEW, без учёта порога балла): 16, из них 7 с `NOT_SUITABLE`.
- **Ложные исключения:** среди названий с признаком «Entwickler/Developer/Software» (15) все — гибриды вида «Support Spezialist Software», «Second Level Software Support», «Linux Administrator». Чистых вакансий Junior Python / Backend среди исключённых нет.
- **Репетиция cache refill на отдельной копии:**
  - сухой прогон: `stale=3234`;
  - применение: записано 3 234 строки;
  - повторный сухой прогон: `fresh=3234 stale=0`;
  - изменилась **только** таблица `job_fit_assessments`, остальные 49 таблиц идентичны по хешу содержимого;
  - заявок 25, событий 54, вакансий 3 234 — без изменений;
  - `integrity_check` = ok.

## 6. Тесты

Полный прогон на ветке (PostgreSQL 16, рабочая копия вне системного TEMP):

- pytest: **8 031 passed, 0 failed, 6 skipped**.
- Ruff `check` и `format --check`: чисто.
- Alembic: одна голова `d6e9a2b4c8f1`, `upgrade head` на тестовой PostgreSQL проходит. Схема не менялась, миграций нет.
- 6 пропусков такие же, как на `main`:
  - 4 — символьные ссылки недоступны на этой ФС;
  - 1 — нет локальной канонической БД;
  - 1 — символьные ссылки без прав в Windows.

Новые тесты:

- `tests/test_shortlist_fit_safety.py` (30 тестов с параметризацией): все уровни, неизвестный уровень, сбой оценки, изоляция соединения на реальном SQL, повторные проверки, отсутствие записей.
- Дополнения в `tests/test_automation_shortlist.py`, `tests/test_telegram_vacancy_feed.py`, `tests/test_collector_runner_lease_guard.py`, `tests/test_job_score_endpoint_recommendation_gated_notification.py`, `tests/test_stage24_target_role.py`.

Замечание: во время работы я дважды прервал полный прогон ради исправлений. Это оставило тестовую PostgreSQL в неконсистентном состоянии, дало ложные падения интеграционных тестов. Я пересоздал схему тестового контейнера `jt-test-pg` (`DROP SCHEMA public`, затем `alembic upgrade head`). Production и ваши данные это не затрагивало.

## 7. Вердикт Codex

| Раунд | Вердикт | Суть |
|---|---|---|
| План | CHANGES_REQUIRED | холодный старт Telegram-ленты, изоляция сессии, повторные проверки, предупреждение оператору, тесты |
| Код 1 | CHANGES_REQUIRED | проверка после паузы, прямое уведомление коллектора, повторное использование Bewerbung, изоляция соединения, слишком широкий паттерн «Systemelektronik» |
| Код 2 | CHANGES_REQUIRED | `/jobs/score`, повторы notifier, аудит заблокированных записей, реальный SQL в тесте |
| Код 3 | CHANGES_REQUIRED | follow-up и ответы Gmail |
| **Код 4 (итог)** | **ACCEPT** | пункты 1–2 раунда 3 отклонены с обоснованием (`ALREADY_APPLIED` ⇒ `NOT_SUITABLE`), Codex согласился |

Все остальные замечания исправлены и покрыты тестами. Уточнение Codex: утверждение «ответы Gmail работают только с вакансиями с заявкой» неточно — для `JOB_ONLY` и несопоставленных писем черновики возможны. Они остаются шаблонами корреспонденции и не создают CV или Bewerbung.

Codex не мог запускать полный набор тестов (read-only песочница). Полный прогон выполнен мной, результат выше.

## 8. Pull request'ы

- Исправление названий (база для этой ветки): https://github.com/Vitalii-Malcov/job-triage/pull/32
- Shortlist safety: ссылка в описании нового PR; он нацелен на ветку PR #32 (стек).
- Отчёт по стабилизации: https://github.com/Vitalii-Malcov/job-triage/pull/33
- Документы rollout: https://github.com/Vitalii-Malcov/job-triage/pull/31

Автоматически ничего не вливалось. Порядок слияния: сначала #32, затем PR shortlist safety (GitHub сменит его базу на `main`).

## 9. Что сделать перед пересчётом production-кэша

1. Просмотреть и влить #32, затем PR shortlist safety. **Правило `rules_hash` зависит от исходников модулей оценки, поэтому любое слияние делает все 3 234 строки кэша устаревшими.** Пересчёт делать только после слияния и обновления `main`.
2. Остановить процессы JobTriage, заново проверить резервную копию `job_triage_runtime.pre-cache-refill.20261106T124137Z.db` (SHA-256 `6d0b2be770f4da4f8c97bdea6b7dd4661496a7bfb34e8b4aecf8e1ce72e4f5a3`) и при необходимости сделать новую.
3. Сухой прогон: `python scripts/stage21_assess_vacancies.py --database runtime/job_triage_runtime.db`. Ожидается `stale=3234`.
4. Только после отдельного разрешения: тот же вызов с `--apply --snapshot C:\Users\V\JobTriage-Backups\refill-snapshots`.
5. Контроль: затронута только `job_fit_assessments`, заявок 25, событий 54, вакансий 3 234, `integrity_check` ok, распределение уровней совпадает с репетицией выше.
6. Откат: восстановить только таблицу `job_fit_assessments` из снимка.

Решения, оставленные вам:

- Обновление `jobs.score` для 154 вакансий с устаревшей оценкой (отдельная операция).
- Политика AWS «только учебный опыт».
- Включение автоподготовки (`automation_auto_prepare_enabled`) остаётся выключенным. Теперь оно безопаснее, но решение за вами.
- Реальный Gmail, Telegram и запуск с рабочими учётными данными по-прежнему не проверены без вашего разрешения.

## 10. Запреты, которые соблюдены

Рабочая SQLite не изменялась (хеш `aef7383ef194fd6a`). Кэш не пересчитывался, `jobs.score` не обновлялся. Статусы заявок не менялись. Gmail, Telegram и Bewerbungen не отправлялись. Автоотправка не включалась. `.env`, секреты и резервные копии не тронуты. Файлы пользователя в коммиты не включены (явные пути в `git add`), force push не использовался, `main` не менялся.

**Вердикт: READY FOR OPERATOR REVIEW — NO PRODUCTION WRITE**
