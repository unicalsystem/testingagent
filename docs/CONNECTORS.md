# Connectors: Azure DevOps, Jira, GitHub, GitLab and webhooks (release 1.7)

To add a connector, open a project, go to **Integrations**, and click **+ New connector**. A project can have any number of connectors. Each one is configured separately.

## What each connector does

| Connector | Test results | Bugs / issues |
|---|---|---|
| **Azure DevOps** (Services or Server) | Each run becomes an Azure Test Plans *test run*, with one result per case (outcome, duration, error) and the HTML report attached. With a **test plan + suite**, mapped cases are recorded against the plan's test points; unmapped cases are added as extra results. | A Bug work item (or Issue for the Basic process) for each failing case, with repro steps, the failure screenshot, area/iteration path, tags, priority and a link to the TestAgent run. |
| **Jira** (Cloud or Data Center) | — | An issue for each failing case (issue type and labels are configurable), with the screenshot attached. |
| **GitHub Issues** (incl. Enterprise) | — | An issue for each failing case, with labels. |
| **GitLab Issues** (gitlab.com or self-managed) | — | An issue for each failing case, with labels and the screenshot uploaded. |
| **Webhook** | A JSON POST with the run summary and the failed (optionally all) cases, signed with `X-TestAgent-Signature: sha256=<HMAC>`. Use it for ServiceNow, Redmine, Zoho, YouTrack, etc. | — |

**No duplicate issues.** Each test case keeps one issue per connector:

- If the same case fails again, TestAgent comments on the existing issue instead of opening a new one.
- If that issue was closed in the tracker, TestAgent opens a new one.
- When a failing case passes again, TestAgent adds a "passing again" note (you can turn this off).
- *Max new issues per run* (default 20) stops a tracker being flooded when many cases fail at once.

## When results are pushed

| Setting | Options |
|---|---|
| **Push** | After every finished run · Only runs with failures · Only when someone presses **Push** |
| **Run types** | API test runs · Automated browser runs · Manual test runs (pushed when the run is **closed**) |

Every API run, browser run and manual test run page has a **⇪ Push** button. It shows which connectors have received the run and lets you push (or push again) on demand.

Runs that finished before a connector was created or re-enabled are not pushed automatically; use the Push button for those.

Pushes are queued and sent by the worker. Failed pushes are retried up to 5 times (after 1, 5, 15 and 60 minutes). The connector's **Pushes** list shows each push's result, links to the created ADO run and issues, and errors, with a **Retry** button.

## Mapping cases to Azure DevOps test cases

To record results against the test points of an ADO test plan, enter the **Test plan ID** and **Test suite ID**, then map cases in either of two ways:

* **Mapping list** – one line per case: `TC-001, 4512`. The left side can be an imported case ID (e.g. `TC-001`), `case:<TestAgent case id>` or `api_case:<API case id>`.
* **"Imported case IDs are ADO test case IDs"** – when your imported cases already use ADO IDs (e.g. `4512` or `TC-4512`).

## Credentials and security

| Connector | Token needed |
|---|---|
| Azure DevOps | PAT with **Test Management (read & write)** and **Work Items (read & write)** |
| Jira Cloud | Account e-mail + API token |
| Jira Data Center | Personal access token |
| GitHub | Fine-grained token with **Issues: read & write** on the repository |
| GitLab | Project or personal access token with scope `api` |

* Tokens and webhook URLs are encrypted (Fernet). They are never shown again; the form shows only the last characters.
* Connector URLs must be **HTTPS on public addresses**. Internal or private hosts are refused, so connectors cannot reach the server's own network.
* Only project managers, QA engineers and administrators can create connectors and store tokens. Pushing on demand needs permission to run tests.
* **Test connection** checks the URL, the token and the project / plan / suite / issue type before any results are sent.
