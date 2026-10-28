# Release 1.2 — authentication & security, Microsoft 365 sign-in, groups and project access

Everything new is **off by default** and configured from the UI. After the upgrade, nothing changes for
users until a Super Admin or organization administrator switches something on. Existing users keep exactly
the project access they had (migration 0004 gives every non-administrator an "All projects" assignment with
their current role).

## What's new

| Area | Where | Notes |
|---|---|---|
| Sign-in policy | Platform → Authentication & Security | Unical organization: password only / Microsoft only / both. What customer organizations may use. Username or e-mail login. Lockout attempts and duration. Session length and idle timeout. Password reset by e-mail. |
| Google reCAPTCHA v3 | same | Site key, secret key (encrypted, write-only, rotate by entering a new one), minimum score 0.0–1.0, which pages are protected (sign-in, registration, password reset), and what to do if Google is unreachable: **refuse** (default) or **allow and audit**. "Check secret with Google" validates the key. Verified server-side: success, expected action, hostname, score, single use. |
| Trusted IPs | same + each organization's Identity & SSO | Addresses or CIDR ranges that skip the CAPTCHA. Platform list applies to every protected page; an organization's list applies to its own sign-in page. Ranges broader than /8 (IPv4) or /32 (IPv6) are refused. Password checks, lockout and permissions still apply. |
| E-mail (SMTP) | Platform → E-mail (SMTP) | Server, port, STARTTLS/SSL, account, password (encrypted), sender. "Send test". Used for password reset, invitations and organization approvals (links are still shown on screen too). |
| Microsoft 365 (Unical) | Platform → Microsoft 365 (Unical) | Unical's own Entra tenant for platform administration. |
| Microsoft 365 (organizations) | Administration → Identity & SSO | Each organization configures its own Entra tenant independently. Connection test before enabling, rotate secret, disconnect, allowed e-mail domains, allowed Entra groups, and new-user policy (invite-only, or create accounts on first sign-in for allowed domains with a chosen role and **no** project access). |
| Groups | Administration → Groups | Name, description, active/inactive, members. |
| Project access | Administration → Project access; Project → Members & access | Assign a user or a group to one project or **All projects** (confirmation required), with a project role and optional start/end date and time in a chosen time zone. Statuses: active, future, expired, revoked. Edit, extend, renew, revoke. |
| Roles & permissions | Administration → Roles & permissions | Read-only matrix of organization and project roles. |

## Access model (enforced in the backend on every request)

* **Tenant Owner / Tenant Administrator**: every project of their organization (implicit).
* **Everyone else**: only projects covered by an assignment that is in force now (status active,
  start ≤ now < end), given directly or through an *active* group they belong to.
* Effective permissions = union of the project roles of all applicable assignments:

| Permission | Project Manager | QA Engineer | Developer | Auditor |
|---|---|---|---|---|
| View project, cases, runs, evidence, reports | ✓ | ✓ | ✓ | ✓ |
| Edit project | ✓ | ✓ | | |
| Archive project, manage project members | ✓ | | | |
| Project secrets | ✓ | ✓ | | |
| Edit test cases, run, cancel, use AI | ✓ | ✓ | ✓ | |

* Expiry and revocation take effect immediately, also for users who are already signed in. A queued run
  is re-checked when the worker picks it up and is not executed if its requester has lost access.
* Project Managers can grant access only to their own project, roles up to Project Manager, never to
  themselves or to a group they belong to, and never "All projects".
* A date without a time means "until the end of that day" in the chosen time zone.
* New users created after 1.2 have **no project access** until an assignment is added (or they are
  owners/administrators). Microsoft sign-in alone never grants project access.
* Custom roles are not available in this release.

## Microsoft Entra ID setup (per organization, and for Unical)

1. Entra admin center → **App registrations → New registration**: *Accounts in this organizational directory
   only (single tenant)*; Redirect URI type **Web**:
   `https://testagent.unical.in/api/public/oidc/microsoft/callback` (also shown on the settings page).
2. **Certificates & secrets → New client secret**; copy the *Value*.
3. In TestAgent enter Directory (tenant) ID, Application (client) ID and the secret → **Save → Test connection**
   → enable Microsoft in the sign-in methods.
4. No API permissions or admin consent are needed (scopes `openid profile email`). For group restrictions,
   add the **groups** claim under *Token configuration*.
5. Users link their account from the sidebar (**Link Microsoft account**) after signing in with a password,
   or activate an invitation with Microsoft (the Microsoft account must be the invited address).

Security properties: authorization-code flow with PKCE; state, nonce and a browser-binding cookie stored
server-side and bound to the organization; ID-token signature (RS256, provider JWKS), issuer, tenant (tid),
audience, expiry checked; accounts are matched by Entra object ID, never by e-mail; each organization's code
exchange uses only its own client secret; turning Microsoft off signs out Microsoft sessions; turning
password sign-in off signs out password sessions.

## reCAPTCHA setup

google.com/recaptcha/admin → **reCAPTCHA v3** → domain `testagent.unical.in` → copy site key and secret →
Platform → Authentication & Security → enable, paste, choose pages → Save (the secret is checked with
Google before reCAPTCHA is switched on). Add your office IPs under Trusted IP addresses if staff should not
be challenged.

## Recovery (if a setting locks you out)

```bash
cd /opt/testpilot
docker compose -f docker-compose.prod.yml exec api python -m testpilot.cli auth-recovery [--disable-captcha] [--disable-microsoft]
docker compose -f docker-compose.prod.yml exec -T api python -m testpilot.cli create-admin --username admin --reset   # new password from stdin
```

## Upgrade

Migration **0004** (additive). `deploy/scripts/deploy.sh` takes a database + evidence backup first.
New Python dependencies: PyJWT, httpx, tzdata. The web container's Content-Security-Policy now allows
Google's reCAPTCHA (`www.google.com`, `www.gstatic.com`). No `.env` changes are required.

## Rollback to 1.1

```bash
docker compose -f docker-compose.prod.yml --profile tools run --rm migrate alembic downgrade 0003
bash deploy/scripts/rollback.sh
```
The downgrade drops the 1.2 tables (settings, groups, assignments, Microsoft links). 1.1 then runs with
tenant-wide roles exactly as before.

## API additions

`/api/public/tenants/{slug}/auth-config`, `/api/public/registration-config`, `/api/public/password-reset/{request,confirm}`,
`/api/public/oidc/microsoft/{start,callback}`, `/api/t/{slug}/auth/microsoft/link-start`, `DELETE /api/t/{slug}/auth/microsoft-link`,
`/api/t/{slug}/admin/sso[/policy|/microsoft|/microsoft/test]`, `/api/t/{slug}/access/{groups,assignments,directory,roles,users/{id}/effective}`,
`/api/platform/settings[/auth|/recaptcha|/recaptcha/test|/trusted-ips|/smtp|/smtp/test]`.
Changed: `POST /auth/login` accepts `captcha_token`; locked accounts now answer 401 like a wrong password;
project responses include `my_permissions`; listing runs of an inaccessible project answers 404.
