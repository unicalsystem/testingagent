# TestAgent — Intelligent Web Application Testing Agent

Unical Systems' testing agent: write or AI-generate end-to-end browser tests, run them on the
server in headless Chromium, and keep auditable evidence (screenshots, traces, video, logs,
HTML/JSON reports) for every run.

**Production:** https://testingagent.unical.in · **Deployment guide:** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

## Stack

| Layer | Technology |
|---|---|
| Frontend | React 19 + TypeScript (Vite) — `frontend/` |
| API | Python FastAPI — `backend/testpilot/api/` |
| Database | PostgreSQL 16 + Alembic migrations — `backend/migrations/` |
| Browser automation | Playwright + Chromium in a dedicated worker — `backend/testpilot/worker.py` |
| AI | Anthropic Claude API (optional; app works without it) — `backend/testpilot/ai.py` |
| Report copy | MySQL, configured in `config.local` — `backend/testpilot/report_store.py` |
| Deployment | Docker Compose + host nginx + Let's Encrypt — `docker-compose.prod.yml`, `deploy/` |

## Features

- Projects with base URL, additional hosts and encrypted, write-only secrets (`{{secret:NAME}}`)
- Declarative test steps: goto, click, fill, press, select, check/uncheck, hover, wait_for, wait, screenshot, expect_visible/hidden/text/url/title. No script execution.
- **Generate with AI.** Claude drafts test cases from a requirement; you review them before saving.
- **Analyse with Claude.** Claude explains a failed run from its step log and failure screenshot.
- Server-side runs with isolated contexts, timeouts, bounded retries, cancellation and a concurrency limit
- Evidence per step, plus a trace, a video and a log per run; downloadable HTML and JSON reports
- SSRF-safe egress: admin allowlist, DNS/IP checks on every request and redirect, metadata endpoints always blocked
- Roles (viewer, tester, admin), audit log, account lockout
- **Import test cases from CSV or Excel (.xlsx).** Upload any layout: columns are matched to TestAgent fields automatically
  (ID, title, module, sub-module, priority, pre-conditions, procedure, expected result, result, …) and every match can be
  changed; unknown columns are kept as custom fields. An Excel/CSV template with instructions is also downloadable.
  Nothing is written until the user reviews a summary and ticks the confirmation. Re-importing the same IDs updates
  cases instead of duplicating them, and an import can be undone.
- **Test runs.** Organise cases into runs (one per module / sub-module / priority / any column, one run for everything,
  or none) during import or later; testers record Pass / Fail / Blocked / Retest / Skipped with actual results, in bulk
  or one by one; cases with automated steps can be executed by the worker from inside the run and their result is
  written back. Results export to Excel/CSV in the same layout as the template.
- **AI automation of manual cases.** Select imported/manual cases (or a whole test run) and the agent explores the
  application (signing in with the project's setup steps), Claude drafts browser steps from each procedure and expected
  result using the selectors it actually found, every draft is executed once and repaired from the failure screenshot
  if needed, and verified drafts are saved to the case (automatically or after review). Cases that cannot be checked in
  a browser (e-mail, CAPTCHA, files, inspection) are marked "stays manual". Verified passes are recorded in the test run.
- **Mobile friendly.** Collapsible navigation, stacked table cards, full-width bottom-sheet dialogs and large tap targets
  on phones.

## Local development

```bash
# PostgreSQL running locally, then:
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && playwright install chromium
export APP_ENV=development DATABASE_URL=postgresql+psycopg://testpilot:devpass@127.0.0.1:5432/testpilot \
       PUBLIC_URL=http://localhost:5173 ALLOWED_ORIGINS=http://localhost:5173 COOKIE_SECURE=false \
       SESSION_SECRET=dev-dev-dev-dev-dev-dev-dev-dev-dev ALLOWED_PRIVATE_TARGETS=127.0.0.1 \
       CREDENTIAL_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") \
       EVIDENCE_DIR=$PWD/../evidence
alembic upgrade head
echo 'Admin-pass-2026' | python -m testpilot.cli create-admin --username admin
(cd ../sample-app/site && python -m http.server 8090 &) && python -m testpilot.cli seed-sample --url http://127.0.0.1:8090
uvicorn testpilot.api.app:app --port 8000 &   python -m testpilot.worker &
cd ../frontend && npm ci && npm run dev        # http://localhost:5173
```

## Tests

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://testpilot:devpass@127.0.0.1:5432/testpilot_test \
TEST_MYSQL_URL=mysql://user:pass@127.0.0.1:3306/tp_reports_test \
pytest -q                                   # API, security, real-Chromium worker and MySQL tests
cd ../frontend && npm run build             # type-check + production build
```
