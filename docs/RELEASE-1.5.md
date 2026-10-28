# TestAgent 1.5 — AI automation of manual test cases

Built on 1.4.0. Additive only.

## New
- **AI automation jobs**: pick manual cases (from the case list, a test run, or "all manual"); the worker
  explores the application (project sign-in setup steps + same-host pages, navigation only) to build an
  application map, Claude drafts browser steps per case, each draft can be verified by a trial run with a
  bounded repair attempt (error + failure screenshot), then saved automatically or after review.
- Project **automation settings**: sign-in setup steps and notes for Claude. Cases can run the setup
  steps first (`use_setup`).
- Job page with per-case drafts, edit/approve/reject/retry, bulk approve, cancel.

## Access, tenancy, data residency
- `automation_jobs` / `automation_items` carry `tenant_id` with composite foreign keys to project, job
  and test case. Every query is scoped to the session's organization; jobs of suspended organizations wait.
- Permissions: view = `project.read`; settings = `project.update`; start/retry = `ai.use` + `case.write`;
  approve/reject/cancel = `case.write`. The organization's AI switch blocks new jobs and stops queued
  items from being sent to Claude.
- Trial runs follow the organization's storage choice (and are skipped while storage is not usable).
  Failure screenshots are only read back while TestAgent still holds them; files already in an
  organization's own SharePoint / Google Drive are never fetched back for Claude.
- Applied drafts write a versioned case copy to organizations with their own storage.

## Database
- Migration **0007** (after 0006): `projects.setup_steps`, `projects.app_notes`, `test_cases.use_setup`,
  tables `automation_jobs`, `automation_items`. Rollback to 1.4.0 is safe without downgrading.
