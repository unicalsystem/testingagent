from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ROLES, AllowedTarget, AuditLog, Session_, User
from ..security import hash_password, password_problems
from ..urlguard import normalize_host, valid_hostname
from .deps import audit, require

router = APIRouter(prefix="/admin", tags=["admin"])
admin_only = require("admin")


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._@-]+$")
    display_name: str = Field(default="", max_length=120)
    role: str = Field(pattern="^(admin|tester|viewer)$")
    password: str = Field(min_length=12, max_length=256)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, pattern="^(admin|tester|viewer)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


class TargetIn(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    note: str = Field(default="", max_length=300)


def _u(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role,
            "is_active": u.is_active, "must_change_password": u.must_change_password,
            "last_login_at": u.last_login_at, "created_at": u.created_at}


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(admin_only)):
    return [_u(u) for u in db.scalars(select(User).order_by(User.username)).all()]


@router.post("/users", status_code=201)
def create_user(body: UserIn, request: Request, db: Session = Depends(get_db), me: User = Depends(admin_only)):
    probs = password_problems(body.password)
    if probs:
        raise HTTPException(422, "Password needs " + " and ".join(probs))
    u = User(username=body.username.lower(), display_name=body.display_name, role=body.role,
             password_hash=hash_password(body.password), must_change_password=True)
    db.add(u)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username already exists")
    audit(db, request, me, "user_created", u.username, role=u.role)
    db.commit()
    return _u(u)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, db: Session = Depends(get_db),
                me: User = Depends(admin_only)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == me.id and (body.role not in (None, "admin") or body.is_active is False):
        raise HTTPException(400, "You cannot demote or disable your own account")
    changes = {}
    if body.display_name is not None:
        u.display_name = body.display_name
    if body.role is not None and body.role != u.role:
        changes["role"] = [u.role, body.role]
        u.role = body.role
    if body.is_active is not None and body.is_active != u.is_active:
        changes["is_active"] = body.is_active
        u.is_active = body.is_active
    if body.password:
        probs = password_problems(body.password)
        if probs:
            raise HTTPException(422, "Password needs " + " and ".join(probs))
        u.password_hash = hash_password(body.password)
        u.must_change_password = True
        u.failed_logins, u.locked_until = 0, None
        changes["password_reset"] = True
    if "role" in changes or "is_active" in changes or "password_reset" in changes:
        db.execute(delete(Session_).where(Session_.user_id == u.id))
    audit(db, request, me, "user_updated", u.username, **changes)
    db.commit()
    return _u(u)


@router.get("/targets")
def list_targets(db: Session = Depends(get_db), _: User = Depends(require("viewer"))):
    priv = get_settings().private_targets
    return [{"id": t.id, "host": t.host, "note": t.note, "created_at": t.created_at, "private_access": t.host in priv}
            for t in db.scalars(select(AllowedTarget).order_by(AllowedTarget.host)).all()]


@router.post("/targets", status_code=201)
def add_target(body: TargetIn, request: Request, db: Session = Depends(get_db), me: User = Depends(admin_only)):
    host = normalize_host(body.host)
    is_ipv6 = host.count(":") > 1
    if "://" in body.host or "/" in host or (":" in host and not is_ipv6):
        raise HTTPException(422, "Enter a bare hostname, e.g. app.example.com (no scheme, path or port)")
    if not valid_hostname(host):
        raise HTTPException(422, "Invalid hostname")
    t = AllowedTarget(host=host, note=body.note, created_by=me.id)
    db.add(t)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Host already on the allowlist")
    audit(db, request, me, "target_allowed", host)
    db.commit()
    return {"id": t.id, "host": t.host, "note": t.note}


@router.delete("/targets/{target_id}")
def remove_target(target_id: int, request: Request, db: Session = Depends(get_db), me: User = Depends(admin_only)):
    t = db.get(AllowedTarget, target_id)
    if not t:
        raise HTTPException(404, "Not found")
    audit(db, request, me, "target_removed", t.host)
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.get("/audit")
def audit_log(limit: int = 200, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))).all()
    names = {u.id: u.username for u in db.scalars(select(User)).all()}
    return [{"id": r.id, "at": r.created_at, "user": names.get(r.user_id), "action": r.action, "target": r.target,
             "detail": r.detail, "ip": r.ip} for r in rows]
