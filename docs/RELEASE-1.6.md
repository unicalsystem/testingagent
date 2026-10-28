# TestAgent 1.6 — API testing and uptime monitoring

Released 27 Oct 2026. The product is renamed from TestPilot AI to **TestAgent** and served at https://testagent.coolaudit.com.

## New

* **API testing** (project → API testing). Give TestAgent an OpenAPI / Swagger document (by URL or upload) or a list of endpoints, plus credentials (API key, bearer, basic, OAuth2 client credentials). It generates positive, authentication and negative cases, runs them through the egress proxy and produces HTML / JSON reports with a SHA-256 fingerprint. Runs are read-only by default. Claude can suggest extra edge cases on request.
* **Uptime monitoring** (project → Monitoring, plus the new **API monitoring** page in the main menu). Opt-in scheduled checks, incidents, and down/recovery alerts by e-mail, Slack / Teams / generic webhook and an in-app banner, configurable per monitor. Uptime and downtime reports cover 7, 30 or 90 days.
* The API now accepts every host listed in `ALLOWED_ORIGINS`, not only the one in `PUBLIC_URL`. This keeps old domains working while you switch over.

## Upgrade

* Database migration `0008` adds seven tables. The migration only adds; nothing existing changes.
* New dependency: `PyYAML` (for YAML specifications). Rebuild the images.
* The worker runs a monitoring scheduler thread next to the run loop. No new service or port is needed.

```
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml --profile tools run --rm migrate
docker compose -f docker-compose.prod.yml up -d
```

See docs/API-TESTING.md.
