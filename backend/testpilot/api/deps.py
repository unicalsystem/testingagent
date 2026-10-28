"""Request dependencies: tenant resolution, authentication, permissions, CSRF, audit.

Tenant context is derived from the URL, never from a client-supplied field:
  /api/t/<slug>/...   -> tenant <slug>
  /api/...            -> the default tenant (release-1.0 compatibility; slug DEFAULT_TENANT_SLUG)
and then checked against the server-side session: a session belongs to exactly one tenant and is
rejected on any other tenant's routes. Every handler then scopes its queries with the tenant id
of the authenticated user, which is guaranteed to equal the URL tenant.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import permissions, settings_store
from ..config import get_settings
from ..db import get_db
from ..models import AuditLog, Session_, Tenant, User
from ..security import hash_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")


def client_ip(request: Request) -> str:
    # nginx sets X-Real-IP; uvicorn runs with --proxy-headers restricted to the proxy network.
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else ""))[:64]


def audit(db: Session, request: Request, user: User | None, action: str, target: str = "", *,
          tenant_id: int | None = None, scope: str = "tenant", **detail) -> None:
    tid = tenant_id if tenant_id is not None else (user.tenant_id if user else None)
    db.add(AuditLog(user_id=user.id if user else None, tenant_id=tid, scope=scope, action=action,
                    target=target[:200], detail=detail, ip=client_ip(request)))


def route_slug(request: Request) -> str:
    slug = request.path_params.get("tenant_slug")
    return slug.lower() if slug else get_settings().default_tenant_slug


def current_tenant(request: Request, db: Session = Depends(get_db)) -> Tenant:
    cached = getattr(request.state, "tenant", None)
    if cached is not None:
        return cached
    slug = route_slug(request)
    t = db.scalars(select(Tenant).where(Tenant.slug == slug)).first() if SLUG_RE.match(slug) else None
    if not t:
        raise HTTPException(404, "Organization not found")
    if t.status != "active":
        raise HTTPException(403, "This organization's access is suspended. Contact Unical Systems support.")
    request.state.tenant = t
    return t


def current_user(request: Request, db: Session = Depends(get_db), tenant: Tenant = Depends(current_tenant)) -> User:
    s = get_settings()
    token = request.cookies.get(s.cookie_name)
    if not token:
        raise HTTPException(401, "Not signed in")
    sess = db.scalars(select(Session_).where(Session_.token_hash == hash_token(token))).first()
    now = datetime.now(timezone.utc)
    idle = settings_store.auth(db)["session_idle_minutes"]
    if not sess or sess.expires_at < now or sess.last_seen_at < now - timedelta(minutes=idle):
        if sess:
            db.delete(sess)
            db.commit()
        raise HTTPException(401, "Session expired")
    if sess.tenant_id != tenant.id:
        # A session is bound to the organization it was created in.
        raise HTTPException(401, "Not signed in to this organization")
    user = db.get(User, sess.user_id)
    if not user or not user.is_active or user.tenant_id != tenant.id:
        raise HTTPException(401, "Account disabled")
    if now - sess.last_seen_at > timedelta(minutes=1):
        sess.last_seen_at = now
        db.commit()
    request.state.session_id = sess.id
    return user


def require_perm(perm: str):
    if perm not in permissions.MATRIX:
        raise KeyError(perm)

    def dep(user: User = Depends(current_user)) -> User:
        if not permissions.has(user.role, perm):
            raise HTTPException(403, "You do not have permission for this action")
        if user.must_change_password and perm != "tenant.member":
            raise HTTPException(403, "Change your password before continuing")
        return user

    return dep


def member(request: Request, user: User = Depends(current_user)) -> User:
    """Any signed-in member of the URL's organization. Project routes then check project permissions
    (access.require) for the specific project. Unsafe methods need a changed password."""
    if user.must_change_password and request.method not in SAFE_METHODS:
        raise HTTPException(403, "Change your password before continuing")
    return user


def require_platform_admin(user: User = Depends(current_user), tenant: Tenant = Depends(current_tenant)) -> User:
    """Unical Super Admin. Only reachable on the default (Unical) tenant's routes."""
    if not user.is_platform_admin or tenant.slug != get_settings().default_tenant_slug:
        raise HTTPException(403, "Unical Super Admin only")
    if user.must_change_password:
        raise HTTPException(403, "Change your password before continuing")
    return user


async def csrf_guard(request: Request) -> None:
    """Defence in depth on top of SameSite=Strict cookies: unsafe methods must carry the
    custom header (which cross-site forms cannot set) and, if present, a trusted Origin."""
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("x-requested-with") != "TestAgent":
        raise HTTPException(403, "Missing CSRF header")
    origin = request.headers.get("origin")
    s = get_settings()
    if origin and origin.rstrip("/") not in s.origins:
        raise HTTPException(403, "Untrusted origin")
