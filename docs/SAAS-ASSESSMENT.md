# TestAgent → multi-tenant SaaS: architecture assessment and status

Assessed against the code deployed on 25 Oct 2026 (commit `2a7afad`, pushed from `/opt/testpilot`).

## 1. Current architecture (verified in the repository)

| Layer | Implementation | SaaS relevance |
|---|---|---|
| Frontend | React 19 + TypeScript (Vite), served by an unprivileged nginx container that also proxies `/api` | Single-page app; tenant routing added with a per-tenant router base path |
| API | FastAPI, cookie sessions (HMAC-hashed tokens, SameSite=Strict, CSRF header + Origin check), argon2 passwords, lockout | Session model extended with a tenant binding |
| Database | PostgreSQL 16, SQLAlchemy 2, Alembic (0001 initial, 0002 MySQL sync state) | All tenant-owned tables needed `tenant_id`; done in 0003 |
| Worker | Separate container; claims runs from PostgreSQL (`FOR UPDATE SKIP LOCKED` + advisory lock); Playwright/Chromium with an SSRF-filtering egress proxy | Queue entries now carry tenant; re-authorised at execution |
| Evidence | Private Docker volume, paths `projects/<pid>/runs/<rid>/`, SHA-256 per file, served only through the API | New runs under `tenants/<tid>/…`; external storage is phase 2 |
| Reports | HTML/JSON generated on demand from the DB + evidence; copied to MySQL `tp_reports` / `tp_report_steps` by an idempotent sync | Tenant columns added; external-only refactor is phase 2 |
| AI | Anthropic API from the API container only (worker has no key); generation output validated against the step schema | Tenant-level AI policy is phase 2 |
| Deployment | Docker Compose on a Hostinger VPS behind aaPanel nginx (`location / → 127.0.0.1:8081`), deploy/backup/restore/rollback scripts | No infrastructure change needed for path-based tenant URLs |

Findings that shaped the design:
- Every authorization decision was role-only (`viewer < tester < admin`) with global queries; any tenant
  layer had to reach every query, not only the routes.
- The allowlist of test targets was global; in a SaaS it would let one customer's approval authorise
  another customer's tests, so it became per tenant.
- `/api/system/status` exposed global queue counts and worker run ids; now scoped/anonymised.
- One existing test was stale after the live hotfix to Claude `tool_choice` (fixed, behaviour unchanged).
- The VPS checkout had no git history (`.git` was an empty directory); deployment now uses a real clone.

## 2. Requirement status (brief sections 1–14)

Legend: **Done** = implemented and covered by automated tests · **Partial** · **Not started** (later phase) · **Blocked** (needs credentials/environment).

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Preserve engine, endpoints, sessions, steps, AI, queue, evidence, reports, secrets, targets, audit | **Done** | All 53 release-1.0 tests pass unchanged through the legacy `/api/…` routes; worker end-to-end runs in a customer tenant |
| 2 | Tenant-aware model, enforceable tenant association, server-derived tenant context, worker re-checks, cross-tenant tests | **Done** (core entities) | Migration 0003, composite FKs (`test_database_refuses_cross_tenant_children`), `test_tenant_sees_only_its_own_data` (20 probes), `test_session_is_bound_to_its_tenant`, worker suspension test. Entities StorageConnection, IdentityProviderConfiguration, Subscription, UsageRecord, ProjectMembership: **Not started** |
| 3 | Public registration, Super Admin approval, provisioning, single-use time-limited owner activation, reserved/duplicate slugs, audit, no leakage | **Done** | `test_registration_*`, `test_expired_invitation_is_refused`, `test_reject_and_platform_access_is_super_admin_only`. Email delivery: **Blocked** (no mail service configured; links are shown to the approver) |
| 4 | Tenant URLs, no collision with system routes, branded login (name, logo, colours, welcome, support) | **Done** | Browser walkthrough (register → approve → activate → brand → sign-in page); `test_branding_validation_and_public_read` |
| 5 | Roles and granular backend permissions | **Done** for the 7 roles + Super Admin | `test_role_permissions_enforced_in_backend` (6 roles × 7 actions), `test_only_owner_can_grant_owner`. Microsoft Entra ID / Google OIDC: **Not started**, then **Blocked** on app registrations |
| 6 | Storage abstraction, external-only mode, tp_reports refactor | **Not started** (phase 2). Tenant columns added to MySQL tables now | — |
| 7 | Direct-to-customer storage, customer-hosted runner | **Not started** (phase 3) | — |
| 8 | SharePoint / Google Drive configuration | **Not started**; **Blocked** on Microsoft/Google app credentials | — |
| 9 | Testing security retained; approvals tenant-scoped | **Done** for tenant scope (`test_another_tenants_target_approval_does_not_apply`); approval for destructive tests: **Not started** | Existing SSRF/egress tests pass |
| 10 | Provider-aware evidence/report references | **Partial**: tenant-scoped paths and access; provider awareness in phase 2 | `test_customer_tenant_run_end_to_end` |
| 11 | AI privacy review and tenant controls | **Not started** (secrets are already never sent to Claude in 1.0; tenant toggle in phase 2) | — |
| 12 | SaaS operations | **Partial**: platform console (registrations, organizations, usage counts, suspend/reactivate, platform audit), tenant user/target/branding admin, tenant audit. Plans/quotas, retention, integration health: **Not started** | — |
| 13 | Deployment, migrations, env docs, rollback, smoke tests | **Done** for this phase | `docs/RELEASE-1.1-MULTITENANCY.md`, `test_upgrade_*` / `test_downgrade_*` migration tests on a populated 1.0 database |
| 14 | Acceptance tests | Phase-1 items **Done** (see above); SSO/storage/external-only items pending later phases | 80 automated tests |

## 3. Phase plan

1. **Phase 1 (this release, 1.1):** tenancy, registration/approval, tenant URLs + branding, roles/permissions, isolation tests.
2. **Phase 2:** storage abstraction; SharePoint (Graph) and Google Drive connectors with upload/read-back validation;
   external-only mode including the `tp_reports`/`tp_report_steps` path, temp-file lifecycle; tenant AI policy.
3. **Phase 3:** Microsoft Entra ID and Google OIDC per tenant; plans/quotas/usage records; retention/deletion workflows;
   project memberships; destructive-test approvals.
4. **Phase 4:** customer-hosted execution runner (separately deployable).
