# Operator commands (stable names)

The operator scripts in `scripts/` carry the development stage in their file names
(`stage22_refresh_ba.py`). `app/cli.py` gives each one a stable command name; it only
dispatches — the script's own options, defaults (dry run), database-target refusals and exit
codes apply unchanged, and the **old script paths keep working**, so existing runbooks and
automation need no edit.

```powershell
python -m app.cli --list
python -m app.cli refresh-ba --help
python -m app.cli candidate-config show --database runtime/job_triage_runtime.db
jobtriage candidate-config show --database ...      # after `pip install -e .`
```

| Command | Script | Purpose |
| --- | --- | --- |
| `send-readiness` | `stage14a_send_readiness.py` | readiness report before enabling sends |
| `canonical-runtime` | `stage14d_canonical_runtime.py` | preflight / serve the canonical runtime |
| `applications` | `stage15_application_tracker.py` | application lifecycle tracker |
| `import-application` | `stage15_import_ald_application.py` | import one existing application |
| `telegram-operator` | `stage16_telegram_operator.py` | Telegram operator control |
| `candidate-config` | `stage17_candidate_config.py` | show / apply the candidate search configuration |
| `job-fit` | `stage17_job_fit.py` | commute and fit assessment of vacancies |
| `assess-vacancies` | `stage21_assess_vacancies.py` | fill the vacancy fit cache |
| `match-vacancies` | `stage22_match_vacancies.py` | match vacancies against the candidate |
| `refresh-ba` | `stage22_refresh_ba.py` | refresh stored Bundesagentur vacancies |
| `ba-liveness` | `ba_liveness.py` | check whether stored BA vacancies are still online |
| `backfill-source-reference` | `backfill_source_reference.py` | fill `jobs.source_reference` |
| `vacancy-identity-report` | `vacancy_identity_report.py` | report vacancy identity collisions |
| `operator-ui` | `run_operator_ui.py` | start the read-only operator UI |
| `presubmit-check` | `presubmit_check.py` | mandatory pre-submit check for a Bewerbung |
| `profile-transfer` | `candidate_profile_transfer_stage13b.py` | export / import the candidate profile |
| `rescore-offline` | `offline_rescore_stage12.py` | offline re-score preview |
| `build-common-stems` | `build_job_ad_common_stems.py` | rebuild the job-ad common-stem list |
| `check-quality` | `check_tracked_quality.py` | ruff gate over tracked sources |

Notes: the scripts belong to a repository checkout; they are not part of the installed package or
the Docker image (the dispatcher says so instead of failing obscurely). `tests/test_cli_dispatcher.py`
fails if a script is added without a command name.
