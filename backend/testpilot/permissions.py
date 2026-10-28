"""Role-based permissions, enforced in the backend.

Each tenant user has exactly one tenant role. Permissions are checked per request by
`deps.require_perm(...)`, always against the tenant resolved from the URL and the session.

The Unical Super Admin flag (User.is_platform_admin) is separate: it unlocks the /platform API
(registrations, tenant lifecycle) and nothing else. It does not grant access to any tenant's
projects, test cases, runs, evidence or reports.
"""
from __future__ import annotations

from .models import LEGACY_ROLE_MAP, ROLES

OWNER, ADMIN, PM, QA, DEV, AUDITOR = ROLES

# TENANT-level permission -> roles that hold it (from the user's organization role).
# Project-level permissions (read, edit, run, evidence, reports, members ...) come from project
# assignments and are evaluated in access.py.
MATRIX: dict[str, frozenset[str]] = {
    "tenant.member": frozenset(ROLES),                     # signed-in member of the organization
    "tenant.settings": frozenset({OWNER, ADMIN}),          # branding, organization configuration
    "tenant.users": frozenset({OWNER, ADMIN}),             # create/disable users, invitations
    "tenant.groups": frozenset({OWNER, ADMIN}),            # groups and memberships
    "tenant.assignments": frozenset({OWNER, ADMIN}),       # project access assignments (any project)
    "tenant.targets": frozenset({OWNER, ADMIN}),           # approve test target hosts
    "tenant.audit": frozenset({OWNER, ADMIN, AUDITOR}),    # read the organization audit log
    "tenant.storage": frozenset({OWNER, ADMIN}),           # storage configuration (release 1.3)
    "tenant.sso": frozenset({OWNER, ADMIN}),               # identity & SSO configuration
    "project.create": frozenset({OWNER, ADMIN, PM, QA}),   # creator is assigned to the new project
}

ROLE_LABELS = {
    OWNER: "Tenant Owner",
    ADMIN: "Tenant Administrator",
    PM: "Project Manager",
    QA: "QA Engineer",
    DEV: "Developer",
    AUDITOR: "Auditor / Read-only",
}

# Who may assign which role: only an owner can create or promote owners.
ASSIGNABLE_BY = {
    OWNER: frozenset(ROLES),
    ADMIN: frozenset(ROLES) - {OWNER},
}


def normalize_role(role: str) -> str:
    """Accepts release-1.0 role names (admin/tester/viewer) as aliases."""
    r = (role or "").strip().lower()
    r = LEGACY_ROLE_MAP.get(r, r)
    if r not in ROLES:
        raise ValueError(f"unknown role '{role}'")
    return r


def has(role: str, perm: str) -> bool:
    if perm not in MATRIX:
        raise KeyError(f"unknown permission {perm}")
    return role in MATRIX[perm]


def permissions_for(role: str) -> list[str]:
    return sorted(p for p, roles in MATRIX.items() if role in roles)


def legacy_role(role: str) -> str:
    """Closest release-1.0 role, returned to old API clients alongside the new role."""
    if role in (OWNER, ADMIN):
        return "admin"
    if role in (PM, QA, DEV):
        return "tester"
    return "viewer"
