# API testing and uptime monitoring (release 1.6)

## API testing

Open a project, go to **API testing**, and click **+ New API test**.

1. **API:** choose where the test cases come from.
   * **OpenAPI / Swagger URL** (recommended): the worker downloads the document (JSON or YAML, OpenAPI 3.x or Swagger 2.0) and generates cases for every operation.
   * **Upload or paste** a document (up to 2 MB through the web server).
   * **Endpoint list:** if you have no specification, enter one endpoint per line: `METHOD /path [JSON body] [-> expected status]`.
2. **Credentials:** API key (header or query), bearer token, basic auth, or OAuth2 client credentials. The token is fetched automatically and cached during a run. Secrets are Fernet-encrypted, never returned by the API, and masked as `[secret]` in results, reports and logs.
3. **Options:** response-time limit, request timeout, TLS verification, and whether write tests are allowed. Then click **Generate & run**.

### What is generated (deterministic, no AI)

| Category | Cases |
|---|---|
| Positive | Every operation with documented or example parameters and bodies. Asserts the documented 2xx status, the content type, the documented response schema and the response-time limit. |
| Auth | Requests without credentials are rejected with 401/403. One request with invalid credentials is also rejected. |
| Negative | A missing required query parameter, an invalid path parameter type and an empty required body each return 4xx. An unknown endpoint returns 4xx. |

Path parameters without examples are filled at run time from ids returned by earlier calls. For example, `GET /pets` feeds `GET /pets/{petId}`.

**✦ Suggest cases with AI** asks Claude for extra edge cases. Claude sees only the endpoint list and the names of existing cases, never credentials or response data. You choose which suggestions to save.

### Safety

* Every host contacted must be on the organization's authorised target allowlist: the API, the OAuth2 token URL and the spec URL. All traffic goes through the per-run egress proxy, the same as browser runs (private-address and metadata blocking, DNS pinning on every redirect).
* **Write requests** (POST/PUT/PATCH/DELETE) are sent only when the suite allows them **and** the run explicitly includes them. Otherwise they are reported as *skipped*.
* Credentials can only be re-pointed to another host by an administrator.

### Reports

Each run page links to an HTML report (self-contained and printable) and a JSON report. The JSON report carries a SHA-256 fingerprint, which is also stored on the run. Results are stored in PostgreSQL. Response excerpts are limited to 4 KB per request.

## Uptime monitoring (opt-in)

Open a project, go to **Monitoring**, and click **+ New monitor**. Each monitor has:

* the endpoint (GET or HEAD), expected status (e.g. `2xx`), optional text the response must contain, an optional slow-response limit and a timeout;
* a check interval of 1, 2, 5, 10, 15, 30 or 60 minutes, and the number of failed checks in a row before it is marked **down**;
* optionally, the credentials of an API suite (only sent to that suite's own host);
* **alert channels**, any combination of:
  * **E-mail:** to the listed addresses, through Platform settings → SMTP.
  * **Webhook:** Slack incoming webhook, Microsoft Teams (Workflows "post to channel" webhook, Adaptive Card) or generic JSON POST. HTTPS on public addresses only; the URL is stored encrypted.
  * **In-app:** a banner for everyone with access to the project, plus the dashboard.

Alerts are sent when a monitor goes down and when it recovers (with the downtime duration). **Send test alert** checks the configuration.

Downtime runs from the first failed check of an incident to the first successful check after it. The monitor page shows uptime for the last 24 h, 7 days and 30 days, response times, incidents and recent checks. **Downtime report** downloads an HTML (or `format=json`) report for 7, 30 or 90 days. Check history is kept for 90 days.

If a monitor's host is not on the allowlist, the monitor is *paused* (not checked, no alerts) until an administrator authorises the host.

## API (tenant-scoped, under `/api/t/<org>/`)

`GET|POST /projects/{pid}/api-suites` · `GET|PUT|DELETE /api-suites/{id}` · `PUT /api-suites/{id}/credentials` ·
`POST /api-suites/{id}/generate` · `GET|POST /api-suites/{id}/cases` · `POST /api-suites/{id}/cases/bulk` ·
`POST /api-suites/{id}/cases/enable` · `PUT|DELETE /api-cases/{id}` · `POST /api-suites/{id}/ai/suggest` ·
`GET|POST /api-suites/{id}/runs` · `GET /api-runs/{id}` · `POST /api-runs/{id}/cancel` · `GET /api-runs/{id}/report?format=html|json` ·
`GET|POST /projects/{pid}/monitors` · `GET|PUT|DELETE /monitors/{id}` · `POST /monitors/{id}/check-now` ·
`POST /monitors/{id}/test-alert` · `GET /monitors/{id}/report?days=30&format=html|json` · `GET /monitoring/overview` · `GET /monitoring/alerts`
