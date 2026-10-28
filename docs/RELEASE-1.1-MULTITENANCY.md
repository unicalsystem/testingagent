# Release 1.1 — multi-tenant foundation (SaaS phase 1)

## What changed

| Area | Release 1.1 |
|---|---|
| URLs | `/` public landing page and registration · `/<org>/` an organization's app · `/unical/` is Unical's own organization (all release-1.0 data). Old links (`/projects/…`, `/runs/…`, `/admin`) redirect to `/unical/…`. |
| API | `/api/t/<org>/…` (tenant taken from the URL and checked against the session). `/api/…` still works and means the Unical organization. New: `/api/public/…` (registration, branding, activation) and `/api/platform/…` (Unical Super Admin). |
| Data | New tables `tenants`, `tenant_registrations`, `tenant_branding`, `invitations`. Every tenant-owned table has a mandatory `tenant_id`; children reference parents by composite `(id, tenant_id)` foreign keys, so PostgreSQL itself refuses cross-tenant rows. Usernames are unique per organization. Target allowlists are per organization. |
| Roles | Tenant Owner, Tenant Administrator, Project Manager, QA Engineer, Developer, Auditor/Read-only, plus the Unical Super Admin flag (platform operations only — it gives no access to customer projects, runs or evidence). Release-1.0 roles are migrated: admin → Tenant Administrator **and** Super Admin, tester → QA Engineer, viewer → Auditor. The old role names are still accepted by the API. |
| Registration | Public form → pending → Super Admin approves (or rejects) → organization created → single-use owner activation link (72 h, token in the URL fragment, only its SHA-256 stored). Reserved and duplicate addresses are refused. 5 registrations per IP per day. |
| Branding | Per organization: display name, logo (PNG/JPEG/WebP ≤ 200 KB, content-checked), primary colour, welcome text, support email/URL. Shown on the organization's sign-in page and sidebar. |
| Suspension | Super Admin can suspend/reactivate an organization: its sessions are revoked, sign-in and API return 403, branding disappears, queued runs wait (never executed) until reactivation. |
| Worker | Claims only runs of active organizations and re-checks organization and project before executing. New evidence goes to `tenants/<id>/projects/<id>/runs/<id>/`; release-1.0 evidence keeps its stored path. |
| MySQL reports | `tp_reports` gains `tenant_id`, `tenant_slug`; `tp_report_steps` gains `tenant_id` (added automatically with `ALTER TABLE … ADD COLUMN`, never dropped). `report_url` now points to `/<org>/runs/<id>`. |
| Email | No email service is configured: activation links are shown once to the approving Super Admin / inviting admin, who sends them. |

## Security notes (from the independent pre-release review, all fixed with regression tests)

- Super Admin accounts cannot be changed by tenant administrators (only via `testpilot.cli platform-admins`
  / `create-admin` on the server).
- An owner activation link can be reissued only while the organization has no owner; once an owner
  exists, Unical cannot mint access to that organization.
- Invitation acceptance is atomic: one link creates at most one account, even under concurrent use.
- `ALLOWED_PRIVATE_TARGETS` (private/loopback hosts) applies only to the Unical organization (and any slugs
  listed in `PRIVATE_TARGET_TENANTS`). Customer organizations can never reach private addresses.
- **Trust limitation until email is configured:** the approving Super Admin sees the first owner
  activation link and is trusted to deliver it. Every acceptance is recorded in the organization's own
  audit log (who, when, IP), which the customer owner can review.
- Release-1.0 `admin` users become Super Admins. Review with
  `docker compose -f docker-compose.prod.yml exec api python -m testpilot.cli platform-admins` and remove
  anyone who should not operate the platform with `--revoke USERNAME`.

## Not in this release (later phases)

Microsoft 365 / Google sign-in, SharePoint / Google Drive storage, external-only storage mode (and the
corresponding `tp_reports`/`tp_report_steps` refactor), customer-hosted runner, plans/quotas/usage records,
project-level memberships, retention workflows, AI privacy controls per tenant.

## Upgrade (run on the VPS from `/opt/testpilot`)

1. `git fetch && git checkout <release commit>` (or unpack the release into `/opt/testpilot`, keeping `.env` and `config.local`).
2. Optionally add the new `.env` keys from `.env.example` (all have safe defaults).
3. `bash deploy/scripts/deploy.sh` — builds, **backs up the database and evidence**, runs migration 0003, restarts, health-checks.
4. Check: `https://<domain>/` shows the landing page; `https://<domain>/unical/` shows the Unical sign-in; existing users sign in with their usual usernames and passwords.

## Rollback to 1.0

Release 1.0 cannot run on the 1.1 schema. While only the Unical organization exists:

```bash
docker compose -f docker-compose.prod.yml --profile tools run --rm migrate alembic downgrade 0002
bash deploy/scripts/rollback.sh
```

If customer organizations were created after the upgrade, restore the pre-upgrade backup instead
(`bash deploy/scripts/restore.sh /var/backups/testpilot/<timestamp>` after `rollback.sh`), which discards
everything created since.

## Operator commands

```bash
docker compose -f docker-compose.prod.yml exec api python -m testpilot.cli list-tenants
docker compose -f docker-compose.prod.yml exec api python -m testpilot.cli create-admin --username NAME [--tenant SLUG] [--platform-admin]
docker compose -f docker-compose.prod.yml exec api python -m testpilot.cli allow-target --host HOST --tenant SLUG
```
