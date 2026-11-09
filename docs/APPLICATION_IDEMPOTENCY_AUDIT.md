# Аудит идемпотентности подготовки Bewerbung (Stage 55)

Дата: 2026-11-09. База: PR #54 (`fix/work-time-gate-confirmation`, `86dc6e6`). Ветка: `fix/application-idempotency`.
Production не менялась. Все эксперименты — на копиях и во временных SQLite-файлах тестов.

## 0. Вывод

Подтверждены и исправлены пять дефектов. Главный: автоматическая подготовка **не удерживала вакансию, по
которой уже есть неотправленная заявка** (`duplicate = IN_PREPARATION`). Ещё четыре нашёл независимый
аудит Codex (раунд 1) в операторских путях, не в автоматике: регистр `source` в идентичности заявки,
заявка без `source_reference`, Telegram APPROVE без проверенной вакансии, ручной эндпоинт CV для уже
поданной вакансии (§2a). Принят один остаточный риск: два воркера могут оставить два локальных черновика
письма для одной вакансии. Повторной реальной Bewerbung, второй активной заявки автоматикой или отправки
ни один найденный путь создать не может.

| Вопрос | Ответ |
|---|---|
| Создаёт ли автоматический код `ApplicationRecord`? | **Нет.** `create_application` вызывают только два операторских CLI-скрипта |
| Может ли автоматика отправить Bewerbung? | **Нет.** Отправляющих путей два (ответ и follow-up), оба за approval, закреплённым получателем и `OUTBOUND_SENDING_ENABLED=false` |
| Что было небезопасно | `IN_PREPARATION` не закрывал gate: автоподготовка создавала CV и письмо для вакансии с заявкой в работе |
| Что исправлено | gate допускает только записанное `duplicate = CLEAR` (`APPLICATION_IN_PROGRESS`); идентичность заявки канонична; APPROVE и ручной CV закрыты (§2a) |
| Что принято | гонка двух воркеров даёт ≤ 2 локальных черновика письма (см. §5) |

## 1. Точки входа подготовки и изменения заявок

| # | Путь | Кто запускает | Флаги | Проверка пригодности | Проверка существующей заявки | Что создаёт | Подтверждение человека |
|---|---|---|---|---|---|---|---|
| 1 | Автоцикл `run_automation_cycle` (scheduler или `POST /automation/run`) | планировщик / оператор | `AUTOMATION_AUTO_PREPARE_ENABLED` (по умолчанию false), scheduler (по умолчанию выключен) | `automatic_block_reason` вживую, 3 раза (выбор, до CV, до письма) | gate: `ALREADY_APPLIED` → NOT_SUITABLE; **Stage 55:** `IN_PREPARATION` → hold | CV-черновик, черновик письма (локальные строки) | не требуется для черновика; заявку не создаёт |
| 2 | `POST /jobs/{id}/cv-draft`, `/bewerbung-draft` | оператор (API-ключ) | — | не применяется (ручной вызов) | `ensure_not_already_applied` в `prepare_bewerbung_draft` (SUBMITTED, RECEIVED, UNDER_REVIEW, INTERVIEW, OFFER, REJECTED …) | CV, письмо (повтор всегда вставляет новую строку — регенерация) | оператор |
| 3 | Review package API | оператор | — | — | `ensure_not_already_applied` | пакет на проверку | оператор |
| 4 | Telegram: подготовка по кнопке | оператор | `TELEGRAM_OPERATOR_CONTROL_ENABLED` и др. | `JOB_NOT_ELIGIBLE`, профиль, идентичность входа | `UNIQUE(review_id)` + CAS-claim (поколение + токен) | подготовка, письмо, превью | кнопка = человек |
| 5 | Telegram APPROVE | оператор | `TELEGRAM_OPERATOR_CONTROL_ENABLED` | статус `PACKAGE_READY` | decisions append-only, `UNIQUE(callback_ref)` | `PACKAGE_READY → APPROVED` | да |
| 6 | CLI `stage15_application_tracker` (record) | оператор | `--apply` (dry-run по умолчанию) | — | duplicate guard: STRONG/PROBABLE → отказ (exit 3) без `--confirm-distinct-from`; `UNIQUE(source, source_reference)` | **заявку** (`SUBMITTED`) | да |
| 7 | CLI обработки входящей почты | оператор | `--apply` | — | детерминированный `correlation_key` (повтор = no-op) | событие и статус RECEIVED/REJECTED для **уже отправленной** заявки | да |
| 8 | Ответ / follow-up по HTTP | оператор | `OUTBOUND_SENDING_ENABLED` (false), approval, закреплённый получатель | — | `UNIQUE` live-send uniqueness | **единственная отправка** | да |
| 9 | Gmail draft handoff | оператор | `TELEGRAM_GMAIL_DRAFT_ENABLED` (false) | точное approval Stage 9C | `UNIQUE(link_id)`, `UNIQUE(marker_message_id)` | один черновик в Drafts (не отправка) | да |

Retry и restart: путь 1 — следующий цикл идемпотентно переиспользует CV и письмо (`bewerbung_draft_is_current`);
путь 4 — зависший `PREPARING` восстанавливается в `FAILED` через 120 с (`reconcile_stale_preparation`),
следующее нажатие начинает новое поколение; путь 7 — повтор того же письма не создаёт события.

## 2. Что такое `IN_PREPARATION`

Это **не статус заявки**. Это значение критерия `duplicate` в fit-оценке: «заявка на эту вакансию есть
(совпадает строка вакансии, source reference, URL или fingerprint — сила STRONG), но не отправлена».
Статусы заявки: `DISCOVERED, MATCHED, PACKAGE_READY, APPROVED` (не отправлена) и `SUBMITTED, RECEIVED,
UNDER_REVIEW, INTERVIEW, TECHNICAL_INTERVIEW, OFFER, REJECTED …` (отправлена).

До Stage 55 `automatic_block_reason` смотрел только на fit-tier, роль и рабочее время. `ALREADY_APPLIED`
закрывал gate через tier NOT_SUITABLE, `POSSIBLE_DUPLICATE` — через NEEDS_OPERATOR, а `IN_PREPARATION`
оставлял tier GOOD_MATCH, и gate возвращал `None`.

**Воспроизведение до правки** (`tests/test_application_idempotency.py`, настоящий `duplicate_criterion` над
настоящей строкой заявки, остальные критерии фиксированы как «хорошее совпадение, Vollzeit, Remote»):

| Статус заявки по вакансии | До правки | После |
|---|---|---|
| нет заявки | подготовлена (CV 1, письмо 1) | подготовлена |
| DISCOVERED / MATCHED / PACKAGE_READY / APPROVED | **подготовлена повторно (1, 1)** | удержана, (0, 0) |
| SUBMITTED / RECEIVED / REJECTED | удержана | удержана, (0, 0) |
| та же вакансия второй строкой `jobs` (общий source reference) | **подготовлена** | удержана |
| другая вакансия того же работодателя | подготовлена | подготовлена (работодатель целиком не блокируется) |

Последствие дефекта — не вторая заявка, а лишние CV и письмо, которые могли стать «последним» черновиком
рядом с уже подготовленным пакетом. Исправление — `shortlist_gate.BLOCK_APPLICATION_IN_PROGRESS`:
после проверки рабочего времени допускается только записанное `duplicate = CLEAR`; неизвестное значение
и отсутствие критерия удерживают вакансию (fail-closed, как для `work_time`). `POSSIBLE_DUPLICATE` и
`ALREADY_APPLIED` по-прежнему закрываются tier-ом.

## 2a. Находки Codex, раунд 1 (исправлены в этом PR)

Ни одна не P0/P1 и ни одна не достижима автоматическим кодом: все — операторские пути.

| Серьёзность | Где | Сценарий | Исправление | Тест |
|---|---|---|---|---|
| P2 | `application_tracker.create_application` | два одновременных `record-submission --apply` с `Bundesagentur` и `bundesagentur` для одной ссылки: `UNIQUE(source, source_reference)` сравнивает разные строки, оба проходят | `canonical_source` (strip + casefold) в репозитории на записи и в поиске; production уже в нижнем регистре, миграция данных не нужна | `test_source_spelling_is_one_identity` |
| P2 | там же | `source_reference = NULL`: поиск пропускается, SQL UNIQUE допускает любое число NULL, две заявки на одну строку `jobs` | `source_reference` обязателен (пустое/NULL → ошибка); ни один вызов в `app/` и `scripts/` не передаёт NULL. Первый вариант (fallback `(source, job_id)`) отклонён во 2-м раунде, см. ниже | `test_an_application_without_a_reference_is_refused` |
| P2 | `telegram_operator_control.assess_package` | заявка `PACKAGE_READY` с `job_id = NULL` и `review_package_id` на пакет чужой вакансии: сверка `job_id` пропускалась, APPROVE одобрял чужой пакет | `job_id is None` → причина `NO_VERIFIED_VACANCY`, пакет не готов | `test_telegram_approval_needs_a_verified_vacancy` |
| P3 | `candidate_preparation.prepare_candidate_cv_draft` | `POST /jobs/{id}/cv-draft` создаёт CV для вакансии с SUBMITTED-заявкой (письмо уже было закрыто) | `ensure_not_already_applied` перед подготовкой; роутер отдаёт 409 | `test_a_sent_application_blocks_the_manual_cv_endpoint_too` |

**Раунд 2 (REJECT, без P0/P1).** Fallback `(source, job_id)`, добавленный мной в раунде 1, оказался хуже проблемы:
оставался гоночным, не срабатывал при последующей подаче со ссылкой и молча склеивал повторную подачу и другую
роль на той же вакансии. Удалён целиком; три P2 исчезли вместе с механизмом. Остальное исправлено:
поиск по `source` нечувствителен к регистру (строка, сохранённая старой версией как `Bundesagentur`, находится;
тест `test_a_row_stored_before_canonical_source_is_still_found`), `vacancy_identity_report` сравнивает источники
канонически. **Раунд 3: ACCEPT, замечаний нет.**

Не исправлено намеренно:

* псевдонимы источника (`ba` и `bundesagentur`) не нормализуются: списка псевдонимов в системе нет, а
  источник берётся из строки вакансии. Последовательные дубли ловит duplicate guard (совпадение ссылки
  между псевдонимами), остаётся только одновременный запуск двух CLI с разными псевдонимами;
* 8 заявок production без `job_id` теперь не могут быть одобрены через Telegram, если вернутся в
  `PACKAGE_READY`; сейчас все они терминальные.

## 3. Транзакционные и структурные гарантии

| Гарантия | Механизм | Проверено |
|---|---|---|
| одна заявка на `(source, source_reference)` | `UNIQUE uq_applications_source_reference`; `IntegrityError` → возврат существующей | тест: 4 потока → 1 строка, `[False, True, True, True]` |
| заявка не начинается терминальной | `TerminalStatusNotPermittedError` | тест |
| повтор того же события | `UNIQUE correlation_key` | существующие тесты Stage 15 |
| уникальная нумерация событий | `UNIQUE(application_id, sequence)` | существующие тесты |
| одна подготовка Telegram на review | `UNIQUE(review_id)`, CAS по `(state, generation, claim token)` | существующие тесты и PostgreSQL-набор |
| один автоцикл на аккаунт | lease `automation_runs` (`RUNNING`, продление, восстановление по истечению) | `tests/test_automation_lease.py` |
| отправленную заявку не готовят снова | `ensure_not_already_applied` + `ALREADY_APPLIED` → NOT_SUITABLE | тесты Stage 55 для SUBMITTED, RECEIVED, REJECTED |
| автоматический код не создаёт заявок | grep по `app/`: `create_application` вызывают только `scripts/stage15_*` | тест: после параллельного прогона заявок 0 |

`UNIQUE(source, source_reference)` допускает любое число NULL, поэтому сервис больше не принимает заявку без
ссылки (в production 25 из 25 со ссылкой). Остаётся, что уникальный индекс стоит на «сыром» `source`: новые
строки пишутся в каноническом виде, а строки старых версий в другом регистре индекс не склеивает; поиск их
находит. В production все источники в нижнем регистре (`bundesagentur`, `stepstone`, `xing`), миграция данных
не нужна.

## 4. Production, только чтение

Свежая копия через backup API; SHA-256 production до и после совпал (`aef7383e…`), миграция проверялась
только на копии.

| Показатель | Значение |
|---|---|
| заявок | 25: RECEIVED 8, REJECTED 9, SUBMITTED 8 |
| событий | 54: CREATED 25, STATUS_CHANGED 23, OUTBOUND_MESSAGE 6 |
| неотправленных (`DISCOVERED … APPROVED`, то есть `IN_PREPARATION`) | **0** |
| дублей `(source, source_reference)` / `job_id` / `vacancy_fingerprint` / компания+роль | **0 / 0 / 0 / 0** |
| заявок без строки в `jobs` | 8 (в `jobs` нет вакансии, сверять нечего) |
| заявок со ссылкой на `job_id` | 17; у всех `jobs.status = NEW`, но live-оценка даёт `ALREADY_APPLIED` → NOT_SUITABLE, gate закрыт у **17 из 17** (включая вторые строки: 2354 для заявки 3) |
| вторые строки `jobs` с общим source reference | 5 пар; все закрыты gate (`NOT_SUITABLE` или `NEEDS_OPERATOR`) |
| черновиков писем / CV | 2 / 2 (job 212 и 376) |
| подготовок Telegram | 1, `PREPARED` |
| автоциклов | 0 |

Потенциальных дубликатов заявок: **0**. Таблица действий не требуется. Вторые строки `jobs` для одной
вакансии — это дубли **вакансий**, а не заявок; они уже обрабатываются Stage 22 (source reference).

Gate по всем 3 234 вакансиям копии: NOT_SUITABLE 2 141, NEEDS_OPERATOR 947, STRETCH 137,
WORK_TIME_NOT_CONFIRMED 7, HYBRID_ADMIN_ROLE 1, **открыт 1 (id 2005)** — как до правки; правка Stage 55 на
текущих данных ничего не меняет, потому что неотправленных заявок в production нет.

## 5. Гонки, crash, retry, restart

| Сценарий | Результат |
|---|---|
| A. первая подготовка | CV 1, письмо 1, заявок 0 |
| B. повторный запуск | ничего нового (`bewerbung_created == 0`) |
| C. два воркера одновременно (барьер) | CV всегда 1; письмо 1 или 2; заявок 0 |
| C′. оба воркера увидели «письма нет» (детерминированно) | **2 письма** (воспроизведено), заявок 0; следующий запуск ничего не добавляет |
| D. сбой после CV, до письма | следующий запуск переиспользует CV (S8C-AUDIT-001, существующие тесты) |
| E. restart с заявкой в работе | вакансия удержана (`APPLICATION_IN_PROGRESS`) |
| F. retry после ошибки | идемпотентно (см. B) |
| G. два collector, одна вакансия | общий source reference → удержана; fingerprint/URL → тот же результат |
| H. SUBMITTED / RECEIVED / REJECTED | подготовка не выполняется |
| H′. DRAFT-уровень (DISCOVERED … APPROVED) | подготовка не выполняется (после правки) |
| I. другая вакансия того же работодателя | не удерживается |
| параллельное создание одной заявки | ровно 1 строка |

**Принятый остаточный риск (C′).** `bewerbung_drafts` не имеет уникальной идентичности намеренно: каждая
регенерация письма вставляет новую строку. Поэтому два воркера, одновременно прочитавшие «письма нет»,
оставляют два одинаковых по содержимому локальных черновика. Это не заявка, не отправка и не сообщение
наружу. Реалистичный второй воркер — ручной API-вызов во время автоцикла (сами автоциклы защищены lease).
Закрытие потребовало бы миграции и изменило бы смысл регенерации, поэтому не делалось. Условие, при котором
риск нужно пересмотреть: автоматическое создание заявок из черновиков.

## 6. Обход Stage 54 (FULL_TIME) и других gate

Автоматический путь один (`automation_shortlist`), gate вызывается вживую и fail-closed три раза. Остальные
пути ручные и требуют нажатия человека: Telegram-подготовка и API-вызовы подготовки **не** используют
`automatic_block_reason` (решает оператор), но проходят `ensure_not_already_applied` / проверку
`JOB_NOT_ELIGIBLE`. Старые shortlist-записи и сохранённые score не обходят gate: он считает оценку заново
(`assess_job` в отдельной сессии, без кэша `job_fit_assessments`). Подготовка без флага
`AUTOMATION_AUTO_PREPARE_ENABLED` не запускается.

## 7. Результаты тестов

Раздельно; локальный Windows, SQLite, ветка `fix/application-idempotency`:

| Набор | Результат |
|---|---|
| новые `tests/test_application_idempotency.py` | 23 passed (подготовка, повтор, 2 воркера, детерминированная гонка, SUBMITTED/RECEIVED/REJECTED, IN_PREPARATION, вторая строка `jobs`, ручной CV, Telegram, регистр источника, обязательная ссылка, параллельное создание заявки) |
| gate, work-time, shortlist, nationwide, очередь | 515 passed |
| отправка, follow-up, Telegram, lease, lifecycle, review package | 1 090 passed, 1 skipped |
| затронутые изменениями идентичности (23 файла) | 801 passed, 8 skipped |
| расширенный (26 файлов, до раунда 2) | 966 passed, 1 skipped |
| Ruff `check` и `format --check` | чисто |
| PostgreSQL-модули локально | пропущены без `TEST_POSTGRES_URL`; выполняются в CI (`scheduler-postgres`) |
| Alembic | миграция не добавлена; одна голова `a7c3e5f1b9d2`; `upgrade head` на свежей копии production прошёл |
| Linux CI, полный набор | см. PR #55 |

## 8. Codex

| Раунд | Вердикт | Содержание |
|---|---|---|
| 1 | REJECT | 3×P2, 1×P3 (§2a); P0/P1 нет; ни один путь не создаёт автоматическую заявку или отправку |
| 2 | REJECT | 4×P2, 1×P3: в основном следствия моего fallback `(source, job_id)`; fallback удалён, остальное исправлено |
| 3 | **ACCEPT** | замечаний нет; «No reachable second ACTIVE application», «Automation remains draft-only» |

Codex в read-only песочнице не может запускать pytest; его выводы о тестах — по чтению кода и проверкам в памяти.
Формулировки запросов не подгонялись под вердикт: раунды 2 и 3 просили искать регрессии и обходы.

## 9. Оставшиеся риски

1. До 2 локальных черновиков письма при гонке двух воркеров (§5, принято).
2. Индекс уникальности — по «сырому» `source`; защита от регистра работает на уровне сервиса, псевдонимы (`ba`/`bundesagentur`) не нормализуются (§2a). Одновременный запуск двух CLI с разными псевдонимами теоретически возможен.
3. Ручные пути (Telegram, API) не применяют work-time gate — решение остаётся за оператором.
4. Production `.env` не читался; булевы проверки — на стороне оператора перед внедрением.

## 10. Условия GO / NO-GO

GO для внедрения цепочки возможен, когда: PR #55 и вся цепочка #47–#55 проходят CI; оператор принял
остаточные риски выше; сделан бэкап и остановлены процессы; миграция и обновление fit-кэша выполняются
явными командами. Нарушение любого условия, а также любое P0/P1 от Codex — NO-GO.
