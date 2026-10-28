# TestAgent 1.3.0 — Storage & data residency, privacy & legal

## What's new

### Storage & data residency (spec sections 7–9)
- **Platform → Storage infrastructure** (Unical Super Admin): enable/disable Unical-managed storage, customer
  SharePoint and customer Google Drive; default policy (*Organization chooses*, *Unical-managed only*,
  *Customer-owned only*); per-organization override; maximum artifact size; how long files may wait for an
  unavailable customer store; default top folder; local storage health check; storage audit trail.
  A change that would invalidate an organization's current choice lists the affected organizations and must be
  acknowledged; they are flagged and cannot run tests until an administrator chooses again. Nothing is switched
  automatically.
- **Administration → Storage & data residency** (organization owners/admins):
  - *Unical-managed storage* requires an explicit agreement checkbox. Organizations that existed before 1.3 keep
    Unical storage (recorded as a "legacy" agreement) and are asked to confirm it.
  - *SharePoint*: the organization's own Entra app registration (client credentials; least privilege =
    Graph application permission `Sites.Selected` with write access to one site), or the Identity & SSO app.
    Site search / site URL → document library → folder browser (create folders) → test → activate.
  - *Google Drive*: the organization's own service-account JSON key; files go to a **Shared Drive** where the
    service account is a Content manager (service accounts cannot store files in a personal My Drive).
  - Folder layout (configurable): `TestAgent-AI/<Project (id)>/run-<id>/{test-cases,evidence,reports}`;
    versioned test-case definitions at `TestAgent-AI/<Project>/test-cases/<Case (id)>/v<N>.json`.
  - Retention (days), permission for TestAgent to delete the files it created, AI on/off switch.
  - "Delete data held on Unical servers": removes local evidence, the MySQL report copies and step error/AI texts.
- **Customer-owned storage mode** (verified against the worker, report, MySQL and backup paths):
  - The storage destination is fixed when a run starts; if the store is unreachable the run is not executed and
    nothing is recorded.
  - Files are written to a spool area (`/data/evidence/_spool`, **excluded from backups**), uploaded, verified
    (size + SharePoint QuickXorHash / Drive SHA-256, falling back to a download-and-compare), then deleted locally.
  - After upload, step error texts and AI analyses are removed from PostgreSQL; the run page reads them back from
    the organization's report. Reports are never copied to MySQL.
  - If uploads fail, files wait and are retried every 30 s; after `spool_max_hours` (default 24) they are deleted
    and marked "not stored". They are never moved to Unical storage.
  - Credentials are Fernet-encrypted, never returned by the API, visible to no one.
- What still runs on Unical infrastructure is stated on the screen (browser execution, operational metadata,
  AI calls). A customer-hosted execution option is **not** part of 1.3.

### Privacy & legal (DPDP Act 2023 / DPDP Rules 2025, UK GDPR & DPA 2018, CCPA/CPRA)
- Public **/privacy** and **/terms** pages (footer links everywhere, cookie notice, public request form).
  Legal entity, address, Grievance Officer, DPO, UK representative, US contact and hosting location are set in
  **Platform → Legal & privacy**. "Publish as a new version" makes every user accept again.
- Registration: country dropdown (ISO list), separate consents (Terms; Privacy Policy/processing; 18+), recorded
  with version, time and IP.
- Users must accept the current Terms & Privacy Policy before using the app (recorded per version).
- **Privacy & my data** (every user): download my data (JSON), raise requests (access, portability,
  correction, erasure, withdraw consent, restrict/object, opt-out, nomination, grievance) and follow them.
- Organization admins: **Administration → Privacy requests**, "Erase personal data" on a user (anonymisation).
  Super Admin: all requests, response deadline (default 30 days), retention periods (audit 730 d, rejected
  registrations 365 d, sessions 30 d), purged automatically by the worker.

### Interface
- Collapsible left menu (remembered per browser) with icons.
- Reset-to-default icon beside the branding colour code.
- "Unical Systems" everywhere instead of "Unical Systems Private Limited".

## Before you rely on the legal pages
The Privacy Policy and Terms are templates aligned with the laws above, not legal advice. Fill in
**Platform → Legal & privacy** (Grievance Officer name/e-mail is required by the DPDP Act; a UK Article 27
representative is needed if you serve UK users without a UK establishment) and have counsel review the text.
The legal entity name is configurable; contracts normally name the registered entity.

## Deployment
Same procedure as 1.2 (`deploy/scripts/deploy.sh`). Migration `0005` runs automatically: existing organizations
keep Unical-managed storage; existing evidence stays where it is. New dependencies: none.

## Rollback
1. `docker compose -f docker-compose.prod.yml --profile tools run --rm migrate alembic downgrade 0004`
2. `deploy/scripts/rollback.sh`

Downgrading loses the storage choices, privacy requests and acceptance records. Evidence already uploaded to
customer storage stays there but is no longer linked from TestAgent.
