from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Session_, User
from ..security import hash_password, hash_token, new_session_token, password_problems, verify_password
from .deps import audit, client_ip, current_user

router = APIRouter(prefix="/auth", tags=["auth"])
MAX_FAILS = 5
LOCK_MINUTES = 15


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role,
            "must_change_password": u.must_change_password}


def set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(s.cookie_name, token, max_age=s.session_ttl_hours * 3600, httponly=True,
                        secure=s.cookie_secure, samesite="strict", path="/")


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    now = datetime.now(timezone.utc)
    user = db.scalars(select(User).where(User.username == body.username.strip().lower())).first()
    if user and user.locked_until and user.locked_until > now:
        verify_password(body.password, None)  # equalise timing
        raise HTTPException(429, "Too many failed attempts. Try again later.")
    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or not user or not user.is_active:
        if user:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILS:
                user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                user.failed_logins = 0
            audit(db, request, user, "login_failed", user.username)
            db.commit()
        raise HTTPException(401, "Invalid username or password")
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    db.execute(delete(Session_).where(Session_.expires_at < now))
    token = new_session_token()
    db.add(Session_(token_hash=hash_token(token), user_id=user.id, expires_at=now + timedelta(hours=s.session_ttl_hours),
                    ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300]))
    audit(db, request, user, "login", user.username)
    db.commit()
    set_cookie(response, token)
    return user_out(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    token = request.cookies.get(s.cookie_name)
    if token:
        db.execute(delete(Session_).where(Session_.token_hash == hash_token(token)))
        db.commit()
    response.delete_cookie(s.cookie_name, path="/", secure=s.cookie_secure, httponly=True, samesite="strict")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.post("/password")
def change_password(body: PasswordIn, request: Request, response: Response,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    probs = password_problems(body.new_password)
    if probs:
        raise HTTPException(422, "Password needs " + " and ".join(probs))
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    # Invalidate every session, then issue a fresh one for this browser.
    db.execute(delete(Session_).where(Session_.user_id == user.id))
    s = get_settings()
    token = new_session_token()
    db.add(Session_(token_hash=hash_token(token), user_id=user.id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=s.session_ttl_hours),
                    ip=client_ip(request)))
    audit(db, request, user, "password_changed", user.username)
    db.commit()
    set_cookie(response, token)
    return user_out(user)
