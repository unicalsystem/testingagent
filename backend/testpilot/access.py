"""Project-level authorization (release 1.2).

Model (documented in docs/ACCESS-MODEL.md):
  * Tenant Owners and Tenant Administrators have every project permission on every project of their
    organization (they administer it). Nobody has access to another organization's projects.
  * Everyone else gets project permissions only from ACTIVE assignments:
      - direct (assignment.user_id = user), or
      - through a group (assignment.group_id = g) where g is active and the user is a member of g,
    covering the project (scope "all" = every current and future project of the organization,
    scope "project" = that project), and in force now: starts_at <= now < ends_at (either may be open).
  * Effective permissions = union of the permissions of the roles of all applicable assignments.
  * The account must be active and the organization active (checked by deps.current_user/current_tenant).
  * Evaluated on every request from the database: an expired or revoked assignment stops working
    immediately, also for sessions that were signed in before. Nothing is cached.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .models import GroupMember, Project, ProjectAssignment, User, UserGroup

ADMIN_ROLES = frozenset({"tenant_owner", "tenant_admin"})
PM, QA, DEV, AUD = "project_manager", "qa_engineer", "developer", "auditor"

PROJECT_MATRIX: dict[str, frozenset[str]] = {
    "project.read": frozenset({PM, QA, DEV, AUD}),
    "project.update": frozenset({PM, QA}),
    "project.archive": frozenset({PM}),
    "project.retarget_with_secrets": frozenset(),          # administrators only
    "project.members": frozenset({PM}),
    "secret.write": frozenset({PM, QA}),
    "case.write": frozenset({PM, QA, DEV}),
    "run.execute": frozenset({PM, QA, DEV}),
    "run.cancel": frozenset({PM, QA, DEV}),
    "ai.use": frozenset({PM, QA, DEV}),
    "evidence.read": frozenset({PM, QA, DEV, AUD}),
    "report.read": frozenset({PM, QA, DEV, AUD}),
}
PROJECT_PERMS = tuple(PROJECT_MATRIX)
ROLE_RANK = {AUD: 0, DEV: 1, QA: 2, PM: 3}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _in_force(t: datetime):
    return and_(ProjectAssignment.status == "active",
                or_(ProjectAssignment.starts_at.is_(None), ProjectAssignment.starts_at <= t),
                or_(ProjectAssignment.ends_at.is_(None), ProjectAssignment.ends_at > t))


def _principal(db: Session, user: User):
    groups = (select(GroupMember.group_id).join(UserGroup, and_(UserGroup.id == GroupMember.group_id,
                                                                 UserGroup.tenant_id == GroupMember.tenant_id))
              .where(GroupMember.user_id == user.id, GroupMember.tenant_id == user.tenant_id,
                     UserGroup.is_active.is_(True)))
    return or_(ProjectAssignment.user_id == user.id, ProjectAssignment.group_id.in_(groups))


def is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES


def roles_for(db: Session, user: User, project_id: int, at: datetime | None = None) -> set[str]:
    if is_admin(user):
        return {"*"}
    t = at or now()
    rows = db.scalars(select(ProjectAssignment.role).where(
        ProjectAssignment.tenant_id == user.tenant_id, _principal(db, user), _in_force(t),
        or_(ProjectAssignment.scope == "all",
            and_(ProjectAssignment.scope == "project", ProjectAssignment.project_id == project_id)))).all()
    return set(rows)


def perms_for(db: Session, user: User, project_id: int, at: datetime | None = None) -> set[str]:
    roles = roles_for(db, user, project_id, at)
    if "*" in roles:
        return set(PROJECT_PERMS)
    return {p for p, allowed in PROJECT_MATRIX.items() if roles & allowed}


def has(db: Session, user: User, project_id: int, perm: str) -> bool:
    if perm not in PROJECT_MATRIX:
        raise KeyError(perm)
    return perm in perms_for(db, user, project_id)


def accessible_project_ids(db: Session, user: User) -> set[int] | None:
    """None = every project of the tenant; otherwise the ids the user can read now."""
    if is_admin(user):
        return None
    rows = db.execute(select(ProjectAssignment.scope, ProjectAssignment.project_id).where(
        ProjectAssignment.tenant_id == user.tenant_id, _principal(db, user), _in_force(now()))).all()
    if any(scope == "all" for scope, _ in rows):
        return None
    return {pid for _, pid in rows if pid is not None}


def require(db: Session, user: User, project: Project | None, perm: str) -> Project:
    """404 if the project is not visible to the user (other tenant, archived, no assignment);
    403 if visible but the permission is missing."""
    if not project or project.archived or project.tenant_id != user.tenant_id:
        raise HTTPException(404, "Project not found")
    p = perms_for(db, user, project.id)
    if "project.read" not in p:
        raise HTTPException(404, "Project not found")
    if perm not in p:
        raise HTTPException(403, "You do not have permission for this action on this project")
    return project


def assignment_state(a: ProjectAssignment, t: datetime | None = None) -> str:
    t = t or now()
    if a.status == "revoked":
        return "revoked"
    if a.ends_at and a.ends_at <= t:
        return "expired"
    if a.starts_at and a.starts_at > t:
        return "future"
    return "active"


def mark_expired(db: Session) -> list[ProjectAssignment]:
    """Bookkeeping only (enforcement never depends on it): flips status to 'expired' for assignments past
    their end, so listings and the audit trail show it. Returns the rows changed."""
    t = now()
    rows = db.scalars(select(ProjectAssignment).where(ProjectAssignment.status == "active",
                                                      ProjectAssignment.ends_at.is_not(None),
                                                      ProjectAssignment.ends_at <= t).with_for_update(skip_locked=True)).all()
    for r in rows:
        r.status = "expired"
    return rows
