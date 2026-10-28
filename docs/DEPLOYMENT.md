# TestAgent — Production Deployment (Hostinger VPS)

Target URL: **https://testingagent.unical.in**

This guide takes a Hostinger VPS from empty to a verified deployment. Every command is safe to
re-run. Nothing here touches the `unical.in` website, its email, or any other DNS record.

---

## 1. Architecture

```
                      Internet
                         │ 443 (HTTPS), 80 → 301 redirect
               ┌─────────▼──────────┐
               │ host nginx + TLS   │  Let's Encrypt, HSTS, login rate limit
               └─────────┬──────────┘
                         │ 127.0.0.1:8081 (localhost only)
  ┌──────────────────────┼───────────────────── docker compose "testpilot" ─────────┐
  │              ┌───────▼────────┐                                                 │
  │   edge net   │  web (nginx)   │  React build + proxies /api                     │
  │              └───────┬────────┘                                                 │
  │              ┌───────▼────────┐        Claude API (HTTPS, optional)             │
  │              │  api (FastAPI) ├──────────────────────────────►                  │
  │              └───────┬────────┘                                                 │
  │  backend net         │ (internal: no internet)                                  │
  │   ┌──────────────────▼─────┐      ┌───────────────────────────┐                 │
  │   │  db (PostgreSQL 16)    │◄─────┤ worker (Playwright/Chromium)│                │
  │   │  volume testpilot_pgdata│      │ volume testpilot_evidence  │               │
  │   └────────────────────────┘      └─────────────┬──────────────┘                │
  │                                     egress net  │ via per-run filtering proxy   │
  └─────────────────────────────────────────────────┼───────────────────────────────┘
                                                    ▼
                        authorised test targets only  (+ MySQL report store, optional)
```

| Component | What it is | Exposed? |
|---|---|---|
| host nginx | TLS termination, HTTP→HTTPS redirect, rate limiting | 80/443 public |
| `web` | nginx serving the React/TypeScript build, proxies `/api/` | 127.0.0.1:8081 only |
| `api` | FastAPI: auth, projects, test cases, runs, evidence, reports, Claude | internal |
| `worker` | Claims queued runs from PostgreSQL, runs Chromium headless on the server | internal |
| `db` | PostgreSQL 16, application system of record | internal network, no internet |
| `migrate` | One-shot Alembic migrations (`tools` profile) | — |
| `sample-app` | Demo target for end-to-end verification (`sample` profile) | internal |
| MySQL (optional) | A copy of every finished report, configured in `config.local` | outbound from worker |

Key design points:

- **Browser runs on the server.** Chromium runs headless inside the worker container. Each run gets a fresh, isolated browser context. Service workers, downloads and non-proxied WebRTC UDP are blocked. The worker container does not receive the Claude API key or the session secret.
- **Egress control (SSRF protection).** All Chromium traffic is forced through a per-run proxy inside the worker. That covers navigations, every redirect hop, sub-resources and WebSockets. The proxy:
  - accepts only exact hostnames on the admin-managed allowlist, and only the project's ports;
  - resolves DNS itself and rejects private, loopback, link-local and multicast addresses, plus cloud-metadata addresses (`169.254.169.254` and friends);
  - connects to the IP it vetted, so DNS rebinding can't redirect the browser.

  Private addresses are permitted only for hosts the operator lists in `ALLOWED_PRIVATE_TARGETS`. By default that is the bundled sample app. Metadata ranges stay blocked regardless.
- **No arbitrary code.** Test steps use a fixed declarative language (goto, click, fill, expect_* …). There is no JavaScript-evaluation step and no shell step. Claude's output is validated against the same schema, and anything else is discarded.
- **Evidence.** Each run keeps per-step screenshots, a Playwright trace, a video, an execution log, and HTML and JSON reports. They live on the private `testpilot_evidence` volume, keyed by project, run and step, with a SHA-256 hash for each file. Downloads go through the API with an authorisation check, and nginx never serves the files directly. Secret values are masked in logs and scrubbed from traces.
- **Graceful AI degradation.** Without a Claude key, or during a Claude outage, projects, runs and reports keep working. Only the "Generate with AI" and "Analyse with Claude" buttons are disabled, and a banner says why.
- **Concurrency.** `MAX_CONCURRENT_RUNS` is enforced globally with a PostgreSQL advisory lock. Runs have a total time limit, per-step timeouts, bounded retries (0–5) and cancellation. A watchdog marks runs whose worker died as `error`.

---

## 2. What you need

| Item | Notes |
|---|---|
| Hostinger **VPS** | KVM 2 or higher recommended (2 vCPU, 8 GB RAM). KVM 1 (4 GB) works with `MAX_CONCURRENT_RUNS=1`. OS template: **Ubuntu 24.04** (plain, or "with Docker"). |
| Root SSH access | hPanel → VPS → Overview → SSH access |
| VPS public IPv4 | hPanel → VPS → Overview. **You enter this in DNS; it is never guessed.** |
| DNS control for `unical.in` | hPanel → Domains → unical.in → DNS / Nameservers (or Cloudflare, if the domain uses it) |
| Anthropic API key | Optional. console.anthropic.com → API keys |
| MySQL database | Optional, for the report copy (see §7) |

> Hostinger **shared/web hosting** (hPanel `public_html`, where `cric-score.unical.in` and `connect.unical.in` run) cannot run this stack. It has no Docker, no long-running worker and no headless Chromium. Use the VPS. The MySQL report store *can* point at a shared-hosting MySQL database (§7).

---

## 3. DNS record (testingagent.unical.in)

Add **one** record where `unical.in` DNS is managed. Change nothing else. Leave the root domain, `www`, the MX/SPF/DKIM email records and the other subdomains alone.

| Type | Name / Host | Points to | TTL |
|---|---|---|---|
| **A** | `testingagent` | *your VPS public IPv4* (from hPanel) | 3600 (Hostinger default) or 300 while testing |

- Add an **AAAA** record only if the VPS really has a working IPv6 address (hPanel shows it) and you want IPv6. If you're unsure, skip it.
- If a CNAME or another A record for `testingagent` already exists, edit that one rather than adding a duplicate.
- If `unical.in` uses Cloudflare, add the record there with the proxy set to **DNS only** (grey cloud) for the first certificate issuance.

Check propagation:

```bash
dig +short A testingagent.unical.in        # must print the VPS IPv4
```

---

## 4. First-time VPS setup

```bash
ssh root@<VPS_IPV4>

# 1. Get the code
mkdir -p /opt && cd /opt
git clone https://github.com/unicalsystem/testingagent.git testpilot
cd /opt/testpilot

# 2. Install Docker, compose, nginx, certbot, firewall rules, swap (idempotent)
bash deploy/scripts/bootstrap-vps.sh
#    -> prints the VPS public IPv4: use it for the DNS A record (§3)
ufw status        # if "inactive":  ufw enable   (SSH and Nginx are already allowed)

# 3. Configuration
cp .env.example .env && chmod 600 .env
cp config.local.example config.local && chown 10001:10001 config.local && chmod 600 config.local
docker compose -f docker-compose.prod.yml build api          # builds the backend image
docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m testpilot.cli generate-keys
#    -> copy SESSION_SECRET, CREDENTIAL_ENCRYPTION_KEY and POSTGRES_PASSWORD into .env.
#       Put the same password inside DATABASE_URL. Optionally set ANTHROPIC_API_KEY.
nano .env

# 4. Deploy (builds, migrates, starts, health-checks). WITH_SAMPLE=1 adds the demo target.
WITH_SAMPLE=1 bash deploy/scripts/deploy.sh

# 5. First admin account (prompts for a password; nothing is echoed or logged)
bash deploy/scripts/create-admin.sh admin

# 6. HTTPS (after the DNS A record resolves to this VPS)
LETSENCRYPT_EMAIL=shiva@unical.in bash deploy/scripts/setup-nginx.sh

# 7. Verify everything, including a real end-to-end browser run against the sample app
bash deploy/scripts/health.sh
bash deploy/scripts/verify-e2e.sh
```

Then open https://testingagent.unical.in, sign in as `admin`, open **Sample App (Acme Tasks)**, run a test case and download the report.

### If the VPS already hosts other sites

- `setup-nginx.sh` adds a single site file, `/etc/nginx/sites-available/testingagent.unical.in.conf`, and runs `nginx -t` before every reload. It does not modify or remove other sites.
- If something else already uses host port 8081, set `WEB_PORT` in `.env` and edit `deploy/nginx/testpilot-proxy.conf` to match.
- If the VPS uses Traefik or Caddy instead of nginx, point that proxy at `127.0.0.1:8081` with the same headers (`Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`) and a 180-second read timeout. Skip `setup-nginx.sh`.

---

## 5. Environment variables (`.env`)

| Variable | Purpose | Default / example |
|---|---|---|
| `APP_ENV` | `production` turns on strict config checks and disables API docs | `production` |
| `PUBLIC_URL` | Public origin | `https://testingagent.unical.in` |
| `ALLOWED_ORIGINS` | Origins allowed on state-changing requests (comma separated) | `https://testingagent.unical.in` |
| `COOKIE_SECURE` | Session cookie `Secure` flag (must be true in production) | `true` |
| `SESSION_TTL_HOURS` / `SESSION_IDLE_MINUTES` | Absolute and idle session lifetime | `12` / `120` |
| `WEB_PORT` | Localhost port the web container publishes | `8081` |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Database bootstrap | generated password |
| `DATABASE_URL` | SQLAlchemy URL used by the api, worker and migrations | `postgresql+psycopg://testpilot:<pw>@db:5432/testpilot` |
| `SESSION_SECRET` | HMAC key for session-token hashes (≥32 chars) | generated |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key that encrypts project secrets. **Back it up**: without it, stored secrets can't be decrypted. | generated |
| `ANTHROPIC_API_KEY` | Claude API key. Empty disables AI features. | empty |
| `CLAUDE_MODEL` | Claude model identifier | `claude-sonnet-5-5` |
| `CLAUDE_TIMEOUT_SECONDS` / `CLAUDE_MAX_TOKENS` | Claude call limits | `90` / `8000` |
| `EVIDENCE_DIR` | Evidence path inside containers (volume `testpilot_evidence`) | `/data/evidence` |
| `CAPTURE_VIDEO` / `CAPTURE_TRACE` / `SCREENSHOT_MODE` | Evidence options (`each_step` or `on_failure`) | `true` / `true` / `each_step` |
| `MAX_CONCURRENT_RUNS` | Global limit on browser runs | `2` |
| `RUN_TIMEOUT_SECONDS` | Hard limit per run | `600` |
| `STEP_TIMEOUT_MS` / `NAVIGATION_TIMEOUT_MS` | Default per-step and navigation timeouts | `15000` / `30000` |
| `MAX_STEP_RETRIES` / `MAX_STEPS_PER_CASE` | Bounded retries; maximum test size | `2` / `200` |
| `WORKER_POLL_SECONDS` / `STALE_RUN_MINUTES` | Queue polling; dead-worker detection | `2` / `5` |
| `ALLOWED_PRIVATE_TARGETS` | Hosts allowed to resolve to private IPs (operator-only) | `sample-app` |
| `LOG_LEVEL` / `LOG_JSON` | Logging. Secrets, API keys and DB passwords are redacted. | `INFO` / `true` |

`docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m testpilot.cli check-config` validates the configuration without starting anything. The api and worker also refuse to start with unsafe production settings.

---

## 6. Authorising test targets

Only an **admin** can authorise targets, under **Administration → Authorised test targets**. Enter bare hostnames such as `staging.customer-app.com` (exact match; subdomains must be listed separately). Each project's base URL host, and any "additional hosts" it loads, must be on this list before its tests can run. All other requests from the browser are blocked and recorded in the run log.

Authorise only systems Unical is permitted to test.

> Project secrets: values can be used only in `fill` steps, and only into password inputs (`type=password`). Once a project holds secrets, only an admin can change its base URL or hosts. That stops a secret being typed somewhere visible or sent to a different site.


---

## 7. MySQL report store (`config.local`)



PostgreSQL remains the application database. When enabled, the worker also copies every finished run into MySQL:

- `tp_reports`: one row per run, holding the summary columns plus the full JSON report and the self-contained HTML report (screenshots embedded).
- `tp_report_steps`: one row per step, with status, attempts, duration, error and the screenshot hash.

Tables are created automatically and never dropped or altered. Pushes are idempotent upserts. If MySQL is down, runs still complete, the copy shows as *pending*, and the worker retries every `MYSQL_REPORTS_SYNC_INTERVAL_SECONDS`.

Edit `/opt/testpilot/config.local` (KEY=VALUE, owned by uid 10001 with mode 600 so only the containers' non-root user can read it, never committed; `deploy.sh` enforces this):

```ini
MYSQL_REPORTS_ENABLED=true
MYSQL_REPORTS_HOST=srv1234.hstgr.io        # Hostinger remote MySQL host (see below)
MYSQL_REPORTS_PORT=3306
MYSQL_REPORTS_DATABASE=u123456789_testpilot
MYSQL_REPORTS_USER=u123456789_testpilot
MYSQL_REPORTS_PASSWORD=********
MYSQL_REPORTS_SSL=required                 # disabled | required | verify
```

Where the database can live:

- **Hostinger shared-hosting MySQL.**
  1. In hPanel → Databases → Management, create the database and user.
  2. In hPanel → Databases → **Remote MySQL**, add the **VPS IPv4**.
  3. Use the remote hostname hPanel shows for the database (not `localhost`).
- **MySQL on the VPS itself.** Use `MYSQL_REPORTS_HOST=host.docker.internal`. MySQL must listen on the Docker bridge (e.g. `bind-address = 0.0.0.0` with ufw keeping 3306 closed to the internet), and the MySQL user must be allowed from `172.16.0.0/255.240.0.0`.

Apply and check:

```bash
docker compose -f docker-compose.prod.yml up -d api worker          # reload config.local
docker compose -f docker-compose.prod.yml exec worker python -m testpilot.cli check-report-db
docker compose -f docker-compose.prod.yml exec worker python -m testpilot.cli sync-reports        # push pending
docker compose -f docker-compose.prod.yml exec worker python -m testpilot.cli sync-reports --all  # re-push every run
```

Sync status appears under Administration ("MySQL reports: N pending") and at the bottom of each run page.

Example query:

```sql
SELECT run_id, project_name, test_case_name, status, steps_passed, steps_failed, finished_at
FROM tp_reports ORDER BY finished_at DESC LIMIT 50;
```

---

## 8. Operations

All commands run from `/opt/testpilot`. `C="docker compose -f docker-compose.prod.yml"`.

| Task | Command |
|---|---|
| Status | `$C ps` |
| Start / stop (data kept) | `$C up -d` / `$C stop` |
| Logs | `$C logs -f --tail=200 api worker` |
| Health check | `bash deploy/scripts/health.sh` |
| Update to latest code | `git pull && bash deploy/scripts/deploy.sh` |
| Run migrations only | `$C --profile tools run --rm migrate` |
| Migration status | `$C run --rm --no-deps api alembic current` |
| Backup now | `bash deploy/scripts/backup.sh` |
| Restore (destructive, asks to type RESTORE) | `bash deploy/scripts/restore.sh /var/backups/testpilot/<timestamp>` |
| Roll back to previous release | `bash deploy/scripts/rollback.sh` |
| Roll back to a specific release | `bash deploy/scripts/rollback.sh <tag>` (tags in `.deploy/history`) |
| Reset an admin password | `RESET=1 bash deploy/scripts/create-admin.sh admin` |
| Scale browser concurrency | edit `MAX_CONCURRENT_RUNS` in `.env`, then `$C up -d worker` |
| End-to-end verification | `bash deploy/scripts/verify-e2e.sh` (needs `WITH_SAMPLE=1`) |

**What `deploy.sh` does:**
1. Preflight: checks `.env` and `config.local` and validates the compose file.
2. Builds images tagged with the git commit.
3. Validates the application configuration.
4. Backs up PostgreSQL and evidence if data exists.
5. Runs migrations (serialised with an advisory lock).
6. Starts the containers and runs the health check.
7. Records the release.

It never runs `docker compose down -v` and never deletes volumes.

**Backups.** `backup.sh` writes `db.dump` (pg_dump custom format), `evidence.tar.gz`, copies of `.env` and `config.local`, and `SHA256SUMS` to `/var/backups/testpilot/<UTC timestamp>/`. It keeps the newest 14 (`KEEP=n`). Schedule it daily:

```bash
( crontab -l 2>/dev/null; echo '30 2 * * * cd /opt/testpilot && bash deploy/scripts/backup.sh >> /var/log/testpilot-backup.log 2>&1' ) | crontab -
```

Copy backups off the server regularly, e.g. with Hostinger VPS snapshots or `rsync` to another machine. A backup on the same disk doesn't protect against losing the VPS.

**Rollback and the database.** Migrations are additive (new tables and columns only), so an older release runs on a newer schema and `rollback.sh` only swaps the images. If a future release ever needs the old schema, restore the backup that `deploy.sh` took just before migrating, using `restore.sh`. That is an explicit, confirmed step.

---

## 9. Security checklist

- [ ] DNS has only the new `testingagent` A record; other records untouched
- [ ] `ufw status` shows only OpenSSH and Nginx Full; 5432, 8000, 8081 and 3306 are not open
- [ ] `health.sh` reports "PostgreSQL not exposed on the host"
- [ ] `.env` is `chmod 600` (root); `config.local` is `chmod 600` owned by uid 10001; neither is in git (`git status` is clean)
- [ ] `check-config` passes (strong `SESSION_SECRET`, Fernet key set, secure cookies, https `PUBLIC_URL`)
- [ ] `CREDENTIAL_ENCRYPTION_KEY` stored in a password manager, separately from backups
- [ ] HTTPS works, and HTTP redirects to HTTPS (`curl -I http://testingagent.unical.in`)
- [ ] `certbot renew --dry-run` succeeds
- [ ] First admin password changed from any temporary value; other users get the minimum role needed
- [ ] Only systems Unical is permitted to test are on the target allowlist
- [ ] `ALLOWED_PRIVATE_TARGETS` contains only `sample-app` (or nothing)
- [ ] Anthropic key set only in `.env` (never in frontend code or git); usage limits set in the Anthropic console
- [ ] MySQL report store uses `MYSQL_REPORTS_SSL=required` or `verify` across the internet, and a dedicated MySQL user limited to its database
- [ ] Daily backup cron in place and a restore tested at least once
- [ ] SSH: key-based login, password login disabled (`PasswordAuthentication no`)
- [ ] OS security updates: `apt install unattended-upgrades`

Built-in protections:
- Argon2id password hashing, account lockout after 5 failures, and an nginx rate limit on login.
- HttpOnly, Secure, SameSite=Strict session cookies, server-side sessions, and revocation on password or role change.
- CSRF header plus Origin checks, a trusted-host check, and strict CSP and security headers.
- Role-based access (viewer, tester, admin) and an audit log.
- Encrypted project secrets that are write-only through the API.
- Non-root containers with `no-new-privileges`, an internal database network, and resource limits on the worker.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `deploy.sh`: ".env still contains placeholders" | Fill in the generated values (§4 step 3) |
| api restarts with "Refusing to start with an unsafe production configuration" | `$C logs api`, then fix the listed settings |
| Banner "No test worker is online" | `$C logs worker`. Chromium usually needs more memory: use a bigger plan or set `MAX_CONCURRENT_RUNS=1` |
| Run fails with "not on the authorised target allowlist" | An admin must add the host under Administration |
| Run fails with "private/internal" | The target resolves to a private IP. Test it via its public name, or (operators only) add it to `ALLOWED_PRIVATE_TARGETS` |
| AI buttons disabled | Set `ANTHROPIC_API_KEY` in `.env`, then `$C up -d api`. Check the Administration page for Claude status. |
| MySQL "N pending" | `$C exec worker python -m testpilot.cli check-report-db`. On Hostinger, confirm the VPS IP is in Remote MySQL. |
| certbot fails | `dig +short testingagent.unical.in` must equal the VPS IP, and port 80 must be open |
| 502 from nginx | `$C ps`. Is `web` healthy and listening on 127.0.0.1:8081? |
