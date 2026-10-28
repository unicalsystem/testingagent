from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import captcha, permissions, privacy, settings_store
from ..config import get_settings
from ..db import get_db
from ..models import ExternalIdentity, Session_, Tenant, User
from ..security import hash_password, hash_token, new_session_token, password_problems, verify_password
from .deps import audit, client_ip, current_tenant, current_user

router = APIRouter(prefix="/auth", tags=["auth"])
FAIL = "Invalid username or password. After repeated failures the account is locked for a while."


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=254)  # username, or e-mail when enabled
    password: str = Field(min_length=1, max_length=256)
    captcha_token: str | None = Field(default=None, max_length=4000)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def user_out(u: User, tenant: Tenant | None = None, db: Session | None = None) -> dict:
    out = {"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email, "role": u.role,
           "role_label": permissions.ROLE_LABELS.get(u.role, u.role),
           "legacy_role": permissions.legacy_role(u.role),
           "permissions": permissions.permissions_for(u.role),
           "is_platform_admin": u.is_platform_admin,
           "must_change_password": u.must_change_password}
    if tenant is not None:
        out["tenant"] = {"id": tenant.id, "slug": tenant.slug, "name": tenant.name}
    if db is not None:
        out["microsoft_linked"] = bool(db.scalars(select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == u.id, ExternalIdentity.provider == "microsoft")).first())
        if tenant is not None:
            out["sign_in_methods"] = settings_store.methods(db, tenant)
        out["legal_pending"] = privacy.needs_acceptance(db, u)
        if tenant is not None:
            from .. import billing
            e = billing.entitlements(db, tenant)
            out["licence"] = {"state": e["state"], "plan": e["plan"], "features": e["features"], "read_only": e["read_only"],
                              "ends_at": e["ends_at"].isoformat() if e["ends_at"] else None, "days_left": e["days_left"],
                              "exempt": e["exempt"]}
    return out


def set_cookie(response: Response, token: str, ttl_hours: int) -> None:
    s = get_settings()
    response.set_cookie(s.cookie_name, token, max_age=int(ttl_hours * 3600), httponly=True,
                        secure=s.cookie_secure, samesite="strict", path="/")


def issue_session(db: Session, request: Request, response: Response | None, user: User, tenant: Tenant,
                  method: str) -> str:
    """Creates a server-side session bound to the user's organization; sets the cookie if a response is given."""
    ttl = settings_store.auth(db)["session_ttl_hours"]
    now = datetime.now(timezone.utc)
    db.execute(delete(Session_).where(Session_.expires_at < now))
    token = new_session_token()
    db.add(Session_(token_hash=hash_token(token), user_id=user.id, tenant_id=tenant.id, auth_method=method,
                    expires_at=now + timedelta(hours=ttl), ip=client_ip(request),
                    user_agent=(request.headers.get("user-agent") or "")[:300]))
    user.last_login_at = now
    if response is not None:
        set_cookie(response, token, ttl)
    return token


def _find_user(db: Session, tenant: Tenant, ident: str, mode: str) -> User | None:
    ident = ident.strip().lower()
    u = db.scalars(select(User).where(User.tenant_id == tenant.id, User.username == ident)).first()
    if not u and mode == "username_or_email" and "@" in ident:
        u = db.scalars(select(User).where(User.tenant_id == tenant.id, func.lower(User.email) == ident)).first()
    return u


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db),
          tenant: Tenant = Depends(current_tenant)):
    a = settings_store.auth(db)
    if not settings_store.methods(db, tenant)["password"]:
        raise HTTPException(403, "Password sign-in is disabled for this organization. Use Microsoft sign-in.")
    captcha.verify(db, request, "login", body.captcha_token, tenant,
                   audit=lambda action, target, **d: audit(db, request, None, action, target, tenant_id=tenant.id, **d))
    db.commit()  # keep the CAPTCHA replay record / outage audit even if the sign-in fails
    now = datetime.now(timezone.utc)
    # Accounts are looked up only inside the URL's organization.
    user = _find_user(db, tenant, body.username, a["login_identifier"])
    if user and user.locked_until and user.locked_until > now:
        # Same answer as a wrong password, so a lockout does not reveal that the account exists.
        verify_password(body.password, None)  # equalise timing
        audit(db, request, user, "login_locked", user.username)
        db.commit()
        raise HTTPException(401, FAIL)
    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or not user or not user.is_active or not user.password_hash:
        if user:
            user.failed_logins += 1
            if user.failed_logins >= int(a["max_failed_logins"]):
                user.locked_until = now + timedelta(minutes=int(a["lockout_minutes"]))
                user.failed_logins = 0
            audit(db, request, user, "login_failed", user.username)
            db.commit()
        raise HTTPException(401, FAIL)
    user.failed_logins, user.locked_until = 0, None
    issue_session(db, request, response, user, tenant, "password")
    audit(db, request, user, "login", user.username)
    db.commit()
    return user_out(user, tenant, db)


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
def me(user: User = Depends(current_user), tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)):
    return user_out(user, tenant, db)


@router.post("/password")
def change_password(body: PasswordIn, request: Request, response: Response,
                    user: User = Depends(current_user), db: Session = Depends(get_db),
                    tenant: Tenant = Depends(current_tenant)):
    if not verify_password(body.current_password, user.password_hash or None):
        raise HTTPException(400, "Current password is incorrect")
    probs = password_problems(body.new_password)
    if probs:
        raise HTTPException(422, "Password needs " + " and ".join(probs))
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    # Invalidate every session, then issue a fresh one for this browser.
    db.execute(delete(Session_).where(Session_.user_id == user.id))
    issue_session(db, request, response, user, tenant, "password")
    audit(db, request, user, "password_changed", user.username)
    db.commit()
    return user_out(user, tenant, db)


@router.delete("/microsoft-link")
def unlink_microsoft(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db),
                     tenant: Tenant = Depends(current_tenant)):
    if not settings_store.methods(db, tenant)["password"] or not user.password_hash:
        raise HTTPException(409, "Microsoft is your only sign-in method here; it cannot be unlinked")
    n = db.execute(delete(ExternalIdentity).where(ExternalIdentity.user_id == user.id,
                                                  ExternalIdentity.provider == "microsoft")).rowcount
    db.execute(delete(Session_).where(Session_.user_id == user.id, Session_.auth_method == "microsoft"))
    audit(db, request, user, "microsoft_unlinked", user.username)
    db.commit()
    return {"ok": bool(n)}
