# Ежедневная работа с JobTriage (v1.0.0)

Коротко: система ищет и оценивает вакансии и готовит черновики. **Отправляете вы сами**, а потом
записываете отправку. Все команды — PowerShell из папки проекта; ничего не придумано, каждая есть в
репозитории (подробности: `docs/DAILY_OPERATIONS.md`).

```powershell
cd E:\Projects\job-triage
$py = ".\.venv\Scripts\python.exe"
$db = "runtime/job_triage_runtime.db"
$bk = "$env:USERPROFILE\JobTriage-Backups"
```

Каталоги: база `runtime\job_triage_runtime.db`; резервные копии `%USERPROFILE%\JobTriage-Backups`;
пакеты документов `%LOCALAPPDATA%\job-triage-packages\`; копии для загрузки в порталы
`.playwright-mcp\upload` (не коммитятся).

## 0. Один раз перед первым днём (после обновления версии)

Кэш оценок привязан к коду: после обновления он «устарел», и фильтры «Fit tier / Applied / Mode» в UI
пусты, пока вы его не пересчитаете (это пишет только таблицу `job_fit_assessments`):

```powershell
& $py scripts\backup_sqlite.py --db $db --dest $bk --keep 14        # резервная копия
& $py scripts\stage21_assess_vacancies.py --database $db            # сухой прогон: сколько stale
& $py scripts\stage21_assess_vacancies.py --database $db --apply --snapshot $bk
```

## 1. Запустить JobTriage

```powershell
& $py scripts\run_operator_ui.py --database $db --snapshot $bk
```

Откройте <http://127.0.0.1:8000/ui/>, войдите ключом `API_KEY` (из `.env`). Остановка — Ctrl+C.
Лаунчер сам выключает Telegram, Gmail, рассылки и планировщик. Проверка: `GET /api/v1/health` и `/api/v1/ready`.

## 2. Обновить вакансии вручную

Остановите UI (Ctrl+C): обновление пишет в базу и не терпит второго писателя.

```powershell
& $py scripts\stage22_refresh_ba.py --database $db                                  # сухой прогон: найдено / новых / строк "COMPLETE"
& $py scripts\stage22_refresh_ba.py --database $db --apply --create-new --snapshot $bk
& $py scripts\stage22_match_vacancies.py --database $db --apply --snapshot $bk
& $py scripts\stage21_assess_vacancies.py --database $db --apply --snapshot $bk
```

В отчёте у каждого поиска должно стоять `COMPLETE`. `PARTIAL_*` значит, что часть результатов не получена
(причина в названии): повторите позже и не считайте поиск полным. Конфликты ссылок BA (`REFERENCE_EXISTS_...`)
не создаются и не склеиваются — проверьте вручную.

## 3. Открыть новые вакансии

Снова запустите UI (шаг 1). `Vacancies` — список (новые сверху), фильтры: поиск, город, источник,
fit tier, applied, work mode. `Review` — очередь на ручную проверку. Не доверяйте колонке `Decision` —
ориентируйтесь на fit tier и «Why».

## 4. Проверить подходящие объявления

Откройте карточку вакансии: критерии, причины, доказательства, дубликаты, ссылка на объявление работодателя.
Читайте само объявление. Fit tier — фильтр, а не решение.

## 5. Выбрать вакансии и проверить дубликат (обязательно)

```powershell
& $py scripts\stage15_application_tracker.py --database $db check-job <job_id>
```

`ALREADY_APPLIED` — не подавайтесь. `POSSIBLE_DUPLICATE` — проверьте глазами.

## 6. Подготовить Anschreiben и Lebenslauf

Нужен запущенный UI-сервер (шаг 1) и ключ:

```powershell
$h = @{ "X-API-Key" = "<API_KEY>" }; $b = "http://127.0.0.1:8000/api/v1"; $job = <job_id>
$m  = Invoke-RestMethod -Method Post -Headers $h "$b/jobs/$job/match"
$cv = Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" -Body (@{match_id=$m.id}|ConvertTo-Json) "$b/jobs/$job/cv-draft"
$bw = Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" -Body (@{cv_draft_id=$cv.id}|ConvertTo-Json) "$b/jobs/$job/bewerbung-draft"
$pk = Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" -Body (@{cv_draft_id=$cv.id;bewerbung_draft_id=$bw.id}|ConvertTo-Json) "$b/jobs/$job/review-package"
```

Черновики используют только подтверждённые факты профиля (навыки, проекты, работодатели, образование,
немецкий B1). Это шаблоны, а не LLM: письмо читайте и правьте (`PATCH /review-packages/{id}`). Предупреждения
в `warnings` читайте обязательно.

## 7. Проверить документы

Прочитайте CV и письмо. Когда всё верно — одобрите пакет (это локальное решение, ничего не отправляется):

```powershell
Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" -Body (@{expected_review_version=1;acknowledge_manual_overrides=$false;decision_note="проверено"}|ConvertTo-Json) "$b/review-packages/$($pk.id)/approve"
```

Если вы правили текст вручную, версия и `acknowledge_manual_overrides` другие — смотрите ответ API. PDF
готовите сами; пакеты лежат вне репозитория (см. каталоги выше).

## 8. Отправить Bewerbung вручную — только после проверки

Загрузите файлы в портал / письмо. **До последнего клика** запустите обязательную проверку:

```powershell
& $py scripts\presubmit_check.py --database $db `
  --package "$env:LOCALAPPDATA\job-triage-packages\<день>_LiveHunt\ApplicationPrep\Final\<пакет>" `
  --company "<работодатель>" --role "<точное название>" `
  --attach cv=.playwright-mcp\upload\<пакет>\<файл>.pdf
```

Код 0 `READY` — можно отправлять по вашему решению; 3 `NEEDS_OPERATOR` — только после того, как вы
увидели и приняли каждое предупреждение; 4 `BLOCKED` или 2 — не отправлять. Отправку выполняете вы.

## 9. Зарегистрировать отправку

```powershell
& $py scripts\stage15_application_tracker.py --database $db record-submission `
  --company "Firma GmbH" --role "Python Developer (m/w/d)" --source bundesagentur --reference <BA-ссылка> `
  --job-id <job_id> --channel EMPLOYER_PORTAL --submitted-at <ISO-время с зоной> `
  --actor operator --evidence "Портал: подтверждение; presubmit READY"             # сухой прогон
# то же с --apply — запись. Повтор безопасен (ALREADY_RECORDED).
```

## 10. Проверить ответы работодателей

```powershell
& $py scripts\stage15_application_tracker.py --database $db process-gmail --since <дата>          # читает почту, ничего не пишет
& $py scripts\stage15_application_tracker.py --database $db process-gmail --since <дата> --apply
& $py scripts\stage15_application_tracker.py --database $db list
& $py scripts\stage15_application_tracker.py --database $db transition <id> REJECTED --actor operator --reason "Absage, Postfach geprüft" --apply
```

Отказы и офферы почта только *предлагает* — подтверждаете вы. Хронология: `... history <id>`.
Через 14 дней после отправки в `list` виден `next_followup_at`; напоминаний и рассылок система не шлёт.

## Если что-то пошло не так

* Резервные копии: `scripts\backup_sqlite.py`; восстановление: `scripts\restore_sqlite.py` (сначала в новый
  файл и проверить; подмена боевого файла — по `docs/BACKUP_AND_ROLLBACK.md`).
* Любая ошибка при записи в базу — не повторяйте вслепую: сообщите, приложите вывод.
* Не включайте `OUTBOUND_SENDING_ENABLED`, планировщик и автоподготовку без отдельного решения.
