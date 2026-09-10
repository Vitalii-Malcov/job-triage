# Interview Guide — JobTriage

~40 questions grounded in this actual codebase. Each has: a short, B1/B2-level German answer; a more technical Russian answer; and a concrete project example (module/function). German answers are kept simple on purpose — do not over-study them beyond B1/B2 level.

---

## Python

### 1. Warum wird `multiprocessing` statt `threading` für die DNS-Auflösung benutzt?

**DE (B1/B2):** Ein blockierender Systemaufruf wie DNS-Auflösung kann man in Python nicht einfach in einem Thread abbrechen — der Thread bleibt hängen. Ein Betriebssystem-Prozess kann man aber wirklich beenden (killen). Deshalb läuft die DNS-Auflösung in einem eigenen Prozess mit einer festen Zeitgrenze.

**RU (технически):** `socket.getaddrinfo()` — блокирующий C-уровневый системный вызов; поток Python, застрявший в нём, нельзя принудительно прервать (нет безопасного способа прервать блокирующий syscall изнутри потока). Только уровень ОС — `SIGTERM`/`SIGKILL` (или `TerminateProcess` на Windows) — гарантированно останавливает выполнение. Поэтому DNS-резолвинг изолирован в дочернем процессе (`multiprocessing.get_context("spawn")`), который можно детерминированно завершить и дождаться (`join()`) как мёртвый.

**Project example:** `app/providers/email/imap_deadline.py::resolve_addrinfo_bounded`.

### 2. Was bedeutet "late binding" / Scoping-Problem, das die `resolve_job_label`-Funktion vermeidet?

**DE:** Wenn eine Variable irgendwo in einer Funktion zugewiesen wird, behandelt Python sie in der GANZEN Funktion als lokal — auch vor der Zuweisung. Wenn eine Funktion `job_label` heißt und `job_label` als lokale Variable zurückgibt, gibt es einen `UnboundLocalError`.

**RU:** Присвоение имени в любом месте тела функции делает его локальным для ВСЕЙ функции целиком (не только после строки присвоения) — если снаружи вызывается функция с тем же именем `job_label`, а внутри есть `job_label = job_label(...)`, интерпретатор считает `job_label` в правой части ещё не определённой локальной переменной → `UnboundLocalError`. Решение — дать функции другое имя.

**Project example:** `app/agents/letter_content.py::resolve_job_label` — named to avoid collision with the local variable `job_label` at both call sites.

### 3. Warum benutzt der Rate Limiter `time.monotonic()` statt `time.time()`?

**DE:** `time.time()` zeigt die "Wanduhr"-Zeit und kann sich ändern (z. B. durch NTP-Synchronisation oder manuelle Änderung). `time.monotonic()` läuft immer nur vorwärts und ist deshalb sicher, um Zeitspannen zu messen.

**RU:** `time.time()` может скачком измениться назад или вперёд (коррекция системных часов, NTP, ручная правка) — это ломает вычисление "сколько времени прошло" для sliding-window rate limiting: скачок часов назад мог бы искусственно продлить окно или обнулить его. `time.monotonic()` гарантированно монотонно неубывает в пределах процесса, поэтому разница `now - timestamp` всегда корректна.

**Project example:** `app/security/rate_limit.py::_RateLimiter.check`.

### 4. Warum ist `python:3.13-slim` als Docker-Basis-Image gewählt und nicht `python:3.13`?

**DE:** Das `slim`-Image ist viel kleiner, weil es keine unnötigen Build-Tools enthält. Das Projekt braucht keinen Compiler, weil `psycopg[binary]` ein fertiges Wheel mitbringt.

**RU:** `psycopg[binary]` — прекомпилированное wheel-распространение с уже статически слинкованным libpq, поэтому компилятор C/сборочные заголовки не нужны на этапе установки зависимостей — можно взять `slim`-образ (меньше размер, меньше поверхность атаки) вместо полного `python:3.13`.

**Project example:** `Dockerfile`, `docs/DOCKER.md` "Runtime facts gathered" table.

---

## FastAPI

### 5. Warum FastAPI für dieses Projekt?

**DE:** FastAPI erstellt automatisch eine API-Dokumentation aus dem Code, validiert Eingaben mit Pydantic und unterstützt asynchrone Programmierung. Das passt gut zu einem Projekt mit vielen externen Aufrufen (E-Mail, Telegram, externe APIs).

**RU:** FastAPI даёт декларативную валидацию запросов/ответов через Pydantic "из коробки", автогенерацию OpenAPI-схемы, встроенную систему Dependency Injection (`Depends`) и нативную поддержку `async`/`await` — важно, так как приложение делает много I/O-bound внешних вызовов (IMAP, SMTP, HTTP к Bundesagentur, Telegram Bot API).

**Project example:** `app/main.py`, `app/api/routes.py`.

### 6. Was ist Dependency Injection in diesem Projekt konkret?

**DE:** FastAPI kann Funktionen automatisch aufrufen und das Ergebnis in eine Route einfügen — zum Beispiel eine Datenbankverbindung oder eine API-Key-Prüfung. Man schreibt das einmal und benutzt es in vielen Routen wieder.

**RU:** `Depends(...)` — механизм, при котором FastAPI сам вызывает функцию-зависимость перед основным обработчиком и передаёт результат как параметр (или, для функций без возврата, просто исполняет её ради побочного эффекта — например, чтобы поднять исключение при неверном ключе). Используется как для получения ресурса (сессии БД), так и для чистых проверок (аутентификация, rate limiting) без явного возвращаемого значения.

**Project example:** `Depends(get_db)` (`app/db/session.py`), `Depends(require_api_key)` (`app/security/auth.py`), `Depends(enforce_*_rate_limit)` (`app/security/rate_limit.py`) — all wired in `app/api/routes.py`.

### 7. Warum hat `routes.py` keinen zentralen Exception Handler?

**DE:** Historisch ist jede Route selbst für die Umwandlung von Fehlern in HTTP-Antworten verantwortlich. Das führt zu Wiederholung (Duplikation), aber es ist sicher und einfach nachzuvollziehen. Eine Zentralisierung ist geplant, aber noch nicht umgesetzt.

**RU:** Сейчас каждый route-обработчик содержит свой `try/except`, транслирующий доменные исключения в `HTTPException` — это даёт 118 мест вызова `raise HTTPException` и минимум 12 дословно продублированных блоков. Централизация через `@app.exception_handler` возможна, но требует пофайлового сравнения статус-кодов/текста ошибки по каждому типу исключения, чтобы не изменить наблюдаемое поведение — сознательно отложено на отдельный review.

**Project example:** `docs/REFACTORING_BACKLOG.md` Part 2.

### 8. Warum ist `/health` nicht genug für einen echten Produktions-Health-Check?

**DE:** `/health` gibt immer `{"status": "ok"}` zurück, ohne die Datenbank zu prüfen. Wenn die Datenbank ausfällt, sagt die API trotzdem "ok" — das ist irreführend.

**RU:** `GET /health` — статичный ответ без проверки зависимостей (`app/api/routes.py:267-269`); при недоступной БД эндпоинт всё равно вернёт `200 {"status": "ok"}`, оркестратор/балансировщик получит ложноположительный сигнал здоровья и продолжит слать трафик на нерабочий инстанс. Зафиксировано как AUD-002, но не исправлено в этой сессии — изменение публичного ответа эндпоинта требует отдельного ревью.

**Project example:** `docs/PRODUCTION_READINESS_AUDIT.md` AUD-002.

---

## Pydantic

### 9. Warum Pydantic statt einfacher Dictionaries?

**DE:** Pydantic prüft automatisch, ob Daten das richtige Format haben (z. B. ob ein Feld wirklich eine Zahl ist), und gibt klare Fehlermeldungen. Das verhindert viele Fehler, die man sonst erst später bemerken würde.

**RU:** Pydantic даёт типизированную валидацию на границе системы (запрос/ответ API, `Settings` из переменных окружения) с понятными ошибками при несоответствии типа/формата, а не тихим падением где-то глубже в бизнес-логике. `pydantic-settings` дополнительно валидирует конфигурацию при старте приложения, а не при первом использовании нужного поля.

**Project example:** `app/models/` (DTOs), `app/core/config.py::Settings`.

### 10. Wie unterscheiden sich Pydantic-Modelle von den SQLAlchemy-Modellen?

**DE:** Pydantic-Modelle sind für Daten, die durch die API rein oder raus gehen. SQLAlchemy-Modelle sind für Daten, die in der Datenbank gespeichert werden. Sie sind absichtlich getrennt.

**RU:** `app/models/` — Pydantic DTO/domain-типы для API-контракта; `app/db/models.py` — SQLAlchemy ORM-модели для персистентности. Разделение намеренное (хотя местами `app/providers` всё же напрямую импортирует ORM-модели — известный архитектурный найдинг AUD-015) — API-контракт не должен меняться синхронно со схемой БД.

**Project example:** `app/models/candidate_profile.py` vs. `app/db/models.py::CandidateProfileRecord`.

### 11. Was ist `field_trust`/Provenance in Pydantic-Begriffen?

**DE:** Jedes wichtige Feld im Kandidatenprofil hat eine Markierung, woher die Information kommt — zum Beispiel "vom Nutzer bestätigt" oder "nur eine Vermutung". Nur bestätigte Fakten dürfen in generierten Texten benutzt werden.

**RU:** Каждое top-level поле `CandidateProfileResponse` несёт `SourceType` (`FACT`/`INFERENCE`/`IMPORTED`/`UNKNOWN`) — Pydantic-модель прямо кодирует происхождение данных как часть контракта, а не просто хранит значение. `is_top_level_fact_usable_for_generation` — явный gate, решающий, годится ли поле для генерации, на основе именно этого провенанса.

**Project example:** `app/models/candidate_profile.py`.

---

## SQLAlchemy

### 12. Warum Repository-Pattern statt direkter Datenbankabfragen in den Routen?

**DE:** Wenn alle Datenbankabfragen an einem Ort (Repository) gesammelt sind, ist der Code leichter zu testen und zu ändern. Die Route muss nicht wissen, wie die Abfrage genau funktioniert.

**RU:** Инкапсуляция SQL/ORM-запросов в отдельном репозиторном слое (`app/db/*_repository.py`) отделяет "что делать" (бизнес-логика в `services`) от "как это сохранено" (детали ORM/схемы) — упрощает unit-тестирование сервисов и изменение схемы БД без правки вызывающего кода в нескольких местах.

**Project example:** `app/db/automation_repository.py`, `app/db/response_draft_repository.py`.

### 13. Warum kein `pool_pre_ping` im Datenbank-Engine — und warum ist das ein Problem?

**DE:** Ohne `pool_pre_ping` merkt SQLAlchemy nicht, wenn eine gespeicherte Datenbankverbindung schon "tot" ist (z. B. nach einem Neustart der Datenbank). Der nächste Request bekommt dann einen Fehler, statt dass SQLAlchemy die Verbindung automatisch erneuert.

**RU:** `create_engine(...)` в `app/db/session.py` вызывается без `pool_pre_ping=True` — при простое соединения (рестарт БД, обрыв firewall'ом, recycle у managed Postgres) следующий checkout из пула вернёт "мёртвое" соединение, что приведёт к необработанному `OperationalError` вместо прозрачной пере-проверки/пересоздания соединения SQLAlchemy. Найдено и исправлено в этой ветке (AUD-007).

**Project example:** `app/db/session.py`, `docs/PRODUCTION_READINESS_AUDIT.md` AUD-007.

### 14. Wie funktioniert `get_db()` als FastAPI-Dependency?

**DE:** `get_db()` öffnet eine Datenbank-Sitzung, gibt sie an die Route weiter und schließt sie danach immer — egal ob die Route erfolgreich war oder einen Fehler hatte.

**RU:** Generator-функция с `yield` внутри `try/finally` — `db.close()` в `finally` гарантированно выполнится независимо от исхода обработчика (успех или исключение); FastAPI управляет жизненным циклом генератора как dependency с `yield` автоматически, вызывая код после `yield` при завершении запроса.

**Project example:** `app/db/session.py::get_db`.

### 15. Warum importieren manche Repository-Module aus `app/services`? Ist das ein Problem?

**DE:** Eigentlich sollten Repositories "unten" in der Architektur stehen und nichts aus "oben" (Services) importieren. Aktuell passiert das an ein paar Stellen — kein aktueller Fehler, aber ein Risiko für die Zukunft.

**RU:** Обнаружено (AUD-014), что некоторые репозитории импортируют чистые типы/константы из слоя `services` (например `app/db/gmail_analysis_repository.py` импортирует `ClassificationEvidenceItem` из `app.agents.email_classifier` и типы из `app.services.email_matching`) — инвертированное направление зависимостей. Сейчас безопасно (импортируются только чистые, не имеющие сайд-эффектов типы), но повышает риск циклического импорта при будущих изменениях. Зафиксировано в backlog, не исправлено.

**Project example:** `docs/ARCHITECTURE.md` "Dependency-direction review".

---

## PostgreSQL

### 16. Warum läuft CI gegen echtes PostgreSQL, nicht nur SQLite?

**DE:** SQLite und PostgreSQL verhalten sich bei gleichzeitigem Zugriff (Concurrency) unterschiedlich. Ein Test, der nur mit SQLite läuft, kann einen echten Fehler in PostgreSQL übersehen.

**RU:** SQLite и PostgreSQL расходятся именно в вопросах, критичных для этого проекта: порядок выделения последовательностей (`SERIAL`/identity) относительно момента коммита, семантика блокировок при конкурентных `UPDATE`, поведение partial unique index. CAS-логика планировщика (`claim_due_schedule`) и логика гонки Gmail watermark проверяются в `tests/integration/test_scheduler_postgres_concurrency.py` и `test_gmail_watermark_postgres_concurrency.py` именно против реального `postgres:16` сервис-контейнера в CI, а не только SQLite.

**Project example:** `.github/workflows/ci.yml` job `scheduler-postgres`.

### 17. Warum wird SQLite trotzdem als Standard für die lokale Entwicklung benutzt?

**DE:** SQLite braucht keine separate Installation oder einen laufenden Server — man kann sofort anfangen zu entwickeln. Für die Produktion wird PostgreSQL empfohlen.

**RU:** SQLite — файл, не требует отдельного сервера/учётных данных, что снижает трение для локальной разработки и большинства модульных тестов; для production рекомендован PostgreSQL (`docs/DEPLOYMENT.md`), а образ Docker вообще не поддерживает SQLite в compose-цели намеренно ("do not use SQLite in production compose").

**Project example:** `.env.example` (`DATABASE_URL=sqlite:///./job_search.db` default), `app/db/session.py` (`connect_args={"check_same_thread": False}` only for `sqlite://`).

### 18. Was bedeutet "PostgreSQL ist noch nicht produktionserprobt" in diesem Projekt?

**DE:** PostgreSQL wird in den Tests intensiv geprüft, aber der Entwickler hat es noch nicht selbst über längere Zeit in einer echten Produktionsumgebung laufen lassen. Das wird ehrlich so dokumentiert, statt es zu verschweigen.

**RU:** Честная формулировка ограничения, а не баг: миграционная цепочка и concurrency-инварианты проверены в CI против реального PostgreSQL, но сам автор ещё не эксплуатировал приложение на PostgreSQL длительное время как реальную production-нагрузку. Задокументировано явно (AUD-011), а не скрыто за общими фразами о "готовности к продакшену".

**Project example:** `docs/PRODUCTION_READINESS_AUDIT.md` AUD-011, `docs/DEPLOYMENT.md` "Scaling limitations".

---

## Alembic

### 19. Warum Alembic statt `Base.metadata.create_all()`?

**DE:** `create_all()` erstellt Tabellen nur, wenn sie noch nicht existieren — es kann bestehende Tabellen nicht sicher ändern. Alembic speichert jede Schemaänderung als eigenen, nachvollziehbaren Schritt (Migration), den man auch rückgängig machen kann.

**RU:** `create_all()` идемпотентен только для "ещё не существует" — не умеет накатывать пошаговые изменения (`ALTER TABLE`, переименование колонки, добавление индекса на непустую таблицу) и не имеет истории/возможности отката. Alembic хранит явную цепочку версионированных миграций (`alembic/versions/`), каждая со своим `upgrade()`/`downgrade()`, что даёт воспроизводимость схемы и путь отката.

**Project example:** `alembic/versions/` (30 files), `alembic/env.py`.

### 20. Wie stellt CI sicher, dass die Migrationskette wirklich funktioniert?

**DE:** CI startet eine leere echte PostgreSQL-Datenbank, führt alle Migrationen aus und prüft danach, ob die Datenbank wirklich beim erwarteten letzten Stand (`head`) angekommen ist.

**RU:** Job `scheduler-postgres` в CI поднимает пустой `postgres:16` сервис-контейнер, выполняет `python -m alembic upgrade head`, затем `alembic current` и через `grep -q "c7d3f9a1e5b8 (head)"` проверяет, что фактически достигнутая версия совпадает с ожидаемым head — что отличается от простого запуска `create_all()`, который бы "прошёл" даже при сломанной цепочке миграций.

**Project example:** `.github/workflows/ci.yml` step "Alembic upgrade head against CI PostgreSQL".

### 21. Warum ist `ALEMBIC_AUTO_UPGRADE` standardmäßig `false`?

**DE:** Migrationen automatisch beim Start laufen zu lassen ist bequem für die lokale Entwicklung, aber gefährlich in der Produktion — mehrere gleichzeitig startende Prozesse könnten versuchen, die Datenbank gleichzeitig zu ändern. In der Produktion soll das ein bewusster, expliziter Schritt sein.

**RU:** Автоматический запуск миграций при старте приложения удобен для dev/test, но опасен в production: при нескольких одновременно стартующих инстансах/воркерах миграция может запуститься гонкой несколько раз, а сбой миграции окажется скрыт за обычным стартом приложения. Поэтому по умолчанию `false`, и `docs/DOCKER.md`/`docs/DEPLOYMENT.md` предписывают явный отдельный шаг `docker compose run --rm web alembic upgrade head` перед стартом `web`.

**Project example:** `app/db/session.py::run_migrations_if_enabled`.

---

## Transactions

### 22. Was ist eine Transaktion, einfach erklärt?

**DE:** Eine Transaktion ist eine Gruppe von Datenbankänderungen, die entweder komplett gelingen oder komplett rückgängig gemacht werden. Es gibt nie einen Zustand "halb gespeichert".

**RU:** Атомарная единица работы с БД: набор изменений либо коммитится целиком, либо откатывается целиком (свойство "A" из ACID) — не существует промежуточного, частично применённого состояния, видимого другим соединениям.

**Project example:** every `db.commit()` call site in `app/db/*_repository.py`.

### 23. Warum wird kein großer Datenbank-Transaktionsblock für den ganzen Automatisierungszyklus benutzt?

**DE:** Der Automatisierungszyklus ruft auch externe Dienste auf (E-Mail, externe APIs) — die kann man nicht "zurückrollen", wenn etwas schiefgeht. Deshalb ist jeder Schritt einzeln gespeichert und so gebaut, dass ein Wiederholen sicher ist (idempotent), statt sich auf eine einzige riesige Transaktion zu verlassen.

**RU:** Внешние побочные эффекты (реальный HTTP-запрос, IMAP-фетч, отправка письма) нельзя откатить транзакцией БД — поэтому `run_automation_cycle` состоит из независимо коммитящихся шагов, каждый из которых спроектирован безопасным для повторного выполнения (уникальные ограничения, CAS, курсоры), а не оборачивается в одну "всё или ничего" транзакцию, которая всё равно не могла бы откатить уже отправленное письмо.

**Project example:** `app/services/automation.py::run_automation_cycle`.

---

## Concurrency

### 24. Was ist Compare-And-Swap (CAS) in diesem Projekt?

**DE:** CAS bedeutet: "Ändere diesen Datenbank-Eintrag nur, wenn er noch genau den erwarteten Wert hat." Wenn ein anderer Prozess ihn schon geändert hat, passiert nichts — kein Konflikt, kein doppeltes Ausführen.

**RU:** Атомарный `UPDATE ... WHERE column = :observed_value` — обновление применяется только если наблюдаемое значение всё ещё актуально на момент выполнения в БД; если другой процесс уже успел его изменить, `UPDATE` не затронет ни одной строки (0 affected rows), и вызывающий код это обнаруживает и не считает claim успешным. Гарантия атомарности обеспечивается самой СУБД, а не блокировкой на уровне приложения.

**Project example:** `app/db/automation_schedule_repository.py::claim_due_schedule`.

### 25. Warum können zwei gleichzeitig laufende Scheduler-Prozesse gefährlich sein — und wie verhindert das Projekt das?

**DE:** Wenn zwei Scheduler gleichzeitig denselben Automatisierungszyklus starten, könnten Jobs doppelt bearbeitet oder doppelt E-Mails verschickt werden. Das Projekt verhindert das nicht durch "Versprechen im Code", sondern durch eine echte Datenbank-Regel: nur ein laufender Zyklus pro Account ist gleichzeitig erlaubt.

**RU:** Два одновременных `python -m app.scheduler` (или гонка нескольких uvicorn-воркеров, если бы планировщик жил в них) рискуют повторным запуском одного и того же цикла — задвоенные API-вызовы к коллекторам, задвоенные попытки отправки. Защита реализована на уровне БД, а не только приложения: частичный уникальный индекс `UNIQUE(account_key) WHERE status='RUNNING'` физически не позволяет вставить вторую строку `RUNNING` для того же аккаунта — попытка второго инстанса упадёт с ошибкой уникальности, а не молча создаст дубликат.

**Project example:** `app/db/models.py` (`uq_automation_runs_one_running_per_account`), `app/scheduler.py` module docstring.

### 26. Was bedeutet Idempotenz — und wo wird sie im Projekt gebraucht?

**DE:** Idempotent heißt: Wenn man denselben Schritt mehrmals ausführt, passiert trotzdem nur einmal die echte Wirkung. Das ist wichtig, wenn ein Prozess mitten in der Arbeit abstürzt und neu gestartet wird — er darf nichts doppelt tun, zum Beispiel keine doppelte E-Mail verschicken.

**RU:** Повторное выполнение операции не приводит к повторному "реальному" эффекту сверх первого раза. Критично для восстановления после сбоя: если процесс упал сразу после отправки письма, но до фиксации записи об этом, повторный запуск не должен отправить письмо ещё раз. Реализовано через уникальные ограничения на "естественный ключ" отправки (`response_draft_id`, `follow_up_proposal_id`) — повторная попытка натыкается на уже существующую запись вместо создания новой.

**Project example:** `ResponseDraftSendRecord`/`FollowUpApprovalRecord` unique constraints (`app/db/models.py`).

### 27. Warum gibt es einen "Lease" (Miet-/Pacht-Mechanismus) für Automatisierungsläufe?

**DE:** Ein Lease ist wie eine zeitlich begrenzte Erlaubnis: "Ich arbeite gerade an diesem Lauf, verlängere das regelmäßig." Wenn der Prozess abstürzt und die Erlaubnis nicht mehr verlängert, merkt das System das nach einer bestimmten Zeit und kann den Lauf als fehlgeschlagen markieren, statt für immer "hängen" zu bleiben.

**RU:** `lease_holder`/`lease_expires_at` на `AutomationRunRecord`, продлеваемый heartbeat-потоком (`_RunLeaseHeartbeat`) — механизм обнаружения "мёртвого" владельца без явного сигнала о смерти: если срок аренды истёк, значит процесс-владелец больше не подтверждает активность (упал, завис, убит), и `reconcile_stale_run_to_failed` может безопасно перевести зависшую запись `RUNNING` в `FAILED`, разблокировав частичный уникальный индекс для следующего запуска.

**Project example:** `app/services/automation.py::_RunLeaseHeartbeat`, `app/db/automation_repository.py::reconcile_stale_run_to_failed`.

---

## Testing

### 28. Warum mocken Integrationstests die Datenbank nicht?

**DE:** Ein gemockter Test kann "grün" sein, obwohl die echte Datenbank sich anders verhält — besonders bei gleichzeitigem Zugriff. Deshalb laufen die wichtigsten Concurrency-Tests gegen eine echte PostgreSQL-Datenbank.

**RU:** Мок БД тестирует только предположения разработчика о её поведении, а не реальную семантику — именно расхождение между мок-поведением и реальным поведением СУБД исторически приводило к незамеченным багам (в частности, к различиям в порядке committed-visibility между SQLite и PostgreSQL). Поэтому `tests/integration/` целенаправленно запускается против настоящего `postgres:16`.

**Project example:** `tests/integration/test_scheduler_postgres_concurrency.py`.

### 29. Wie werden ~1900 Tests handhabbar gehalten?

**DE:** Die meisten Tests laufen schnell gegen SQLite und benutzen echte, aber isolierte Testdatenbanken. Nur die paar Tests, die wirklich PostgreSQL-spezifisches Verhalten prüfen, laufen extra — lokal übersprungen, aber immer in CI ausgeführt.

**RU:** Основная масса тестов — быстрые, против SQLite, с изолированной тестовой БД на тест; лишь узкий набор PostgreSQL-специфичных concurrency-тестов реально нуждается в постоянно работающем сервере — они самостоятельно скипаются локально при отсутствии `TEST_POSTGRES_URL`, но безусловно запускаются в CI, где сервис-контейнер `postgres:16` всегда доступен.

**Project example:** `tests/integration/test_scheduler_postgres_concurrency.py` (self-skips without `TEST_POSTGRES_URL`).

### 30. Warum verlangt das Projekt einen Test für jede Verhaltensänderung?

**DE:** Ohne Test kann man später nicht sicher sein, ob ein neues Feature noch funktioniert oder ob es kaputtgegangen ist. Ein Test macht das Verhalten überprüfbar, nicht nur behauptet.

**RU:** Без теста утверждение о поведении — просто утверждение, а не проверяемый факт. Явное правило проекта ("Any feature change requires tests") превращает тесты в контракт, фиксирующий инвариант, а не документацию, которая может устареть без предупреждения.

**Project example:** `CLAUDE.md` "Any feature change requires tests and must satisfy Ruff/format checks before Codex review."

---

## Security

### 31. Warum sollte man `hmac.compare_digest` statt `!=` für den API-Key-Vergleich benutzen?

**DE:** Ein normaler String-Vergleich (`!=`) stoppt sofort beim ersten falschen Zeichen — dadurch dauert der Vergleich je nachdem, wie viele Zeichen schon richtig sind, unterschiedlich lange. Ein Angreifer könnte diese winzigen Zeitunterschiede theoretisch nutzen, um den Schlüssel Zeichen für Zeichen zu erraten. `compare_digest` vergleicht immer gleich lang, egal was reinkommt.

**RU:** Обычное `!=` — сравнение с ранним выходом при первом несовпадающем байте, из-за чего время выполнения теоретически коррелирует с числом верно угаданных初symbols с начала строки — потенциальный timing side-channel. `hmac.compare_digest` выполняется за время, не зависящее (в разумных пределах) от того, где произошло расхождение — устраняет этот канал. В текущем коде используется `!=` (`app/security/auth.py`) — найдено как AUD-006, низкий практический риск (один статический ключ, джиттер сети), но отложенное исправление зафиксировано.

**Project example:** `app/security/auth.py::require_api_key`, `docs/PRODUCTION_READINESS_AUDIT.md` AUD-006.

### 32. Warum ist ein In-Memory-Rate-Limiter bei mehreren Worker-Prozessen ein Problem?

**DE:** Wenn die App mit mehreren parallelen Worker-Prozessen läuft, hat jeder Prozess seinen eigenen, getrennten Zähler im Arbeitsspeicher. Ein Nutzer, der zufällig auf mehrere Worker verteilt wird, kann dadurch effektiv viel mehr Anfragen machen als eigentlich erlaubt.

**RU:** Каждый воркер uvicorn — отдельный ОС-процесс с собственной памятью; `_RateLimiter.buckets` (`dict` + `threading.Lock`) существует независимо в каждом процессе, а не является общим состоянием. При N воркерах эффективный лимит становится `configured_limit × N`, так как один и тот же клиент, случайно распределённый по разным воркерам, получает отдельный бюджет в каждом. Решение — вынести хранилище во внешнее общее хранилище (Redis) — не реализовано, задокументировано как ограничение (AUD-004) для текущей однопроцессной схемы деплоя.

**Project example:** `app/security/rate_limit.py`, `docs/DEPLOYMENT.md` "Why single-server, not distributed, first".

### 33. Wie verhindert das Projekt, dass Inhalte aus einer E-Mail in generierten Text gelangen?

**DE:** Die Anwendung vertraut dem Inhalt eingehender E-Mails grundsätzlich nicht. Nur bestimmte, als "vertrauenswürdig" markierte Datenquellen (zum Beispiel eine strukturierte API, nicht eine frei geschriebene E-Mail) dürfen tatsächlich in einem generierten Text erscheinen.

**RU:** Явная модель доверия по источнику: `TRUSTED_JOB_SOURCES = frozenset({"bundesagentur"})` — факты о вакансии из structured API считаются доверенными, а из XING-дайджеста (распарсенного из непроверенного входящего письма) — нет; при генерации недоверенные факты трактуются как "нет данных" (плейсхолдер), а не как реальные данные с риском. Аналогично для кандидата: каждое поле профиля несёт `field_trust`, и только прошедшие `is_top_level_fact_usable_for_generation` попадают в сгенерированный текст. Содержимое письма используется лишь для выбора языка шаблона (`detect_language`), никогда как сырой текст в самом драфте.

**Project example:** `app/services/response_draft.py`, `app/models/candidate_profile.py`, `docs/SECURITY_MODEL.md`.

### 34. Warum ist eine öffentliche Internet-Bereitstellung dieses Projekts aktuell nicht empfohlen?

**DE:** Es gibt drei konkrete, dokumentierte Lücken: Der Health-Check prüft die Datenbank nicht, der Rate Limiter funktioniert hinter einem Reverse Proxy nicht richtig, und der API-Key-Vergleich ist theoretisch zeitangreifbar. Alle drei sind bekannt und aufgeschrieben, aber noch nicht behoben.

**RU:** Три задокументированных, но не устранённых в этой ветке находки делают публичную экспозицию преждевременной: health-check не проверяет реальную доступность зависимостей (AUD-002), rate limiter не умеет доверять `X-Forwarded-For` за reverse proxy и коллапсирует в один общий бюджет на всех клиентов (AUD-003), и не multi-worker safe (AUD-004). Рекомендация — приватная/VPN-защищённая однопроцессная развёртка как первый шаг (`docs/DEPLOYMENT.md`).

**Project example:** `docs/PRODUCTION_READINESS_AUDIT.md` "Deployment-safety matrix".

---

## IMAP/SMTP

### 35. Warum braucht IMAP explizite Zeitgrenzen (Deadlines)?

**DE:** Ein Netzwerkpartner könnte extrem langsam antworten (zum Beispiel ein Byte nach dem anderen schicken) — normale Timeouts, die nur auf einzelne Netzwerk-Aufrufe schauen, würden das nicht erkennen. Deshalb gibt es eine Gesamt-Zeitgrenze für die ganze IMAP-Sitzung.

**RU:** Стандартный сокетный таймаут (например, `socket.settimeout`) срабатывает только на конкретный блокирующий вызов `recv()`/`connect()` — "slow-drip"-атака или просто очень медленный/зависший пир, присылающий данные по одному байту с интервалом чуть меньше таймаута, обходит его: каждый отдельный `recv()` успевает завершиться. Поэтому добавлен независимый wall-clock watchdog (`threading.Timer`), принудительно закрывающий сокет по истечении ОБЩЕГО времени сессии, независимо от того, сколько успешных отдельных операций произошло внутри неё.

**Project example:** `app/providers/email/imap_deadline.py` (module docstring, `IMAP_SESSION_DEADLINE_SECONDS`).

### 36. Wie unterscheidet das Projekt zwischen "wiederholbaren" und "dauerhaften" E-Mail-Fehlern?

**DE:** Manche Fehler sind vorübergehend (z. B. ein kurzer Netzwerkausfall) und sollten später noch einmal versucht werden. Andere Fehler liegen an der Nachricht selbst (z. B. kaputtes Format) und werden nie durch ein erneutes Versuchen gelöst. Das Projekt behandelt beide Fälle unterschiedlich, statt alles gleich zu wiederholen.

**RU:** Ошибки классифицируются на retryable (сетевые сбои, временная недоступность — сообщение остаётся кандидатом для повторной обработки на следующем цикле бессрочно) и permanent (структурно некорректное/слишком большое сообщение — помечается пропущенным окончательно, не блокируя прогресс курсора вечными повторными попытками одного и того же "плохого" сообщения).

**Project example:** `app/providers/email/imap.py` (comments at fetch/parse error sites), `gmail_permanent_skips` table.

### 37. Was verhindert, dass eine Antwort-E-Mail zweimal verschickt wird?

**DE:** Bevor eine E-Mail wirklich verschickt wird, muss zuerst ein eindeutiger "Sende-Versuch"-Datenbankeintrag erfolgreich angelegt werden. Ein zweiter Versuch für dieselbe Nachricht stößt auf einen bereits existierenden Eintrag und wird deshalb nicht noch einmal ausgeführt.

**RU:** `ResponseDraftSendRecord` имеет уникальное ограничение по `response_draft_id` — попытка отправки сначала атомарно claim'ит (создаёт/CAS-обновляет) запись о попытке отправки; при неоднозначном исходе провайдера (не ясно, ушло письмо или нет) запись переводится в терминальное состояние `UNCERTAIN`, которое НИКОГДА не повторяется автоматически — предпочтение отдаётся ручному разбору перед риском дублирующей отправки.

**Project example:** `app/services/response_draft_send.py::send_response_draft`.

---

## Docker

### 38. Was löst Docker in diesem Projekt konkret?

**DE:** Vorher gab es keine wiederholbare Art, die Anwendung in einer produktionsähnlichen Umgebung zu starten — jede Bereitstellung wäre improvisiert gewesen. Docker macht das Setup (Python-Version, Abhängigkeiten, Startbefehl) zu etwas, das man genau nachvollziehen und wiederholen kann.

**RU:** До этой ветки в репозитории не было ни одного Docker-артефакта — любое развёртывание было бы импровизированным и невоспроизводимым (AUD-001). `Dockerfile` фиксирует точную версию Python (совпадающую с CI), способ установки зависимостей и команду запуска; `compose.yaml` фиксирует топологию (`db`+`web`+опциональный `scheduler`), health-check-gated порядок старта и явный, а не автоматический, шаг миграции — то есть превращает "как это вообще запустить" из знания одного человека в проверяемый, задокументированный артефакт.

**Project example:** `Dockerfile`, `compose.yaml`, `docs/DOCKER.md`.

### 39. Warum läuft der Scheduler als eigener Container statt im selben Prozess wie die FastAPI-App?

**DE:** Wenn der Scheduler im selben Prozess wie die Web-App liefe, würde jeder zusätzliche Worker-Prozess der Web-App auch einen eigenen Scheduler starten — das würde alles mehrfach ausführen. Ein komplett separater Prozess/Container garantiert genau einen Scheduler, egal wie viele Web-Worker laufen.

**RU:** Если бы планировщик стартовал внутри lifespan FastAPI, то при запуске нескольких воркеров uvicorn (`--workers N`) получилось бы N независимых экземпляров планировщика, каждый со своим таймером — цикл автоматизации запускался бы N раз вместо одного. Поэтому `app/scheduler.py` — намеренно отдельная точка входа (`python -m app.scheduler`), не запускаемая из `app/main.py`; в Docker это выражается отдельным compose-сервисом `scheduler` с собственным политикой рестарта, который никогда не должен масштабироваться выше одной реплики.

**Project example:** `app/scheduler.py` module docstring, `compose.yaml` service `scheduler`, `docs/DEPLOYMENT.md` "Scheduler strategy: Option A vs Option B".

### 40. Warum wird eine echte lokale Validierung (Build + Smoke-Test) statt nur ein YAML-Review gemacht?

**DE:** Eine Compose-Datei kann syntaktisch korrekt aussehen, aber trotzdem beim echten Start scheitern. Ein echter Build- und Start-Test hat tatsächlich einen konkreten Fehler gefunden (der Scheduler-Container würde sich ohne Anpassung ständig neu starten), den ein reines Lesen der Datei nicht gezeigt hätte.

**RU:** Статический просмотр YAML не выявляет поведение во время выполнения — реальная сборка образа, поднятие `postgres:16`, прогон миграций и старт `web`-контейнера в этой сессии обнаружили конкретную находку: при `restart: unless-stopped` контейнер `scheduler` с обоими автоматизационными флагами выключенными завершается кодом `0` и бесконечно перезапускается компоузом — это чисто runtime-поведение, невидимое при простом чтении `compose.yaml`, исправленное сменой политики на `restart: on-failure`.

**Project example:** `docs/DOCKER.md` "Local validation performed" table, `compose.yaml` scheduler `restart: on-failure` comment.

---

## Architecture

### 41. Warum sind `app/agents` komplett von der Datenbank getrennt?

**DE:** `app/agents` enthält nur reine Logik: Daten rein, fertiger Text raus, ohne Datenbankzugriff oder Netzwerk. Das macht diese Module sehr einfach zu testen — man braucht keine Datenbank, um sie zu prüfen.

**RU:** Модули `app/agents` — чистые функции (только импорты из `app/models`/`app/utils`, без обращений к БД/сети/I-O) — детерминированная генерация CV/писем/драфтов зависит исключительно от входных аргументов, что делает юнит-тестирование тривиальным (без фикстур БД, без моков сети) и гарантирует отсутствие скрытых побочных эффектов в "генеративном" слое.

**Project example:** `app/agents/response_draft_generator.py`, `app/agents/cv_adapter.py`.

### 42. Was ist der zirkuläre Import zwischen `app/providers` und `app/collectors`, und warum ist er (noch) kein echter Fehler?

**DE:** Zwei Pakete importieren gegenseitig voneinander. Das funktioniert aktuell, weil Python ein Modul beim ersten Import "cached" — aber es ist ein fragiles Muster, das bei zukünftigen Änderungen echte Probleme verursachen könnte.

**RU:** `app/providers/email/{imap,smtp}.py` импортируют `is_configured` из `app.collectors.base`, а `app/collectors/xing_email.py` импортирует из `app.providers.email.*` — цикл на уровне графа импортов. Сейчас не проявляется как `ImportError`, потому что оба модуля всегда импортируются в порядке, при котором кэш модулей Python (`sys.modules`) успевает разрешить обе стороны до того, как понадобится ещё не готовый атрибут — но это хрупко: изменение порядка импорта или разделение модуля может обнажить реальный цикл. Зафиксировано как AUD-012, не исправлено (архитектурное изменение, вне рамок этой ветки).

**Project example:** `docs/PRODUCTION_READINESS_AUDIT.md` AUD-012, `docs/ARCHITECTURE.md`.

---

## AI-assisted development

### 43. Wie wurde dieses Projekt mit KI-Unterstützung entwickelt?

**DE:** Claude Code hat den Code geschrieben und Architekturentscheidungen umgesetzt. Codex hat unabhängig den Code überprüft — auf Bugs, Sicherheitsprobleme und fehlende Tests. Der Mensch (der Entwickler) trifft die eigentlichen Entscheidungen und genehmigt riskante Schritte.

**RU:** Двухагентный процесс: Claude Code — основной реализующий агент (архитектура, код, тесты), Codex — независимый ревьюер того же диффа (баги, security, полнота тестов, повторная проверка после исправлений). Финальные архитектурные решения, авторизация рискованных операций (мердж, force-push, отправка реальных писем) и общее направление проекта остаются за человеком-разработчиком — задокументировано явно в `CLAUDE.md`/`AGENTS.md` как рабочий процесс, а не просто использовано молча.

**Project example:** `CLAUDE.md` "Workflow" section, commit messages referencing "Codex re-review" (e.g. `da724f6`).

### 44. Warum dokumentiert das Projekt explizit, was NICHT gefixt wurde?

**DE:** Ehrlichkeit über bekannte Grenzen ist wertvoller als der Eindruck von Perfektion. Wenn eine Einschränkung bekannt, verstanden und aufgeschrieben ist, ist das etwas anderes, als wenn niemand weiß, dass sie existiert.

**RU:** Явная фиксация "известного, но не исправленного" (P0-P3 находки с обоснованием, почему не исправлено сейчас — риск для бизнес-логики, требуется отдельный review, вне текущего скоупа) превращает технический долг из скрытого риска в управляемый и приоритизируемый список — сознательное решение не чинить что-то сейчас, задокументированное с причиной, гораздо ценнее для ревьюера/нанимателя, чем видимость отсутствия проблем.

**Project example:** `docs/PRODUCTION_READINESS_AUDIT.md`, `docs/TECHNICAL_DEBT.md`, `CLAUDE.md` "Known security-relevant risk" section.
