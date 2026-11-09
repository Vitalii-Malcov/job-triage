# Отчёт: классификация Vollzeit/Teilzeit (Stage 53)

Все измерения сделаны на **копиях** рабочей SQLite (SQLite backup API из `mode=ro`, миграция до
головы `a7c3e5f1b9d2` только на копии). Рабочая БД не менялась: SHA-256 `aef7383e…` до и после,
3 234 вакансии, 25 Bewerbungen, 54 события, alembic `d6e9a2b4c8f1`.

## 1. Причина дефекта

`classify_work_time` (`app/agents/job_fit_signals.py`) возвращал `PART_TIME` при **любом**
неотрицаемом слове `teilzeit…` / `part-time`. Слово из списка преимуществ работодателя
(«Teilzeitmöglichkeiten», «part-time options») считалось «только Teilzeит». Результат —
`work_time = PART_TIME` → в `_STRETCH` (`job_fit_assessment.py`) → тир `STRETCH`/ручная проверка и
вводящая в заблуждение подпись в `/ui/review`.

Путь на копии БД до исправления (вакансии 705, 752, 1337):

| id | Текст (реальный) | `jobs.full_time_offered` | `work_time` | fit tier | triage |
|---|---|---|---|---|---|
| 705 | «…Gleitzeitregelungen, Teilzeitmöglichkeiten **Dein Profil**…» | NULL | PART_TIME | NEEDS_OPERATOR | MANUAL_REVIEW / ROUTE_REVIEW_REQUIRED |
| 752 | то же | NULL | PART_TIME | NEEDS_OPERATOR | MANUAL_REVIEW / ROUTE_REVIEW_REQUIRED |
| 1337 | «…flextime, mobile working, part-time options, job sharing…» | NULL | PART_TIME | NEEDS_OPERATOR | MANUAL_REVIEW / ROUTE_REVIEW_REQUIRED |

Структурное поле `full_time_offered` пусто **у всех 3 234 вакансий** (миграция добавила колонку, но
сбор BA-флага на рабочей БД ещё не запускался), поэтому флаг в этом дефекте не участвовал: источником
ошибки был только текст.

## 2. Исправление (минимальное, в существующем классификаторе)

* `PART_TIME` — только если часы вакансии названы частичными: «nur / ausschließlich Teilzeit»,
  «part-time only», размеченное поле («Arbeitszeit: Teilzeit», «Umfang Teilzeit 50 %»,
  «Vertragsart: Teilzeit»), либо слово без признаков «опции».
* Не доказывают `PART_TIME`: `Teilzeitmöglichkeit(en)`, `-modell`, `-option`, `teilzeitgeeignet`,
  «Teilzeit nach Absprache / auf Wunsch / bei Bedarf möglich», «Möglichkeit zur Teilzeit…»,
  «part-time options / possible / available», «auch in Teilzeit», «teilzeit eligible». Слова-льготы рядом (Gleitzeit, Sabbatical…) сами по себе опцией **не** считаются (см. ревью Codex).
* `Voll- oder Teilzeit`, `Voll-/Teilzeit`, `full- or part-time` — теперь `FULL_TIME` (как
  «Vollzeit oder Teilzeit»: полная занятость предлагается).
* Правило приоритета: явный текст → структурный флаг BA → `UNKNOWN`. Нет подтверждения Vollzeit —
  это `UNKNOWN`, не `PART_TIME` и не `FULL_TIME`.
* **Конфликт** текста и флага (`Vollzeit` при `arbeitszeitVollzeit=false`; «nur Teilzeit» при
  `true`) → новый статус `work_time = CONFLICT` в `_VERIFY`: `decision = NEEDS_VERIFICATION`, тир
  `NEEDS_OPERATOR`, причина с объяснением сохраняется. Раньше текст молча побеждал.
* Пороги scoring, проверки seniority, опыта, языка, роли не менялись.

Ревью Codex (8 раундов, каждый — REJECT с новыми состязательными примерами; все исправлены, кроме
того, что Codex не может запускать pytest в своей read-only песочнице). Ключевые находки и итог:
(1) эвристика «слова-льготы рядом» превращала реальную неполную занятость в `UNKNOWN` и открывала
автоматическую подготовку — удалена; (2) исключение «персонал компании» обходилось в «Voll- und
Teilzeit» — заменено списком разрешённых продолжений (после «Voll-/Teilzeit» должны идти слова о
**работе**, а не существительное-человек); (3) явные часы вакансии («20 hours», «75 %») и слова
«Teilzeitstelle/-position» сильнее любых слов-опций рядом, а «part-time position with … options» —
это всё ещё неполная занятость; (4) квадратичное время: границы клауз считаются один раз на текст
(bisect, семантика прежняя, remote-тесты и 716 сравнений с HEAD совпали), число проверок отрицания
ограничено, а при исчерпании бюджета ответ — `PART_TIME` (человек в контуре), не `UNKNOWN`/`FULL_TIME`;
(5) 400-символьное окно в `_clause` отклонено как меняющее remote-семантику — не используется.
**Итог ревью: формального ACCEPT нет.** Последний раунд (8) нашёл одну узкую проблему
(`möglich` как префикс `möglichst`), она исправлена и покрыта тестом, но повторно Codex не
проверял. Серия сошла к всё более узким формулировкам, но полностью исключить обход регулярными
выражениями нельзя — это эвристика; направление ошибок смещено в безопасную сторону, а
неопределённость уходит оператору.

Принцип: **ложный `PART_TIME` лишь оставляет вакансию оператору, ложный `UNKNOWN` открывает gate**;
«опцией» считаются только явные слова и фразы (`Teilzeitmöglichkeiten`, «nach Absprache möglich»,
«options available», «auch in Teilzeit», «Möglichkeit zur…»).

Намеренная асимметрия: сомнительное упоминание теперь даёт `UNKNOWN` (оператор решает), а не ложный
`FULL_TIME`. Остаточный шум идёт в безопасную сторону: перечень льгот вроде «Mobile Working & Teilzeit, Sabbaticals» и упоминания вроде «part-time volunteers» в описании компании остаются `PART_TIME` (вакансия остаётся на ручной проверке).

Результат для трёх вакансий: **705, 752, 1337 → `UNKNOWN`** (текст не называет часы вакансии;
Vollzeit **не** подтверждён и не утверждается).

## 3. Влияние на все 3 234 вакансии (одна и та же копия, старый и новый код)

| Метрика | До | После |
|---|---:|---:|
| work_time FULL_TIME | 670 | 693 |
| work_time PART_TIME | 259 | 134 |
| work_time UNKNOWN | 2 305 | 2 407 |
| work_time CONFLICT | 0 | 0 (флаг BA пуст) |
| Remote совместим + FULL_TIME | 8 | 8 |
| Junior Python/Backend в очереди | 8 | 8 |
| В ручной очереди | 682 | 682 |
| MANUAL_REVIEW | 668 | 668 |
| Автоматическая подготовка допустима (gate открыт) | 8 | 8 |
| NOT_SUITABLE | 2 141 | 2 141 |
| Новых Bewerbungen | 0 | 0 |

* Изменился `work_time` у **125** вакансий: 102 `PART_TIME → UNKNOWN`, 23 `PART_TIME → FULL_TIME`
  («Voll- oder Teilzeit»). Полный список — в §7.
* **Ни у одной** вакансии не изменились fit tier, triage, членство в очереди, Junior-группа и
  автоматический gate. Ни одна из 125 не получила права на автоматическую подготовку (все остаются
  заблокированными другими критериями).
* В предыдущем анализе упоминались 14 вакансий «Vollzeit + FULL_REMOTE»; на этой копии при
  определении «remote = COMPATIBLE и work_time = FULL_TIME» получается 8 (и до, и после). Расхождение
  не объяснено данными этой копии; цифра 14 не подтверждена.
* Остаётся 134 `PART_TIME`; выборка показывает в основном настоящие («Arbeitszeit: Teilzeit», «Teilzeit
  (75 %)», «Teilzeitstelle»).

## 4. Manual Review Queue (`/ui/review`)

* Состав после исправления **такой же**, как до: 682 вакансии, те же 8 Junior Python/Backend (705,
  752, 1337, 347, 328, 324, 310, 292); число подогнано не было — оно получено пересчётом.
* В колонке часов 705/752/1337 теперь `UNKNOWN`, а не `PART_TIME`. Ссылки на источник, дубли и
  порядок (глобальная сортировка, курсор) не менялись; страница остаётся read-only, форм действий нет
  (тесты PR #51/#52 проходят).
* Вакансии из очереди не исчезают из-за work-time: причины членства (маршрут, remote, роль) не
  зависят от `PART_TIME`.
* Вакансия «#3294 BFI» в копии отсутствует (максимальный id — 3 234); проверена эквивалентная
  по структуре тестовая фикстура. Bewerbung #25 (Leani, `SUBMITTED`) по-прежнему исключает свою
  вакансию из очереди (тест); #919 «Sr. Softwareentwickler» остаётся `NOT_SUITABLE`.

## 5. Холодный и тёплый кэш (копия, SQLite, Windows, один прогон)

| | Холодный (кэш устарел после изменения правил) | Тёплый (после штатного `stage21_assess_vacancies.py --apply`) |
|---|---|---|
| Первая страница | 3,6 с | 1,1 с |
| SQL-запросов (первая страница) | 1 294 | 37 |
| Живых оценок | 60 (потолок) | 0 |
| Показано / доступно | 16 записей | 682 записи |
| Не пересчитано | **3 174** — страница пишет об этом | 0 |
| Все 8 Junior найдены | да (фильтр junior, 3,2 с) | да (0,6 с) |
| Полный проход | недоступен (один неполный список) | 28 страниц, 17,6 с, 1 036 SQL, 682 уникальных id, дублей нет |
| Последующие страницы | — | ~0,6 с каждая |

* **Хеш правил.** Правка классификатора меняет хеш правил: все 3 234 строки кэша автоматически
  стали STALE (холодный сценарий — это именно он). Тест `test_a_changed_rules_hash_makes_every_cached_decision_stale`
  подменяет хеш и доказывает, что старые решения не используются, живая работа ограничена, а страница
  объявляет себя неполной.
* **Обновление кэша** (только на копии): 3 234 строки за ~2 мин 15 с; повторный запуск — `0 rows
  written` (идемпотентно); `PRAGMA integrity_check = ok`, `foreign_key_check` пусто; заявки (25) и
  события (54) побайтно совпадают с копией до обновления; alembic `a7c3e5f1b9d2`.
* **Alembic** на копии: `downgrade -1` (→ `d6e9a2b4c8f1`) и `upgrade head` без ошибок, данные целы.
* **Неполный список не выдаётся за полный.** При холодном кэше страница показывает число
  непересчитанных вакансий и команду offline-обновления.

## 6. Производительность и безопасность

* Event loop: маршрут — обычная `def`, выполняется в пуле потоков; тест это проверяет.
* **Новая находка (не P1, но важна):** 4 одновременных запроса первой страницы на тёплом кэше заняли
  по ~4,8 с каждый (одиночный — ~1 с): нагрузка CPU-bound (хеширование свежести) и упирается в GIL,
  запросы фактически выполняются последовательно. Для одного оператора это допустимо; для
  многопользовательской нагрузки нужен сохранённый индекс сортировки (схема + миграция) — отдельное
  решение. Результаты параллельных запросов идентичны.
* Массовых пересчётов без явного запуска нет; запись в БД из `/ui/review` невозможна
  (read-only соединение).

## 7. Все вакансии, у которых изменился work_time

«Evidence» — фрагмент текста, который вызвал старое решение (для `→ UNKNOWN`) или новое (для
`→ FULL_TIME`). Fit tier и triage не изменились ни у одной строки; столбцы показывают текущее
значение.

| JT id | Title | Employer | Old | New | Evidence (text excerpt) | Fit tier (unchanged) | Triage (unchanged) | Auto-prepare |
|---:|---|---|---|---|---|---|---|---|
| 45 | PhD Candidate / Doctoral Researcher – Lipidomics a | Universitätsklinikum Frankfu | PART_TIME | UNKNOWN | xation, sports and exercise  Part-time employment is possible, we of | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 56 | Wissenschaftlicher Mitarbeiterin (m/w/d) – Institu | Universitätsklinikum Frankfu | PART_TIME | UNKNOWN | a, Cafés  Work-Life-Balance, Teilzeitmöglichkeiten  Gesundheitsförderu | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 62 | Cloud Infrastructure Engineer (m/w/d) | zollsoft GmbH  | PART_TIME | UNKNOWN | fice ausgeübt werden. - Einer Teilzeitanstellung steht bei uns nichts  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 63 | IT-Administrator:in Linux | zollsoft GmbH  | PART_TIME | UNKNOWN | fice ausgeübt werden. - Einer Teilzeitanstellung steht bei uns nichts  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 68 | IT Security Engineer (m/w/d) | zollsoft GmbH  | PART_TIME | UNKNOWN | fice ausgeübt werden. - Einer Teilzeitanstellung steht bei uns nichts  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 69 | DevSecOps Engineer (m/w/d) | zollsoft GmbH  | PART_TIME | UNKNOWN | fice ausgeübt werden. - Einer Teilzeitanstellung steht bei uns nichts  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 78 | DevOps / Platform Engineer (m/w/d) – (full remote  | zollsoft GmbH  | PART_TIME | UNKNOWN | ut selbstbestimmt, denn einer Teilzeitanstellung steht bei uns nichts  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 92 | Operations Manager Expert (w/m/div.) | Robert Bosch GmbH | PART_TIME | UNKNOWN | odelle: von unterschiedlichen Teilzeitmöglichkeiten über mobiles Arbei | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 99 | Wissenschaftliche Mitarbeiterinnen /Wissenschaftli | BAV Dienstsitz Aurich | PART_TIME | UNKNOWN | Besetzung der Dienstposten in Teilzeit ist grundsätzlich möglich. | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 100 | Wissenschaftliche Mitarbeiterin /Wissenschaftliche | BAV Dienstsitz Aurich | PART_TIME | UNKNOWN | esetzung des Dienstpostens in Teilzeit ist grundsätzlich möglich. | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 136 | Systems & Software Integration Specialist (m/w/d) | BAV Dienstsitz Aurich | PART_TIME | UNKNOWN | esetzung des Dienstpostens in Teilzeit ist grundsätzlich möglich. | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 162 | Lead Architect - Snowflake (all genders, full-/par | Merck KGaA | PART_TIME | FULL_TIME | ect - Snowflake (all genders, full-/part-time) Weiterstadt Work Your M | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 165 | IT Operations Consultant (m/w/d) | FERCHAU GmbH Niederlassung D | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 178 | Wissenschaftliche Mitarbeiterin/wissenschaftlicher | Bundesinstitut für Bevölkeru | PART_TIME | UNKNOWN | für Beschäftigungsumfänge in Teilzeit geeignet. Wir freuen uns, wen | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 185 | DevOps Engineer (m/w/d) | FERCHAU GmbH Niederlassung D | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 198 | Wissenschaftlicher Mitarbeiter all genders im Bere | Fraunhofer-Gesellschaft e.V. | PART_TIME | UNKNOWN | les Arbeiten und verschiedene Teilzeitmodelle für eine optimale Balanc | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 203 | Senior Resource Manager (m/w/d) | Boehringer Ingelheim Pharma  | PART_TIME | UNKNOWN | nager Fixed term contract: no Part time eligible: yes (80%) Job Shari | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 205 | Senior Software Engineer Data (m/w/d/x) | Deutsche Telekom AG | PART_TIME | UNKNOWN | el) Diese Stelle ist auch in Teilzeit möglich. Schwerbehinderte Me | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 229 | Site Reliability Engineer (SRE) (m/w/d) | Deutsche Telekom AG | PART_TIME | UNKNOWN | is position is also available part-time. People with disabilities wil | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 360 | Quereinstieg Softwareentwicklung für Naturwissensc | zollsoft GmbH  | PART_TIME | UNKNOWN | bei uns zu arbeiten. - Einer Teilzeitanstellung steht auch grundsätzli | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 387 | Java Backend Entwickler:in (m/w/d) | Peoplesource GmbH | PART_TIME | UNKNOWN | beitszeiten - Möglichkeit zur Teilzeitbeschäftigung - Strukturierte Ei | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 395 | Backend Engineer T Cloud Public – Container (m/w/d | Deutsche Telekom AG | PART_TIME | UNKNOWN | ie! Diese Stelle ist auch in Teilzeit möglich. Schwerbehinderte Me | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 426 | iOS Entwickler:in (Homeoffice möglich) | zollsoft GmbH  | PART_TIME | UNKNOWN | weit arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 533 | Softwareentwickler:in Java (Homeoffice möglich) | zollsoft GmbH  | PART_TIME | UNKNOWN | u wo arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 535 | Senior Softwareentwickler Frontend (m/w/d) | FERCHAU GmbH Niederlassung D | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 548 | Senior Softwareentwickler:in Java (Homeoffice mögl | zollsoft GmbH  | PART_TIME | UNKNOWN | u wo arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 619 | Anwendungsberater:in | zollsoft GmbH  | PART_TIME | UNKNOWN | ner Seite voraussetzt. - Eine Teilzeitanstellung ist auch möglich, jed | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 641 | Frontend / Web Entwickler:in (Angular, Home Office | zollsoft GmbH  | PART_TIME | UNKNOWN | einer Region gründen. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 643 | Linux Entwickler:in (Homeoffice möglich) | zollsoft GmbH  | PART_TIME | UNKNOWN | weit arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 689 | Inhouse SAP-Entwickler (m/w/d) SAP ABAP | Bluewave Select GmbH | PART_TIME | FULL_TIME | SAP-Entwickler (m/w/d)\\ in Voll- oder Teilzeit. Die IT betreut die SA | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 705 | (Junior) Software Developer Python (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Du ha | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 706 | Anwendungsentwickler/-in (m/w/d) für Python (DevOp | BGHW - Berufsgenossenschaft  | PART_TIME | UNKNOWN | –, individuelle Arbeits- und Teilzeitmodelle, mobiles Arbeiten, Beratu | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 742 | Softwareentwickler (m/w/d) Mobilfunk 5G für die Si | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 746 | (Junior) Testingenieur / Software Tester (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Erfol | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 747 | Fullstack Developer (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 751 | Softwareentwickler C++ (m/w/d) Industrielle Bildve | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 752 | (Junior) Backend-Entwickler (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 753 | (Junior) DevOps Engineer (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Du ha | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 755 | Postdoktorandin | Universität Trier UTR allgem | PART_TIME | UNKNOWN | ergleichbar). Die Stelle ist teilzeitgeeignet. Die Universität Trier i | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 760 | DevOps Engineer (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Studi | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 773 | Praktikum im Bereich Digitalisierung und Automatis | Robert Bosch GmbH | PART_TIME | UNKNOWN | en Arbeitens sowie attraktive Teilzeitmodelle. Lebenslanges Lernen ist | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 774 | Experte Bildverarbeitung Medizintechnik (m/w/d) | FERCHAU GmbH Niederlassung M | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 801 | Softwareentwickler Embedded Systems - Security (m/ | FERCHAU GmbH Niederlassung G | PART_TIME | UNKNOWN | durch Gleitzeitregelungen und Teilzeitmöglichkeiten  Mehr als der Tari | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 804 | (Senior) Security Analyst (m/w/d) | REWE digital GmbH | PART_TIME | FULL_TIME | t: 51149 Köln / Vertragsart: Voll-/Teilzeit, unbefristet / Job-ID: 984 | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 810 | REFERATSLEITUNG (m/w/d) des Referats 14 „Informati | Aufsichts- u.Dienstl.Direkti | PART_TIME | UNKNOWN | nsch wird die Möglichkeit der Teilzeitbeschäftigung geprüft. Im Rahmen | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 858 | Linux Systemadministrator (m/w/d) | KT Abrechnungsdienste e.K. | PART_TIME | FULL_TIME | t Heidelberg, unbefristet, in Voll- oder Teilzeit (mind. 85%), zum näc | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 899 | PhD Student - DNA Break Signature in Cancers | Deutsches Krebsforschungszen | PART_TIME | UNKNOWN | ossibility of mobile work and part-time work - Family-friendly workin | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 925 | Senior Mechatronik Design Engineer – Halbleiter-Au | Robert Bosch GmbH | PART_TIME | UNKNOWN | odelle: von unterschiedlichen Teilzeitmöglichkeiten über mobiles Arbei | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 961 | Digital Solutions Engineer (w/m/div.) | Robert Bosch GmbH | PART_TIME | UNKNOWN | odelle: von unterschiedlichen Teilzeitmöglichkeiten über mobiles Arbei | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 981 | Wissenschaftler für Hyperspektraldatenanalyse und  | Fraunhofer-Gesellschaft e.V. | PART_TIME | UNKNOWN | nden. Die Stelle kann auch in Teilzeit besetzt werden. Die Stelle is | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1011 | Postdoctoral Researcher - Computational & Translat | Deutsches Krebsforschungszen | PART_TIME | UNKNOWN | orking hours - Possibility of part-time work - Family-friendly workin | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1071 | Wissenschaftlicher Mitarbeiter - Experte für vertr | Fraunhofer-Gesellschaft e.V. | PART_TIME | UNKNOWN | rebt. Die Stelle kann auch in Teilzeit besetzt werden. Anstellung, V | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1072 | Research Software Engineer im Bereich Mobile Senso | Fraunhofer-Gesellschaft e.V. | PART_TIME | UNKNOWN | nden. Die Stelle kann auch in Teilzeit besetzt werden. Die Stelle is | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 1091 | (Senior) Security Platform Engineer (m/w/d) | REWE digital GmbH | PART_TIME | FULL_TIME | t: 51149 Köln / Vertragsart: Voll-/Teilzeit, unbefristet / Job-ID: 978 | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1116 | Softwareentwickler:in Machine-/Deep-Learning (Home | zollsoft GmbH  | PART_TIME | UNKNOWN | u wo arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1180 | IT Architekt und Consultant für Datenmanagement (w | zeb | PART_TIME | UNKNOWN | Für Festangestellte sind auch Teilzeitmodelle möglich: Unter Berücksic | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1223 | Risk Data & Exposure Reporting Analyst (w/m/d) | TARGOBANK AG | PART_TIME | FULL_TIME | rweiterbar). - Sie können in Voll- oder Teilzeit arbeiten, Homeoffice  | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 1229 | (Senior) Analytics Berater (SAP BDC inkl. SAC & Da | W. Brandenburg GmbH & Co.oHG | PART_TIME | FULL_TIME | nkfurt am Main / Vertragsart: Voll-/Teilzeit, unbefristet / Job-ID: 96 | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1266 | Web-Entwickler (m/w/d) für abwechslungsreiche Proj | CREAVO | PART_TIME | FULL_TIME | befristeter Arbeitsvertrag. - Voll- oder Teilzeit (25 bis 40 Stunden/W | STRETCH | STATED_GAP | blocked |
| 1267 | PHP-Entwickler (m/w/d) für abwechslungsreiche Proj | CREAVO | PART_TIME | FULL_TIME | befristeter Arbeitsvertrag. - Voll- oder Teilzeit (25 bis 40 Stunden/W | STRETCH | STATED_GAP | blocked |
| 1273 | IT Specialist (w/m/d) - Schwerpunkt Enablement für | KfW | PART_TIME | UNKNOWN | -Stunden-Woche, individuellen Teilzeitmodellen, Home Office & Workatio | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1316 | (Senior) Platform Engineer - Kubernetes, Private A | OEDIV | PART_TIME | UNKNOWN | itzeitkonto und verschiedenen Teilzeitmodellen Der Hauptsitz von OEDIV | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1324 | Senior Software Engineer (m/w/d/x) – Broadband CPE | Deutsche Telekom AG | PART_TIME | UNKNOWN | ten Diese Stelle ist auch in Teilzeit möglich. Schwerbehinderte M | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1337 | Junior Backend Engineer (m/w/d) | Airbus Defence and Space Gmb | PART_TIME | UNKNOWN | s (flextime), mobile working, part-time options, job sharing, and sab | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 1349 | (Junior) Consultant (m/w/d) im Geschäftsfeld Energ | Becker Büttner Held PartG mb | PART_TIME | UNKNOWN | gestaltung, mobilem Arbeiten, Teilzeitmodellen, Lohnfortzahlung für di | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1408 | IT-Administrator / Systemadministrator (m/w/d) | TBZ-Pariv Technologieberatun | PART_TIME | FULL_TIME | unden. Diese Tätigkeit ist in Voll- oder Teilzeit möglich. ### Vorauss | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1467 | IT-Systemadministrator (w/m/d) | GLR Gesellschaft für Leit- u | PART_TIME | FULL_TIME | ystemadministrator (w/m/d) in Voll- oder Teilzeit ### DEINE MISSION -  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1491 | Software Developer/Softwareentwicklerin für ProSto | TEAM GmbH | PART_TIME | FULL_TIME | echnologien Festanstellung, Voll- oder Teilzeit · Paderborn, Remote Da | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 1517 | Data Engineer (m/w/d) | ADS Allg Deutsche Steuerbera | PART_TIME | FULL_TIME | t, keine Remote-Tätigkeit) in Voll- oder Teilzeit Du stehst bei uns im | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 1527 | Lead Developer Full Stack (w/m/d) | Griepenstroh Personalvermitt | PART_TIME | FULL_TIME | nft: Unbefristeter Vertrag in Voll- oder Teilzeit - Work-Life-Balance: | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1541 | Consultant m/w/d für CSV, CSA und Prozesse | sepp.med gmbh | PART_TIME | UNKNOWN | lichkeiten für Homeoffice und Teilzeitmodelle unterstützen wir deine W | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 1645 | Senior Consultant Dokumentenmanagement mit fachlic | Komm.ONE AöR Standort Stuttg | PART_TIME | UNKNOWN | en Team • Möglichkeiten für Teilzeitbeschäftigungen sowie das Arbeiten | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1696 | 732 Wissenschaftliche_r Mitarbeiter_in Epidemiolog | Helmholtz-Zentrum für Infekt | PART_TIME | UNKNOWN | exible Arbeitszeitgestaltung, Teilzeitmodelle sowie Homeoffice - Eine  | NEEDS_OPERATOR | ROLE_UNCLEAR | blocked |
| 1698 | DevOps-Entwickler m/w/d in Forchheim | sepp.med gmbh | PART_TIME | UNKNOWN | lichkeiten für Homeoffice und Teilzeitmodelle unterstützen wir deine W | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 1707 | Specialist (w/m/d) Datenmanagement & MIS | TARGOBANK AG | PART_TIME | FULL_TIME | : Du hast die Möglichkeit, in Voll- oder Teilzeit zu arbeiten (ab 20 S | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 1730 | Data Product Engineer (m/w/d) | REWE Group | PART_TIME | FULL_TIME | Ort: 50933 Köln /Vertragsart:Voll-/Teilzeit, unbefristet /Job-ID: 9535 | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 1736 | Junior Softwareentwickler (m/w/d) | FERCHAU GmbH Niederlassung F | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 1758 | Softwareentwickler:in tomedo® (full remote möglich | zollsoft GmbH  | PART_TIME | UNKNOWN | ndweit arbeiten willst. Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1769 | DevOps Engineer (m/w/d) | FERCHAU GmbH Niederlassung D | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1810 | Embedded Software-Entwickler (m/w/d) | FERCHAU GmbH Niederlassung M | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1878 | Kotlin Backend Engineer - Energy Tech / IoT (m/w/d | metiundo | PART_TIME | UNKNOWN | exible Vertrauensarbeitszeit, Teilzeitoption, mindestens zwei Tage mob | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 1916 | Software Architekt (m/w/d) | LinoPro GmbH | PART_TIME | FULL_TIME | möchte. Anstellung erfolgt in Voll- oder Teilzeit. Deine Aufgaben • Au | NEEDS_OPERATOR | REMOTE_UNVERIFIED | blocked |
| 1934 | Embedded Software / Firmware Developer (m/w/d) | secunet Security Networks AG | PART_TIME | UNKNOWN | auensarbeitszeit und flexible Teilzeitmodelle. Da ist für jeden das pa | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1945 | Software Engineer - AI Powered Development (m/w/d) | WSW Software | PART_TIME | FULL_TIME | geben Dir die Möglichkeit, in Voll- oder Teilzeit zu arbeiten. Wir wer | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 1947 | Software Engineer - AI Powered Development (m/w/d) | WSW Software GmbH | PART_TIME | FULL_TIME | geben Dir die Möglichkeit, in Voll- oder Teilzeit zu arbeiten. Wir wer | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 1993 | (Senior) SAP Entwickler ABAP (m/w/d) | W. Brandenburg GmbH & Co.oHG | PART_TIME | FULL_TIME | nkfurt am Main / Vertragsart: Voll-/Teilzeit, unbefristet / Job-ID: 96 | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2071 | IT Application Manager / Specialist für medizinisc | Universitätsklinikum Frankfu | PART_TIME | UNKNOWN | a, Cafés  Work-Life-Balance, Teilzeitmöglichkeiten  Gesundheitsförderu | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 2159 | Administrative Lab Technician | Deutsches Krebsforschungszen | PART_TIME | UNKNOWN | ing payments - Possibility of part-time work - Family-friendly workin | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 2161 | Laboratory Technician / Laboratory Manager | Deutsches Krebsforschungszen | PART_TIME | UNKNOWN | ing payments - Possibility of part-time work - Family-friendly workin | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 2228 | IT-Systemadministrator Onsite Support (m/w/d) | KION Group AG | PART_TIME | UNKNOWN | sics: Flexible Arbeitszeiten, Teilzeitmodelle, Möglichkeit zum regelmä | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2269 | IT-Anwendungsberater (m/w/d) fürr HRIS (Human Reso | WISAG Job & Karriere GmbH &  | PART_TIME | FULL_TIME | rid - Wähle deine Arbeitszeit:Voll- oder Teilzeit mit Vertrauensarbeit | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 2285 | SAP-Berater (m/w/d) | FERCHAU GmbH Niederlassung G | PART_TIME | UNKNOWN | durch Gleitzeitregelungen und Teilzeitmöglichkeiten  Mehr als der Tari | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2441 | Leitender Expertin Robotik (w/m/div.) | Robert Bosch GmbH | PART_TIME | UNKNOWN | itsmodelle: von verschiedenen Teilzeitoptionen über mobiles Arbeiten b | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2469 | Embedded Softwareentwickler (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten  Sehr gute Deutsch | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2485 | Softwareentwickler (m/w/d) Mobile Apps | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 2486 | (Junior) Embedded Softwareentwickler (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2498 | Softwareentwickler:in C# / .NET (m/w/d) | DV Trend Service GmbH | PART_TIME | UNKNOWN | rkeit von Familie und Beruf - Teilzeit möglich - Homeoffice nach erf | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2531 | (Senior) Software Entwickler C#/Angular (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2537 | COBOL Softwareentwickler (m/w/d) - z/OS COBOL Entw | Ratbacher GmbH | PART_TIME | UNKNOWN | Lebensumstände erfordern ein Teilzeitmodell? Kein Problem, Ihr zukünft | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2543 | SPS-Entwickler / Softwareentwickler (m/w/d) | Syntegon Technology GmbH | PART_TIME | UNKNOWN | s mobilen Arbeitens - Diverse Teilzeitmöglichkeiten, Kinderbetreuungsz | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2561 | (Senior-) Embedded-Software-Entwickler C/C++ (m/w/ | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2608 | (Senior) Software Developer Java (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Du ha | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2609 | (Junior) Software Developer Java (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Du ha | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 2610 | (Junior) C#.net-Entwickler (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 2617 | (Senior) Embedded Software Developer C/C++ (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2618 | (Junior) Embedded Software Developer C/C++ (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2619 | (Junior) Software Developer C#.NET (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Du ha | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 2620 | Softwareentwickler im Bereich der Datenanalyse von | Fraunhofer SCAI | PART_TIME | UNKNOWN | ch flexible Arbeitszeiten und Teilzeitmodelle - Unterstützungsangebote | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2636 | IT-Systemadministrator:in | zollsoft GmbH  | PART_TIME | UNKNOWN | er Seite voraussetzt. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2638 | Software Developer (m/w/d) Rust | secunet Security Networks AG | PART_TIME | UNKNOWN | auensarbeitszeit und flexible Teilzeitmodelle. Da ist für jeden das pa | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2663 | IT-Projektkoordinator:in Software-Einführung | zollsoft GmbH  | PART_TIME | UNKNOWN | er Seite voraussetzt. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2672 | Junior Customer Success Manager:in (deutschlandwei | zollsoft GmbH  | PART_TIME | UNKNOWN | uns auf Deinen Input. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2673 | Fullstack-Entwickler:in Schwerpunkt Webentwicklung | zollsoft GmbH  | PART_TIME | UNKNOWN | u wo arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2678 | Senior Produktmanager:in für tomedo/ tomedo.air (H | zollsoft GmbH  | PART_TIME | UNKNOWN | weit arbeiten willst. - Einer Teilzeitanstellung steht auch grundsätzl | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2857 | Change Manager (w/m/d) | DHL AG Zentrale | PART_TIME | UNKNOWN | working arrangements, such as part-time options or remote work possib | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2865 | Scientific coordinator (m/f/d) | Universitätsklinikum Bonn Aö | PART_TIME | FULL_TIME | cutive year. The following full-/part-time position (38.5/19.5 hours p | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2871 | Softwaretester / Software Test Engineer (m/w/d) | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 2872 | (Senior) Softwaretester (m/w/d) Testautomatisierun | FERCHAU GmbH Niederlassung K | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten Dein Profil  Abges | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2876 | Systemintegrator (w/m/div.) | Robert Bosch GmbH | PART_TIME | UNKNOWN | eitens sowie unterschiedliche Teilzeitmodelle bis hin zum Jobsharing.  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 2993 | Wissenschaftlicher Mitarbeiter (w/m/d) Promotion i | Technische Hochschule Nürnbe | PART_TIME | UNKNOWN | Gerne können Sie bei uns in Teilzeit arbeiten, wenn durch Job-Shar | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 3060 | NTT DATA Business Solutions Global Managed Service | NTT DATA Business Solutions  | PART_TIME | FULL_TIME | Formula Heidelberg, Neckar In Voll- oder Teilzeit (ab 80%) Als NTT DAT | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 3069 | IT Operations Consultant (m/w/d) | FERCHAU GmbH Niederlassung D | PART_TIME | UNKNOWN | ng durch Gleitzeitregelungen, Teilzeitmöglichkeiten und z. T. Vertraue | NEEDS_OPERATOR | ROUTE_REVIEW_REQUIRED | blocked |
| 3136 | Data Engineer (m/w/d) – Python & BI Automatisierun | Autowork | PART_TIME | UNKNOWN | exibles Arbeiten: in diversen Teilzeitmodellen, Hybrid, mobil oder in  | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
| 3207 | Fachinformatiker (m/w/d) Digitale Forensik Würzbur | Polizeipräsidium Unterfranke | PART_TIME | UNKNOWN | Stelle kann grundsätzlich mit Teilzeitbeschäftigten besetzt werden, so | NEEDS_OPERATOR | NOT_TARGET_ROLE | blocked |
| 3225 | Senior Data Analytics Operations Manager | Boehringer Ingelheim Pharma  | PART_TIME | UNKNOWN | or Biberach This position is part time eligible with 80 %. Tasks & r | NOT_SUITABLE | CONFIRMED_BARRIER | blocked |
