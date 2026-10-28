# TestAgent 1.4 — test-case import, test runs, mobile layout

Built on 1.3.0 (multi-tenancy, project access, storage & privacy). Additive only.

## New
- **Import test cases from CSV / Excel** (`/projects/<id>/import`): column mapping with suggestions,
  preview (writes nothing), duplicate handling by Test Case ID (update / skip / create), optional
  results (Pass/Fail, actual result, executed by/on), undo of a whole import. Templates (xlsx/csv).
- **Test runs**: planned executions of many cases (manual or automated), grouped by module or any
  column, result recording (single and bulk), close/reopen, export to Excel/CSV. Automated items queue
  normal browser runs; the worker writes the outcome back into the item.
- Test-case fields: Test Case ID, module, sub-module, priority, scenario type, assigned to, requirement,
  pre-conditions, test data, procedure, expected result, custom columns. "Draft steps with AI" turns a
  manual case into automated steps (draft only).
- Mobile layout (slide-in menu, responsive tables). Upload limit raised to 12 MB in nginx.

## Access & tenancy
- `test_runs` and `test_run_items` carry `tenant_id` with composite (id, tenant_id) foreign keys, like
  every other project child. All queries are scoped to the session's organization.
- Project permissions: `project.read` view/export test runs; `case.write` import and undo;
  `run.execute` create test runs, record results, queue automated runs; `ai.use` (and the organization's
  AI switch) for AI drafting. Organizations with their own storage get a versioned copy of imported cases.

## Database
- Migration **0006** (after 0005): new columns on `test_cases`, new tables `test_runs`, `test_run_items`.
  Rollback to 1.3.0 is safe without downgrading (1.3.0 ignores the new columns and tables).

## Deploy
- `deploy/scripts/deploy.sh` (backs up, builds, migrates, health-checks). On the host nginx set
  `client_max_body_size 12m;` for the site, then reload nginx.
