# TestAgent 1.7 — Connectors

* **Azure DevOps connector:**
  * Publishes test results to Azure Test Plans: a test run per TestAgent run, results per case, and the HTML report attached.
  * Can record results against the test points of a plan/suite, using a case mapping.
  * Opens Bug work items for failures, with the screenshot, area/iteration path, tags and priority.
* **Jira, GitHub and GitLab connectors:** open one issue per failing case. A repeat failure comments on the existing issue, and a recovery adds a "passing again" note.
* **Webhook connector:** sends signed JSON results to any other tool.
* **Configurable per connector:**
  * Trigger: every run, failures only, or manual push.
  * Run types: API runs, automated browser runs, and manual test runs when closed.
  * Issue cap per run.
* **Push button** on every run page, plus a push history with retry.
* **Upgrade:** migration `0009` adds three tables (nothing existing changes). The worker processes pushes in its housekeeping loop.

See docs/CONNECTORS.md.
