"""Groups and project access assignments (tenant-scoped).

Who may manage what:
  * Tenant Owner / Administrator (tenant.groups, tenant.assignments): any group, any assignment, scope
    "all" or "project", any project role.
  * Project Manager of a project (project.members on that project): assignments with scope "project" for
    that project only, roles up to Project Manager, never for themselves, never "all projects".
Nobody can grant tenant-level administrative rights through groups or assignments: assignment roles are
project roles only (the database refuses anything else).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import access, permissions
from ..db import get_db
from ..models import PROJECT_ROLES, GroupMember, Project, ProjectAssignment, User, UserGroup
from .deps import audit, member, require_perm

router = APIRouter(prefix="/access", tags=["access"])
groups_perm = require_perm("tenant.groups")


# ---- schemas -----------------------------------------------------------------------------------
class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    is_active: bool = True


class MembersIn(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)


class AssignmentIn(BaseModel):
    user_id: int | None = None
    group_id: int | None = None
    scope: str = Field(pattern="^(all|project)$")
    project_id: int | None = None
    role: str = Field(pattern="^(project_manager|qa_engineer|developer|auditor)$")
    # Either full ISO-8601 datetimes with offset, or dates (+ optional HH:MM) in `timezone`.
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    start_time: time | None = None
    end_date: date | None = None
    end_time: time | None = None
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    reason: str = Field(default="", max_length=300)
    confirm_all_projects: bool = False


class AssignmentPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(project_manager|qa_engineer|developer|auditor)$")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    start_time: time | None = None
    end_date: date | None = None
    end_time: time | None = None
    clear_end: bool = False
    timezone: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=300)


# ---- helpers -----------------------------------------------------------------------------------
def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, f"Unknown time zone '{name}'")


def window(tzname: str, starts_at, ends_at, start_date, start_time, end_date, end_time):
    """Returns (starts_at_utc, ends_at_utc). A date-only end means: until the END of that day in the
    chosen time zone (i.e. the next local midnight), so access never stops early because of UTC."""
    tz = _tz(tzname)
    s = e = None
    if starts_at:
        if starts_at.tzinfo is None:
            raise HTTPException(422, "starts_at needs a time-zone offset")
        s = starts_at.astimezone(timezone.utc)
    elif start_date:
        s = datetime.combine(start_date, start_time or time(0, 0), tz).astimezone(timezone.utc)
    if ends_at:
        if ends_at.tzinfo is None:
            raise HTTPException(422, "ends_at needs a time-zone offset")
        e = ends_at.astimezone(timezone.utc)
    elif end_date:
        local = (datetime.combine(end_date, end_time, tz) if end_time
                 else datetime.combine(end_date + timedelta(days=1), time(0, 0), tz))
        e = local.astimezone(timezone.utc)
    if s and e and e <= s:
        raise HTTPException(422, "The end must be after the start")
    return s, e


def _group(db: Session, me: User, gid: int) -> UserGroup:
    g = db.get(UserGroup, gid)
    if not g or g.tenant_id != me.tenant_id:
        raise HTTPException(404, "Group not found")
    return g


def _user(db: Session, me: User, uid: int) -> User:
    u = db.get(User, uid)
    if not u or u.tenant_id != me.tenant_id:
        raise HTTPException(404, "User not found")
    return u


def _member_of(db: Session, me: User, gid: int) -> bool:
    return db.get(GroupMember, (gid, me.id)) is not None


def _g_out(db: Session, g: UserGroup) -> dict:
    members = db.execute(select(User.id, User.username, User.display_name, User.email).join(
        GroupMember, GroupMember.user_id == User.id).where(GroupMember.group_id == g.id,
                                                          GroupMember.tenant_id == g.tenant_id)
        .order_by(User.username)).all()
    return {"id": g.id, "name": g.name, "description": g.description, "is_active": g.is_active,
            "created_at": g.created_at, "updated_at": g.updated_at,
            "members": [{"id": m.id, "username": m.username, "display_name": m.display_name, "email": m.email}
                        for m in members]}


def _a_out(db: Session, a: ProjectAssignment, names: dict | None = None) -> dict:
    u = db.get(User, a.user_id) if a.user_id else None
    g = db.get(UserGroup, a.group_id) if a.group_id else None
    p = db.get(Project, a.project_id) if a.project_id else None
    return {"id": a.id, "principal_type": "user" if a.user_id else "group",
            "user": {"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email} if u else None,
            "group": {"id": g.id, "name": g.name, "is_active": g.is_active} if g else None,
            "scope": a.scope, "project": {"id": p.id, "name": p.name} if p else None, "role": a.role,
            "role_label": permissions.ROLE_LABELS.get(a.role, a.role),
            "starts_at": a.starts_at, "ends_at": a.ends_at, "timezone": a.timezone,
            "state": access.assignment_state(a), "reason": a.reason, "created_at": a.created_at,
            "created_by": (names or {}).get(a.created_by)}


def _can_manage(db: Session, me: User, scope: str, project_id: int | None) -> str:
    """'admin' or 'pm'; raises if neither."""
    if permissions.has(me.role, "tenant.assignments"):
        return "admin"
    if scope == "project" and project_id and access.has(db, me, project_id, "project.members"):
        return "pm"
    raise HTTPException(403, "You cannot manage access for this scope")


# ---- groups --------------------------------------------------------------------------------------
@router.get("/groups")
def list_groups(db: Session = Depends(get_db), me: User = Depends(member)):
    if not permissions.has(me.role, "tenant.groups"):
        raise HTTPException(403, "You do not have permission for this action")
    return [_g_out(db, g) for g in db.scalars(select(UserGroup).where(UserGroup.tenant_id == me.tenant_id)
                                               .order_by(UserGroup.name)).all()]


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, request: Request, db: Session = Depends(get_db), me: User = Depends(groups_perm)):
    g = UserGroup(tenant_id=me.tenant_id, name=body.name.strip(), description=body.description,
                  is_active=body.is_active, created_by=me.id)
    db.add(g)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A group with this name already exists")
    audit(db, request, me, "group_created", g.name, group_id=g.id)
    db.commit()
    return _g_out(db, g)


@router.put("/groups/{gid}")
def update_group(gid: int, body: GroupIn, request: Request, db: Session = Depends(get_db), me: User = Depends(groups_perm)):
    g = _group(db, me, gid)
    changes = {k: [getattr(g, k), v] for k, v in body.model_dump().items() if getattr(g, k) != v}
    g.name, g.description, g.is_active = body.name.strip(), body.description, body.is_active
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A group with this name already exists")
    audit(db, request, me, "group_updated", g.name, group_id=g.id, changes={k: str(v) for k, v in changes.items()})
    db.commit()
    return _g_out(db, g)


@router.delete("/groups/{gid}")
def delete_group(gid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(groups_perm)):
    g = _group(db, me, gid)
    audit(db, request, me, "group_deleted", g.name, group_id=g.id)
    db.delete(g)  # memberships and the group's assignments cascade
    db.commit()
    return {"ok": True}


@router.post("/groups/{gid}/members")
def add_members(gid: int, body: MembersIn, request: Request, db: Session = Depends(get_db),
                me: User = Depends(groups_perm)):
    g = _group(db, me, gid)
    added = []
    for uid in sorted(set(body.user_ids)):
        u = _user(db, me, uid)  # 404 for another tenant's user (also refused by the composite FK)
        if not db.get(GroupMember, (g.id, u.id)):
            db.add(GroupMember(group_id=g.id, user_id=u.id, tenant_id=me.tenant_id, added_by=me.id))
            added.append(u.username)
    audit(db, request, me, "group_members_added", g.name, group_id=g.id, users=added)
    db.commit()
    return _g_out(db, g)


@router.delete("/groups/{gid}/members/{uid}")
def remove_member(gid: int, uid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(groups_perm)):
    g = _group(db, me, gid)
    m = db.get(GroupMember, (g.id, uid))
    if not m or m.tenant_id != me.tenant_id:
        raise HTTPException(404, "Not a member")
    u = db.get(User, uid)
    db.delete(m)
    audit(db, request, me, "group_member_removed", g.name, group_id=g.id, member=u.username if u else uid)
    db.commit()
    return _g_out(db, g)


# ---- assignments ----------------------------------------------------------------------------------
@router.get("/assignments")
def list_assignments(state: str = "all", project_id: int | None = None, user_id: int | None = None,
                     group_id: int | None = None, q: str = "", db: Session = Depends(get_db), me: User = Depends(member)):
    query = select(ProjectAssignment).where(ProjectAssignment.tenant_id == me.tenant_id)
    if not permissions.has(me.role, "tenant.assignments"):
        # Project managers see the assignments that apply to projects they manage.
        if not project_id or not access.has(db, me, project_id, "project.members"):
            raise HTTPException(403, "You do not have permission for this action")
    if project_id:
        query = query.where(or_(ProjectAssignment.project_id == project_id, ProjectAssignment.scope == "all"))
    if user_id:
        query = query.where(ProjectAssignment.user_id == user_id)
    if group_id:
        query = query.where(ProjectAssignment.group_id == group_id)
    rows = db.scalars(query.order_by(ProjectAssignment.id.desc()).limit(2000)).all()
    names = {u.id: u.username for u in db.scalars(select(User).where(User.tenant_id == me.tenant_id)).all()}
    out = [_a_out(db, a, names) for a in rows]
    if state != "all":
        out = [a for a in out if a["state"] == state]
    if q:
        ql = q.lower()
        out = [a for a in out if ql in " ".join(filter(None, [
            (a["user"] or {}).get("username"), (a["user"] or {}).get("email"), (a["user"] or {}).get("display_name"),
            (a["group"] or {}).get("name"), (a["project"] or {}).get("name"), a["reason"]])).lower()]
    return out


@router.post("/assignments", status_code=201)
def create_assignment(body: AssignmentIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    if (body.user_id is None) == (body.group_id is None):
        raise HTTPException(422, "Choose exactly one user or one group")
    if body.scope == "project" and not body.project_id:
        raise HTTPException(422, "Choose a project")
    if body.scope == "all" and body.project_id:
        raise HTTPException(422, "All-projects access cannot name a project")
    mode = _can_manage(db, me, body.scope, body.project_id)
    if body.scope == "project":
        p = db.get(Project, body.project_id)
        if not p or p.tenant_id != me.tenant_id or p.archived:
            raise HTTPException(404, "Project not found")
    if body.scope == "all" and not body.confirm_all_projects:
        raise HTTPException(409, "All-projects access covers every current and future project of this "
                                 "organization. Confirm to continue.")
    if body.user_id is not None:
        u = _user(db, me, body.user_id)
        if u.id == me.id and mode != "admin":
            raise HTTPException(403, "You cannot change your own access")
        if access.is_admin(u):
            raise HTTPException(422, f"{u.username} is an organization administrator and already has access to every project")
    else:
        _group(db, me, body.group_id)
        if mode != "admin" and _member_of(db, me, body.group_id):
            raise HTTPException(403, "You cannot change access for a group you belong to")
    if mode == "pm" and access.ROLE_RANK[body.role] > access.ROLE_RANK["project_manager"]:
        raise HTTPException(403, "You cannot grant this role")
    s, e = window(body.timezone, body.starts_at, body.ends_at, body.start_date, body.start_time, body.end_date,
                  body.end_time)
    a = ProjectAssignment(tenant_id=me.tenant_id, user_id=body.user_id, group_id=body.group_id, scope=body.scope,
                          project_id=body.project_id if body.scope == "project" else None, role=body.role,
                          starts_at=s, ends_at=e, timezone=body.timezone, reason=body.reason, created_by=me.id)
    db.add(a)
    db.flush()
    audit(db, request, me, "assignment_created", f"assignment {a.id}", assignment_id=a.id, user_id=a.user_id,
          group_id=a.group_id, scope=a.scope, project_id=a.project_id, role=a.role,
          starts_at=str(s) if s else None, ends_at=str(e) if e else None, reason=a.reason)
    db.commit()
    return _a_out(db, a)


def _assignment(db: Session, me: User, aid: int) -> tuple[ProjectAssignment, str]:
    a = db.get(ProjectAssignment, aid)
    if not a or a.tenant_id != me.tenant_id:
        raise HTTPException(404, "Assignment not found")
    try:
        mode = _can_manage(db, me, a.scope, a.project_id)
    except HTTPException:
        raise HTTPException(404, "Assignment not found")
    if mode != "admin" and (a.user_id == me.id or (a.group_id and _member_of(db, me, a.group_id))):
        raise HTTPException(403, "You cannot change your own access")
    return a, mode


@router.patch("/assignments/{aid}")
def update_assignment(aid: int, body: AssignmentPatch, request: Request, db: Session = Depends(get_db),
                      me: User = Depends(member)):
    """Edit role or dates; extending/renewing an expired assignment makes it active again."""
    a, mode = _assignment(db, me, aid)
    if a.status == "revoked":
        raise HTTPException(409, "A revoked assignment cannot be changed; create a new one")
    before = {"role": a.role, "starts_at": str(a.starts_at), "ends_at": str(a.ends_at)}
    if body.role:
        if mode == "pm" and access.ROLE_RANK[body.role] > access.ROLE_RANK["project_manager"]:
            raise HTTPException(403, "You cannot grant this role")
        a.role = body.role
    tzname = body.timezone or a.timezone
    if any(v is not None for v in (body.starts_at, body.start_date)):
        a.starts_at = window(tzname, body.starts_at, None, body.start_date, body.start_time, None, None)[0]
    if body.clear_end:
        a.ends_at = None
    elif any(v is not None for v in (body.ends_at, body.end_date)):
        a.ends_at = window(tzname, None, body.ends_at, None, None, body.end_date, body.end_time)[1]
    if a.starts_at and a.ends_at and a.ends_at <= a.starts_at:
        raise HTTPException(422, "The end must be after the start")
    a.timezone = tzname
    if body.reason is not None:
        a.reason = body.reason
    if a.status == "expired" and (a.ends_at is None or a.ends_at > access.now()):
        a.status = "active"  # renewed
    audit(db, request, me, "assignment_updated", f"assignment {a.id}", assignment_id=a.id, before=before,
          after={"role": a.role, "starts_at": str(a.starts_at), "ends_at": str(a.ends_at)})
    db.commit()
    return _a_out(db, a)


@router.post("/assignments/{aid}/revoke")
def revoke_assignment(aid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    a, _ = _assignment(db, me, aid)
    if a.status != "revoked":
        a.status, a.revoked_at, a.revoked_by = "revoked", access.now(), me.id
        audit(db, request, me, "assignment_revoked", f"assignment {a.id}", assignment_id=a.id, user_id=a.user_id,
              group_id=a.group_id, project_id=a.project_id, scope=a.scope)
        db.commit()
    return _a_out(db, a)


@router.get("/users/{uid}/effective")
def effective_access(uid: int, db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.assignments"))):
    u = _user(db, me, uid)
    projects = db.scalars(select(Project).where(Project.tenant_id == me.tenant_id, Project.archived.is_(False))
                          .order_by(Project.name)).all()
    out = []
    for p in projects:
        perms = access.perms_for(db, u, p.id)
        if "project.read" in perms:
            out.append({"project": {"id": p.id, "name": p.name}, "roles": sorted(access.roles_for(db, u, p.id)),
                        "permissions": sorted(perms)})
    return {"user": {"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active},
            "administrator": access.is_admin(u), "projects": out}


@router.get("/roles")
def roles_matrix(_: User = Depends(member)):
    """Read-only description of the built-in roles (custom roles are not supported in this release)."""
    return {
        "tenant_roles": [{"role": r, "label": permissions.ROLE_LABELS[r], "permissions": permissions.permissions_for(r)}
                         for r in permissions.ROLE_LABELS],
        "project_roles": [{"role": r, "label": permissions.ROLE_LABELS[r],
                           "permissions": sorted(p for p, rs in access.PROJECT_MATRIX.items() if r in rs)}
                          for r in PROJECT_ROLES],
        "administrators": "Tenant Owners and Tenant Administrators have every project permission on every project.",
    }


@router.get("/directory")
def directory(project_id: int | None = None, db: Session = Depends(get_db), me: User = Depends(member)):
    """People and groups to choose from when assigning access (names only, this organization only)."""
    if not permissions.has(me.role, "tenant.assignments"):
        if not project_id or not access.has(db, me, project_id, "project.members"):
            raise HTTPException(403, "You do not have permission for this action")
    users = db.scalars(select(User).where(User.tenant_id == me.tenant_id, User.is_active.is_(True))
                       .order_by(User.username)).all()
    groups = db.scalars(select(UserGroup).where(UserGroup.tenant_id == me.tenant_id).order_by(UserGroup.name)).all()
    projects = db.scalars(select(Project).where(Project.tenant_id == me.tenant_id, Project.archived.is_(False))
                          .order_by(Project.name)).all()
    if not permissions.has(me.role, "tenant.assignments"):
        projects = [p for p in projects if p.id == project_id]
    return {"users": [{"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email,
                       "administrator": access.is_admin(u)} for u in users],
            "groups": [{"id": g.id, "name": g.name, "is_active": g.is_active} for g in groups],
            "projects": [{"id": p.id, "name": p.name} for p in projects]}
