from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AuditLog, Session_, User
from ..security import hash_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"viewer": 0, "tester": 1, "admin": 2}


def client_ip(request: Request) -> str:
    # nginx sets X-Real-IP; uvicorn runs with --proxy-headers restricted to the proxy network.
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else ""))[:64]


def audit(db: Session, request: Request, user: User | None, action: str, target: str = "", **detail) -> None:
    db.add(AuditLog(user_id=user.id if user else None, action=action, target=target[:200], detail=detail,
                    ip=client_ip(request)))


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    s = get_settings()
    token = request.cookies.get(s.cookie_name)
    if not token:
        raise HTTPException(401, "Not signed in")
    sess = db.scalars(select(Session_).where(Session_.token_hash == hash_token(token))).first()
    now = datetime.now(timezone.utc)
    if not sess or sess.expires_at < now or sess.last_seen_at < now - timedelta(minutes=s.session_idle_minutes):
        if sess:
            db.delete(sess)
            db.commit()
        raise HTTPException(401, "Session expired")
    user = db.get(User, sess.user_id)
    if not user or not user.is_active:
        raise HTTPException(401, "Account disabled")
    if now - sess.last_seen_at > timedelta(minutes=1):
        sess.last_seen_at = now
        db.commit()
    request.state.session_id = sess.id
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[role]:
            raise HTTPException(403, f"Requires {role} role")
        if user.must_change_password and role != "viewer":
            raise HTTPException(403, "Change your password before continuing")
        return user

    return dep


async def csrf_guard(request: Request) -> None:
    """Defence in depth on top of SameSite=Strict cookies: unsafe methods must carry the
    custom header (which cross-site forms cannot set) and, if present, a trusted Origin."""
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("x-requested-with") != "TestPilot":
        raise HTTPException(403, "Missing CSRF header")
    origin = request.headers.get("origin")
    s = get_settings()
    if origin and origin.rstrip("/") not in s.origins:
        raise HTTPException(403, "Untrusted origin")
