# Финальный аудит, Stage 54: цепочка PR #47 → #53 и gate автоматической подготовки

Дата: 2026-11-09. Аудит read-only; все замеры на копиях БД (SQLite backup API, `mode=ro`).
Production (`runtime/job_triage_runtime.db`) не менялась: SHA-256 `aef7383e…0a02fe` до и после, mtime
2026-11-06 09:14, 3 234 вакансии, 25 Bewerbungen, 54 события, alembic `d6e9a2b4c8f1`.

## 1. Executive summary

**Вердикт: CONDITIONAL GO — не NO-GO, но и не «готово».** Рекомендация — вливать цепочку только вместе
с PR #54 (он закрывает единственную найденную реальную проблему безопасности) и только после
выполнения условий из §9.

* Найден один реальный дефект: **неопределённое рабочее время (UNKNOWN) открывало gate автоматической
  подготовки**. Это сознательная политика старых версий («отсутствие часов не причина удерживать»),
  но она противоречит критерию «неопределённость не должна обходить gate», а regex-классификатор
  не отличает «часы не указаны» от «часы указаны словами, которых он не знает» («Halbtagsstelle»,
  «50 %-Stelle», «Minijob», «20 Stunden pro Woche» → UNKNOWN и в PR #51, и в #53). На копии
  production gate был открыт у 8 вакансий, **у 7 из них — при UNKNOWN**.
* Исправление (PR #54, поверх #53): автоматическая подготовка требует **подтверждённого**
  FULL_TIME (текст вакансии или структурный флаг источника). Без изменения модели данных и без
  изменения fit tier / triage / очереди. Gate открыт: 8 → 1 вакансия (id 2005, у неё
  `NEEDS_ENRICHMENT`, то есть до подготовки не дойдёт).
* Расхождение 8 и 14 полностью объяснено (§4): это **разные базы** (3 234 против 3 368 строк) и
  **наличие структурного BA-флага** (0 против 399 строк), а не ошибка расчёта.
* BFI №3294 найдена (§5): существует только в тестовых копиях nationwide-прогона, в production никогда не была.
* Автоматической **отправки** Bewerbung в коде нет вообще; отправка писем-ответов и follow-up — только
  через два HTTP-эндпоинта с approval, закреплённым получателем и kill switch (§6).
* Формальный Codex ACCEPT: **для PR #53 — нет (REJECT, два P2-замечания остаются в #53)**; для PR #54 —
  **ACCEPT (код)** после 4 раундов, с одним P3.

## 2. Codex findings

| Объект | Вердикт | Открытые замечания |
|---|---|---|
| PR #52 (очередь) | REJECT (раунд 1) → раскрытие компромиссов принято, кода-дефектов нет; формального ACCEPT нет | P1/P2 из ревью очереди оформлены как «Known limits» в `docs/MANUAL_REVIEW_QUEUE.md`: холодный кэш неполон; O(N) на запрос; нет единого снимка; повтор удваивает живую работу |
| PR #53 (work-time), независимое ревью на актуальном HEAD `98ad8a8` | **REJECT** | P2 регрессия: «Die Stelle ist im Teilzeitmodell zu besetzen» → UNKNOWN; P2: «Part-time employment available immediately» и «Keine Vollzeit möglich. Teilzeit ist möglich.» → UNKNOWN; P2: «Our company offers full- and part-time work. This position is part-time.» → FULL_TIME; P2 (pre-existing): UNKNOWN допускает подготовку. Последнее исправление (`möglich` против `möglichst`/«nach Möglichkeit») **подтверждено**: Codex воспроизвёл закрытие. Все более ранние P1/P2 (Teilzeitmöglichkeiten, part-time options, Teilzeitstelle/-position, часы, staff-фразы, отрицания, BA-флаг, квадратичные пути) подтверждены закрытыми; CONFLICT корректно обрабатывается всеми потребителями |
| PR #54 (поверх #53), 4 раунда | **ACCEPT (код)**; полный pytest Codex запустить не может (read-only песочница) | P3: «Teilzeit möglich, 20 Stunden pro Woche.» → UNKNOWN; при BA-флаге `true` итог FULL_TIME (продвижение через структурный флаг). Остаточный риск признан допустимым при обязательном ручном approval перед отправкой |

Что закрывает PR #54 из замечаний к #53: gate больше не допускает UNKNOWN (закрывает все три
«ложный UNKNOWN»-регрессии как gate-проблему); общее утверждение «full- and part-time» не перекрывает
собственное «Teilzeit» вакансии (в т.ч. «Vollzeit oder Teilzeit. Diese Stelle ist Teilzeit.» и «Our company
offers full-time work. This position is part-time.»); повторные «Vollzeit oder Teilzeit» не считаются
override; часы «Vollzeit (40 h),» не приписываются соседнему «Teilzeit auf Anfrage möglich».

Что **остаётся в #53 без исправления**: сами ложные UNKNOWN-классификации («Teilzeitmodell zu
besetzen» и т.п.) — они больше не открывают gate, но остаются неточностью текста; а если у такой вакансии
BA-флаг `true`, итог всё равно FULL_TIME (P3 выше).

## 3. Work-time classification

Корректные решения (из набора тестов, проверено вручную на финальном коде):

| Текст | Результат |
|---|---|
| «Vollzeit», «Full-time position», «Vollzeit/Teilzeit», «Vollzeit oder Teilzeit» | FULL_TIME |
| «Nur Teilzeit», «Ausschließlich Teilzeit», «Part-time only», «Arbeitszeit: Teilzeit» | PART_TIME |
| «Teilzeitmöglichkeiten», «part-time options available», «Teilzeit nach Absprache möglich» | UNKNOWN (опция, не часы вакансии) |
| «Teilzeit nicht möglich», «Keine Vollzeit möglich» | UNKNOWN |
| «Vollzeit (40 h), Teilzeit auf Anfrage möglich.» | FULL_TIME |
| «Our company offers full-time work. This position is part-time.» | PART_TIME |
| «Sie arbeiten in Teilzeit möglichst vormittags.» | PART_TIME (не опция) |

Известные ошибочные/неполные решения (без regex-«заплаток», см. ограничение в задаче):

| Текст | Результат | Класс риска |
|---|---|---|
| «Halbtagsstelle, 20 Stunden pro Woche», «50 %-Stelle», «Minijob», «Werkstudent, 15 h/Woche», «25 Stunden pro Woche» | UNKNOWN (pre-existing, одинаково в PR #51) | закрыто gate: без подтверждённого FULL_TIME подготовки нет |
| «Die Stelle ist im Teilzeitmodell zu besetzen» | UNKNOWN | закрыто gate; при флаге `true` — FULL_TIME (P3) |
| «Teilzeit möglich, 20 Stunden pro Woche.» | UNKNOWN | то же |

Вывод по архитектуре: regex-классификатор **надёжен для запрета** (его ошибки идут в сторону PART_TIME/UNKNOWN),
но **недостаточен для разрешения**, пока UNKNOWN допустим. Поэтому разрешение теперь требует
подтверждения; полноценное разделение `CONFIRMED_FULL_TIME / CONFIRMED_PART_TIME_ONLY / UNKNOWN / CONFLICT`
в модели данных **не требуется**: существующие `FULL_TIME` (подтверждён текстом или флагом) /
`PART_TIME` / `UNKNOWN` / `CONFLICT` уже дают это разделение, нужно было лишь изменить gate.

## 4. Remote + Vollzeit: почему 8 и 14

Воспроизводимый отчёт: `scripts/audit_remote_fulltime_reconciliation.py` (read-only, SQLite `mode=ro`,
отказывается писать поверх базы). Запуск и результат ниже — на **финальном** коде.

| Определение | prod_copy (3 234 строки, флагов нет) | scratch_3368 (3 368 строк, флагов 399) |
|---|---:|---:|
| A. `work_format = FULL_REMOTE` | 68 | 73 |
| B. A и `work_time = FULL_TIME` (определение воронки nationwide: «14») | **8** | **15** (14 до исправления PR #54) |
| C. remote-критерий `COMPATIBLE` | 68 | 73 |
| D. C и FULL_TIME | 8 | 15 |
| E. A и FULL_TIME **по тексту** | 8 | 9 |
| F. A и FULL_TIME **только через BA-флаг** | 0 | 6 |
| G. A и work_time UNKNOWN | 33 | 30 |
| H. A и PART_TIME/CONFLICT | 27 | 28 |

Причина расхождения: «8» — это production-копия 3 234 вакансий, где колонка `full_time_offered` пуста
(миграция `a7c3e5f1b9d2` на production ещё не применялась, сбор BA-флага не запускался), поэтому
Vollzeit берётся только из текста. «14» — scratch-копия nationwide-прогона 2026-11-08 (3 368 строк = 3 234 +
134 новые), в которой флаг заполнен у 399 строк: 8 вакансий подтверждены текстом, ещё 2 **существующие**
вакансии (1375, 1397) получили Vollzeit только из флага, и 4 вакансии — новые строки (3271, 3281, 3294 —
через флаг; 3335 — по тексту). Оба числа верны для своих множеств; ошибка была в интерпретации:
отчёт назвал «подтверждёнными» и вакансии, у которых Vollzeit даёт только структурный флаг, и вакансии,
которых в production нет. Сходная история с «673» очереди (scratch) против «682» (production-копия):
28 вакансий очереди production-копии отсутствуют в scratch (в scratch у части вакансий заполнены координаты места работы: у #1337 в production-копии их нет,
в scratch есть, поэтому там #1337 получает `ESTIMATE_BEYOND`), 19 появляются только в scratch.

На финальном коде значение scratch поднялось с 14 до 15: вакансия 1728 (zollsoft, NOT_SUITABLE) раньше была
ложным PART_TIME ( «mindestens 32 Stunden … einer flexiblen Teilzeitanstellung steht grundsätzlich nichts
im Wege» ), теперь текст UNKNOWN, и флаг `true` даёт FULL_TIME — это верное прочтение.

Вакансии, которые участвуют в сверке (FULL_REMOTE + подтверждённый или спорный work-time), по обоим
снимкам; `ref` — BA-референс или XING-ссылка, id различаются по снимкам:

| ref | title | employer | production copy: id, work_time (text/flag), tier | scratch 3 368: id, work_time (text/flag), tier | verdict |
|---|---|---|---|---|---|
| xing/m/Luhfl9SAYNCR6XwbomPJgd | (Junior) SAP ABAP Entwickler (m/w/d) / H | ADVERGY GmbH | 267, FULL_TIME (FULL_TIME/None), NOT_SUITABLE | 267, FULL_TIME (FULL_TIME/None), NOT_SUITABLE | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 10001-1000231286-S | Javascript Entwickler 100% remote (m/w/d | Anke Holst - ALKA - So | 1375, UNKNOWN (UNKNOWN/None), STRETCH | 1375, FULL_TIME (UNKNOWN/True), STRETCH | FULL_REMOTE, work time FULL_TIME/UNKNOWN |
| 15794-470192_49894-1-S | Senior Conversational AI & ML Solution E | Deloitte GmbH | 1390, PART_TIME (PART_TIME/None), NOT_SUITABLE | 1390, CONFLICT (PART_TIME/True), NOT_SUITABLE | FULL_REMOTE, work time CONFLICT/PART_TIME |
| 10001-1003753844-S | GenAI Developer [m/w/d] - 100% Remote | Die Tech Recruiter Gmb | 1397, UNKNOWN (UNKNOWN/None), NOT_SUITABLE | 1397, FULL_TIME (UNKNOWN/True), NOT_SUITABLE | FULL_REMOTE, work time FULL_TIME/UNKNOWN |
| 10001-1003697556-S | Lead Spatial Data & Geospatial Engine Ar | RE-Managers GmbH | 1464, FULL_TIME (FULL_TIME/None), NOT_SUITABLE | 1464, FULL_TIME (FULL_TIME/None), NOT_SUITABLE | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 10001-1003818217-S | KI-Entwickler Datenmigration (w/m/d) | SIV.AG - Service für I | 1474, FULL_TIME (FULL_TIME/None), STRETCH | 1474, FULL_TIME (FULL_TIME/True), STRETCH | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 15794-471556_50427-1-S | Consultant AI Engineering (m/w/d) | Deloitte GmbH | 1533, PART_TIME (PART_TIME/None), NOT_SUITABLE | 1533, CONFLICT (PART_TIME/True), NOT_SUITABLE | FULL_REMOTE, work time CONFLICT/PART_TIME |
| 10001-1003664872-S | Senior Platform / Backend Engineer (m/w/ | zollsoft GmbH | 1728, UNKNOWN (UNKNOWN/None), NOT_SUITABLE | 1728, FULL_TIME (UNKNOWN/True), NOT_SUITABLE | FULL_REMOTE, work time FULL_TIME/UNKNOWN |
| 21223-16793239-S | Business Intelligence Manager (m/f/d) /  | K.D. Pharma Bexbach Gm | 2828, FULL_TIME (FULL_TIME/None), NEEDS_OPERATOR | 2828, FULL_TIME (FULL_TIME/None), NEEDS_OPERATOR | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 10001-1003853660-S | IT-Systemadministrator / Softwareentwick | ICONSA GmbH & Co. KG | 2971, FULL_TIME (FULL_TIME/None), STRETCH | 2971, FULL_TIME (FULL_TIME/None), STRETCH | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 10001-1003940605-S | DevOps / DevSecOps Engineer (m/w/d) | Jochen Blöcher GmbH | 3046, FULL_TIME (FULL_TIME/None), STRETCH | 3046, FULL_TIME (FULL_TIME/None), STRETCH | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 12928-975IE29WBAZ7QURN-S | Site Reliability Engineer (gn) – fully r | Experis GmbH | 3093, FULL_TIME (FULL_TIME/None), STRETCH | 3093, FULL_TIME (FULL_TIME/None), STRETCH | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 12928-HAYNYR1X4FGFDY83-S | Senior SAP Developer (gn) - fully remote | Experis GmbH | 3094, FULL_TIME (FULL_TIME/None), NOT_SUITABLE | 3094, FULL_TIME (FULL_TIME/None), NOT_SUITABLE | CONFIRMED FULL_REMOTE + FULL_TIME (text) |
| 14774-BB-562880-12683-682-S | Spring Developer (m/w/d) | Deichmann SE | absent | 3271, FULL_TIME (UNKNOWN/True), NEEDS_OPERATOR | CONFIRMED FULL_REMOTE + FULL_TIME (BA flag only); absent in prod_copy |
| 10001-1003747124-S | Senior SAP Commerce Cloud/Hybris Develop | Maify GmbH | absent | 3281, FULL_TIME (UNKNOWN/True), NOT_SUITABLE | CONFIRMED FULL_REMOTE + FULL_TIME (BA flag only); absent in prod_copy |
| 15794-471822_50549-1-S | Senior Consultant AI Engineer (m/w/d) | Deloitte GmbH | absent | 3289, CONFLICT (PART_TIME/True), NOT_SUITABLE | FULL_REMOTE, work time CONFLICT; absent in prod_copy |
| 15297-0gddsy3w-S | Junior Engineer (m/w/d) Schwerpunkt Pyth | BFI Informationssystem | absent | 3294, FULL_TIME (UNKNOWN/True), STRETCH | CONFIRMED FULL_REMOTE + FULL_TIME (BA flag only); absent in prod_copy |
| 19886-USQ4F38W70GZRDWT-S | Python Developer mit fachlicher Koordina | digatus personal GmbH | absent | 3335, FULL_TIME (FULL_TIME/True), NEEDS_OPERATOR | CONFIRMED FULL_REMOTE + FULL_TIME (text); absent in prod_copy |


Итоги сверки на финальном коде: **9** вакансий FULL_REMOTE + FULL_TIME подтверждены текстом (8 в обоих
снимках + 1 новая, 3335), **6** — только структурным флагом BA (1375, 1397, 1728, 3271, 3281, 3294),
**ни одна** из 15 не проходит gate автоматической подготовки сейчас (все либо NOT_SUITABLE/STRETCH/
NEEDS_OPERATOR, либо отсутствуют в production). Ни одна вакансия не подгонялась под число.

## 5. BFI №3294

* **Вакансия:** «Junior Engineer (m/w/d) Schwerpunkt Python», BFI Informationssysteme GmbH, Ilmenau
  (Thüringen). Источник: Bundesagentur, референс **`15297-0gddsy3w-S`**, ссылка
  `https://www.arbeitsagentur.de/jobsuche/jobdetail/15297-0gddsy3w-S`, `first_seen_at` 2026-11-08 10:05:58.
* **Где:** только в тестовых копиях из `E:\Projects\jt-match-work` (`junior*.db`, `nat3.db`: id **3294**;
  в `nat.db` — id 3242, в `nat2.db` — 3237: id автоинкрементный и **не стабилен между копиями**, ссылаться
  нужно на референс). В этих копиях 3 368 строк = 3 234 production + 134 строки, добавленные dry-run'ом
  nationwide-сбора 2026-11-08 10:02–10:09.
* **В production никогда не было:** по `source_reference`, `url`, `fingerprint` и по городу совпадений нет;
  последняя новая строка production — 2026-11-01 03:12 (id 3234); 32 бэкапа в `C:\Users\V\JobTriage-Backups`
  и `runtime\*.db` не содержат id > 3234 с этой вакансией. Поэтому в копии из 3 234 строк её нет — это не ошибка.
* **Исходное описание сохранено** (3 568 символов, BA-флаг `arbeitszeitVollzeit = true`,
  `posting_type = ARBEIT`), решение воспроизведено на нём, не на фикстуре: stored score **81 / MAYBE**
  (после исправления scoring; до него SKIP), fit tier **STRETCH** (`skills WEAK 3/100`, `language GAP`:
  немецкий B2–C1 при подтверждённом B1), triage **MANUAL_REVIEW / THIN_SKILL_EVIDENCE**, work_time **FULL_TIME
  только по BA-флагу** (текст молчит), remote **FULL_REMOTE**, `jobs.full_time_offered = 1`. Автоматическая
  подготовка заблокирована: **`FIT_TIER_STRETCH`**. В очереди `/ui/review` (scratch, тёплый кэш) вакансия
  присутствует и стоит в Junior-группе.
* Автоматически переносить её в production не пытался и не нужно: при обычном сборе BA она появится сама.

## 6. Automation safety: реальные gate'ы

Цепочка и условия переходов проверены по коду, не по названиям:

| Этап | Что решает | Блокирует | Повторы |
|---|---|---|---|
| collect | коллекторы BA/XING (`collector_runner`); XING-ссылки из писем не открываются | `NATIONWIDE_REMOTE_DISCOVERY_ENABLED=false` по умолчанию; `LOCAL_DISCOVERY_ENABLED` | идентичность по `source_reference`/fingerprint (DB-01) |
| normalize/score | `jobs.score`, `recommendation` | — | — |
| assess | `assess_job` (живой) → fit tier | NOT_SUITABLE (барьеры), NEEDS_OPERATOR, STRETCH | `duplicate`: ALREADY_APPLIED → NOT_SUITABLE; POSSIBLE_DUPLICATE → NEEDS_OPERATOR |
| triage | категория для оператора и очереди | не открывает ничего сама | — |
| **shortlist** | `automation_shortlist.prepare_shortlist_drafts` | **только** задетые этим запуском вакансии; `status ∈ {NEW, SAVED}`; `recommendation ∈ {APPLY, MAYBE}`; `match ≥ 80`; затем gate; лимит `shortlist_max_per_run` | — |
| **prepare (черновик)** | CV и Bewerbung-черновик | `automatic_preparation_block` вызывается **три раза** (при отборе, перед CV, перед Bewerbung) живой оценкой в отдельной сессии, fail-closed; кэш не используется; флаг `AUTOMATION_AUTO_PREPARE_ENABLED=false` по умолчанию | повторное использование черновика (`bewerbung_draft_is_current`) |
| approve | `/review-packages/.../approve`, Telegram APPROVE: решение оператора | — | — |
| **send** | **автоматической отправки Bewerbung нет.** Есть два HTTP-эндпоинта (`/response-drafts/{id}/send`, follow-ups) — ответы по уже существующей переписке | approval + закреплённый получатель + `OUTBOUND_SENDING_ENABLED=false` (503) + защита от повторной отправки (CAS) | approval расходуется один раз |

Автоматический shortlist, автоматическая подготовка черновика и автоматическая отправка — три разных
действия: shortlist/подготовка существуют (и выключены по умолчанию), отправки Bewerbung автоматикой нет.
Модули `automation.py` и `automation_shortlist.py` не импортируют SMTP/outbound-провайдеры (проверено grep;
в `automation_gmail.py` провайдер упомянут только в docstring).

Эмпирическая матрица gate: чистая вакансия (GOOD_MATCH) + одно условие за раз, результат реального
`automatic_block_reason`:

| Условие | Gate (PR #53) | Gate (PR #54) |
|---|---|---|
| чистая, work_time FULL_TIME | открыт | открыт |
| `work_time = UNKNOWN` | **открыт** | **закрыт** `WORK_TIME_NOT_CONFIRMED` |
| `work_time = PART_TIME` | закрыт `FIT_TIER_STRETCH` | то же |
| `work_time = CONFLICT` | закрыт `FIT_TIER_NEEDS_OPERATOR` | то же |
| `skills = WEAK` (THIN_SKILL_EVIDENCE) | закрыт STRETCH | то же |
| `skills = NOT_EVALUATED` | закрыт NEEDS_OPERATOR | то же |
| `language = GAP / GAP_TO_VERIFY / MAJOR_GAP` | закрыт STRETCH | то же |
| `language = MINOR_GAP`, `skills = PARTIAL` | открыт (PARTIAL_MATCH) | открыт |
| `remote = UNVERIFIED_ELIGIBILITY` (REMOTE_UNVERIFIED) | закрыт NEEDS_OPERATOR | то же |
| `location = DISTANCE_UNVERIFIED / UNKNOWN` | закрыт NEEDS_OPERATOR | то же |
| `duplicate = ALREADY_APPLIED` / `POSSIBLE_DUPLICATE` | закрыт | то же |
| `duplicate = IN_PREPARATION` | **открыт** | открыт — остаётся (см. ниже) |
| гибрид Admin/Developer в названии | закрыт `HYBRID_ADMIN_ROLE` | то же |
| `MAYBE` / `MANUAL_REVIEW` как таковые | не входят в gate: решает fit tier; `MAYBE` — лишь предусловие отбора | то же |

Замечания: (1) `duplicate = IN_PREPARATION` не закрывает gate — защита от второй Bewerbung на ту же вакансию
обеспечивается переиспользованием черновика и проверками approval/application guard, но gate её не
выделяет; это не новая проблема и не изменялось; (2) в матрице критерии меняются по одному, поэтому
несогласованные комбинации (`remote=ONSITE` при `location=REMOTE_OK`) в реальном потоке не возникают.
Реальный обход gate неподтверждённым **FULL_TIME** остаётся единственным путём: ошибочный текст или
флаг BA; он ограничивается ручным approval (§9).

## 7. Data integrity

| Проверка | Результат |
|---|---|
| Production vs исходный снимок | SHA-256 `aef7383e…0a02fe` идентичен; mtime 2026-11-06 09:14 (новых законных записей с 06.11 нет) |
| `PRAGMA integrity_check` / `foreign_key_check` (свежий snapshot) | ok / пусто |
| Счётчики | 3 234 jobs · 25 applications · 54 events · 3 234 строк кэша · alembic `d6e9a2b4c8f1` |
| Статусы Bewerbungen | RECEIVED 8, REJECTED 9, SUBMITTED 8 |
| Alembic heads | одна голова в каждом worktree цепочки: `d6e9a2b4c8f1` (#47, #48) → `a7c3e5f1b9d2` (#49–#53 и PR #54) |
| Кэш на snapshot | все 3 234 строки STALE (правила изменились); `stage21_assess_vacancies.py --apply` на копии: 3 234 строк, повтор — 0 |
| Чтение `/ui/review` не пишет | счётчики applications/events/jobs до и после чтения совпали (25/54/3 234 и 25/54/3 368) |

## 8. Manual Review Queue и тесты

Очередь на финальном коде (тёплый кэш, копии): production-копия — **682** записи, 28 страниц, 22 с,
1 036 SQL, 0 живых оценок, первая страница 1,1 с, глобальная сортировка и отсутствие дублей проверены
(`globally_sorted=True`, 682 уникальных); scratch — **673** записи, 27 страниц; холодный кэш — 3,4 с,
16 записей, **3 174 не пересчитано** (страница сообщает о неполноте). Junior Python/Backend: production-копия —
705, 752, 347, 328, 324, 310, 292, 1337; scratch — 705, 752, 347, 328, 324, 310, 292, **3294**. Отличие 1337/3294
объяснено данными (координаты места работы в scratch → #1337 `ESTIMATE_BEYOND`/NOT_SUITABLE). #705/#752/#1337:
work_time UNKNOWN (production-копия) или FULL_TIME по BA-флагу (scratch для 705/752). Страница read-only.

Тесты (точные числа):

| Набор | Результат |
|---|---|
| Локальные targeted (Windows), PR #53 HEAD `98ad8a8`: work-time, remote, junior, triage, shortlist, automation, approval, dedupe, review, cache, nationwide, migrations, stage12/21/22, send-readiness | **2 519 passed, 5 skipped, 0 failed** |
| Расширенный набор (+ collectors, score, stage16, send) на ветке PR #54, финальный код | **3 011 passed, 5 skipped, 0 failed**; отдельно meta/contract-тесты: 195 passed |
| До обновления фикстур | 37 failed — тесты, закреплявшие старую политику «UNKNOWN допустим» (фикстуры без часов); обновлены без ослабления утверждений |
| Skipped (5) | все — Windows: нет права создавать symlink (`test_stage12_human_review_gate` ×4, `test_stage21_production_identity` ×1); PostgreSQL-модули локально пропускаются без `TEST_POSTGRES_URL` |
| Полный локальный pytest | **не запускался** (слишком долго); `test_imap_deadline.py` исключён из-за известной WinError 4551 — защита Windows не отключалась |
| Linux CI, PR #53 HEAD `98ad8a8` | 8 622 passed, 169 skipped, 0 failed; PostgreSQL (`scheduler-postgres`): 36 passed, 125 deselected, ноль пропусков; docker: pass |
| Linux CI, PR #54, первый прогон (`fbaeb04`) | 8 646 passed, **1 failed**, 169 skipped: `tests/test_cli_dispatcher.py::test_every_operator_script_has_exactly_one_stable_command` — новый скрипт аудита не был зарегистрирован в `app/cli.py`; мой локальный выбор тестов этот файл не включал. Исправлено коммитом `c563f54`; результат повторного прогона — в PR #54 |
| Ruff | `ruff check .` и `ruff format --check .` чистые на ветке PR #54 |
| Alembic | одна голова во всех worktree; `upgrade head` (d6e9a2b4c8f1 → a7c3e5f1b9d2) на свежем snapshot production; `downgrade -1` + `upgrade head` проверены на копии (Stage 53); `PRAGMA integrity_check = ok` |
| Codex | PR #53: REJECT (§2); PR #54: ACCEPT (код), 4 раунда; pytest Codex в своей read-only песочнице запустить не может |

## 9. Release blockers

Блокирующее для GO — только то, что не выполнено; блокеров «обхода без approval» не найдено.

1. **PR #54 не влит.** Без него gate допускает UNKNOWN (§1, §6). Вливать #47→#53 без него не рекомендуется.
2. **Формальный Codex ACCEPT для #52 и #53 отсутствует.** Остаются: компромиссы очереди (холодный кэш неполон,
   O(N) на запрос, нет единого снимка), P2-замечания по ложным UNKNOWN в #53.
3. **Проверка production `.env`** не выполнялась (файл с секретами не читался): нужно булево подтверждение
   отсутствия шаблонных `API_KEY`/`UI_SESSION_SECRET`/`POSTGRES_PASSWORD` и значений `false` для
   `AUTOMATION_SCHEDULER_ENABLED`, `AUTOMATION_AUTO_PREPARE_ENABLED`, `OUTBOUND_SENDING_ENABLED`,
   `TELEGRAM_GMAIL_DRAFT_ENABLED`, `NATIONWIDE_REMOTE_DISCOVERY_ENABLED`.
4. Резервная копия и остановка процессов перед миграцией и перезаполнением кэша (см. `docs/FINAL_RELEASE_READINESS.md`).

Не блокирует, но известно: первая страница `/ui/review` не быстрее старой, параллельные запросы
сериализуются (GIL); regex-классификатор — эвристика.

## 10. Рекомендуемые следующие действия

1. Просмотреть и принять/отклонить PR #54 (политика «автоматическая подготовка только при подтверждённом
   FULL_TIME» — решение оператора; откат — revert одного коммита).
2. После approve: слить цепочку по порядку #47 → #48 → #49 → #50 → #51 → #52 → #53 → PR #54 (перенацеливая базы).
3. Проверить `.env` (п.9.3), сделать бэкап, остановить приложение, `alembic upgrade head`, затем явный
   `stage21_assess_vacancies.py --apply --snapshot …`, запустить приложение, проверить `/ui/review`
   (нет предупреждения о неполноте, 25 Bewerbungen, 54 события).
4. Планировщик и автоподготовку не включать без отдельного решения; после включения наблюдать `blocked_by_fit`.
5. Документацию аудита стоит держать в PR #54 (код изменён), а не в отдельном PR.
