# Stage 63 — API key rotation plan

Status: **plan only. The production key has NOT been changed.** Rotation needs the operator's
explicit go-ahead. No secret value appears in this document, in the chat, in CI or on GitHub.

## 1. Finding (measured without printing the value)

| Property | Result |
|---|---|
| `API_KEY` set in `E:\Projects\job-triage\.env` | yes |
| Length | 23 characters (rule: at least 32, `app/core/key_strength.py`, `MIN_STRONG_KEY_CHARS`) |
| Distinct characters | 15 (rule: at least 12 — passes) |
| Verdict of `secret_strength()` | **weak** (too short only; not a placeholder) |

Consequence of "weak" in the code: the key is still accepted and works; the only difference is
that the failed-authentication budget can lock out the correct key too when other clients fail
(see `key_strength.py` docstring). It is not a code defect; it is an operator-owned value.
Exposure is limited: the server binds `127.0.0.1` only and the UI cookie is `HttpOnly; SameSite=Strict`.

## 2. Where the key is used

| Consumer | How | Affected by rotation |
|---|---|---|
| `.env` `API_KEY` | read by `Settings.api_key` at process start | the single source of truth |
| REST API `/api/v1/*` | header `X-API-Key` (`app/security/auth.py`) | every client that sends it |
| Operator UI `/ui/*` login | the same key; session cookie is signed with a key derived from it unless `UI_SESSION_SECRET` is set (`app/ui/auth.py`) | all browser sessions are signed out (also on every restart) |
| `scripts/run_operator_ui.py` | reads `.env`; refuses to start without a key | restart after the change |
| Docker/compose | `.env` via compose (`docker/README.md`) | only if you run the container with this `.env` |
| GitHub CI | replaces `API_KEY` with the literal placeholder `ci-validation-only` in a throw-away `.env` (`ci.yml`) | **not affected**; no repository secret holds the key |
| Postman | the repository has no environment file with the key (`postman/environments` is empty, `postman/globals` has no key) | the Postman **cloud workspace** may hold a variable you set by hand: update it there. Store the new key as a *secret-type* variable, never in an exported collection |
| `BUNDESAGENTUR_API_KEY`, Gmail/XING app passwords, Telegram token | separate settings | **not affected** |
| External integrations | none found in the code or `.env` names that call JobTriage with this key | none known |

`UI_SESSION_SECRET` is not set in `.env` (the name is not in the file), so the cookie key derives
from `API_KEY`; rotating the key therefore also rotates the cookie signing key.

## 3. Rotation steps (operator, about 5 minutes)

1. **Back up first** (cheap, restorable): 
   `.\.venv\Scripts\python.exe -m scripts.backup_sqlite --db runtime\job_triage_runtime.db --dest E:\JobTriage-Backups --label before-key-rotation`
2. **Generate** a key with the OS random source and copy it straight into the password manager.
   Do not paste it into chat, tickets or shell history that is synced:
   `.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"` (43 characters, about 256 bits).
   Clear the terminal afterwards.
3. **Keep the old value** in the password manager as "old key (delete after verification)".
4. **Stop the server** (Ctrl+C in its window). Confirm nothing listens on 8000:
   `Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue`.
5. **Edit only the `API_KEY=` line** of `E:\Projects\job-triage\.env` (Notepad; no quotes, no trailing space).
   Do not touch any other line (flags stay `false`).
6. **Start the server** with the normal command from the daily runbook §1.
7. **Verify** (these do not print the key):
   - first log line `startup_safety_state ... api_key_strength=strong`, all flags `false`;
   - `GET http://127.0.0.1:8000/api/v1/ready` → `{"status":"ready"}`;
   - UI login with the **new** key works; the **old** key gives 401;
   - `curl.exe -s -o NUL -w "%{http_code}" -H "X-API-Key: <new>" http://127.0.0.1:8000/api/v1/jobs` → 200 and without the header → 401
     (type the key in the shell only; do not save the command in a script);
   - update the Postman variable (secret type) and re-run one request.
8. After a successful check, delete the "old key" entry from the password manager.

## 4. Rollback

If the server refuses to start or login fails: stop the server, restore the previous `API_KEY=`
line from the password manager into `.env`, start again, verify as in step 7 (the old key must
log in and `api_key_strength` returns to `weak`). The database is not touched by rotation, so
no database restore is needed; the `before-key-rotation` backup is only a precaution.

## 5. Safety notes

* Never write the key into `docs/`, CI variables, Postman exports, screenshots or commit messages.
* `.env` is untracked (`git check-ignore .env`); keep it that way.
* If the old key may have leaked (shared screen, chat, export), treat it as compromised: rotate
  immediately and also check Postman exports and browser-saved passwords.
* Rotation does not need production database writes, migrations or any flag change.
