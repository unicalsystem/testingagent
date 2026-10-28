"""Unauthenticated endpoints: tenant login branding, organization registration, invitation activation.

Nothing here reveals anything about other tenants or registrations beyond what a tenant's own public
login page shows (its display name, logo, colours, welcome text and support contacts).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import captcha, countries, mailer, permissions, privacy, settings_store, tenancy
from ..config import get_settings
from ..db import get_db
from ..models import Session_, Tenant, TenantBranding, TenantRegistration, User
from ..security import hash_password, hash_token, new_session_token, password_problems
from .deps import audit, client_ip
from .routes_admin import EMAIL_RE, branding_out
from .routes_auth import issue_session, user_out

router = APIRouter(prefix="/public", tags=["public"])


class RegistrationIn(BaseModel):
    org_name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=3, max_length=40)
    contact_name: str = Field(min_length=2, max_length=120)
    contact_email: str = Field(min_length=5, max_length=254)
    contact_phone: str = Field(default="", max_length=40)
    country: str = Field(default="", max_length=80)
    country_code: str = Field(default="", max_length=2)
    message: str = Field(default="", max_length=3000)
    accept_terms: bool = False
    accept_privacy: bool = False   # privacy notice read; consent to process the contact details
    confirm_adult: bool = False    # the contact person is 18 or older (DPDP Act: no child data)
    captcha_token: str | None = Field(default=None, max_length=4000)


class AcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._@-]+$")
    display_name: str = Field(default="", max_length=120)
    password: str = Field(min_length=12, max_length=256)


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@router.get("/tenants/{slug}/branding")
def tenant_branding(slug: str, db: Session = Depends(get_db)):
    s = tenancy.normalize_slug(slug)
    t = db.scalars(select(Tenant).where(Tenant.slug == s)).first() if tenancy.SLUG_RE.match(s) else None
    # Pending, rejected and suspended organizations are indistinguishable from unknown ones.
    if not t or t.status != "active":
        raise HTTPException(404, "Organization not found")
    return branding_out(t, db.get(TenantBranding, t.id))


@router.get("/slug-check")
def slug_check(slug: str, db: Session = Depends(get_db)):
    if not get_settings().registration_enabled:
        raise HTTPException(403, "Registration is closed")
    p = tenancy.slug_problem(slug, db)
    return {"slug": tenancy.normalize_slug(slug), "available": p is None, "problem": p}


@router.post("/registrations", status_code=202)
def register(body: RegistrationIn, request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    if not s.registration_enabled:
        raise HTTPException(403, "Registration is closed")
    if not body.accept_terms:
        raise HTTPException(422, "Please accept the Terms and Conditions")
    if not body.accept_privacy:
        raise HTTPException(422, "Please read the Privacy Policy and give your consent")
    if not body.confirm_adult:
        raise HTTPException(422, "Registration is only for people aged 18 or over acting for an organization")
    code = (body.country_code or "").upper()
    if code not in countries.CODES:
        raise HTTPException(422, "Select your country")
    captcha.verify(db, request, "register", body.captcha_token,
                   audit=lambda action, target, **d: audit(db, request, None, action, target, tenant_id=None,
                                                          scope="platform", **d))
    email = body.contact_email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid work email address")
    ip = client_ip(request)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    recent = db.scalar(select(func.count()).select_from(TenantRegistration)
                       .where(TenantRegistration.ip == ip, TenantRegistration.created_at >= since)) or 0
    if ip and recent >= s.registrations_per_ip_per_day:
        raise HTTPException(429, "Too many registrations from this network today. Try again tomorrow.")
    slug = tenancy.normalize_slug(body.slug)
    problem = tenancy.slug_problem(slug, db)
    if problem:
        raise HTTPException(409, f"Organization address: {problem}")
    ack = {"status": "received",
           "message": "Thank you. Unical Systems will review your registration and email the contact address "
                      "once it is approved."}
    # A second pending request from the same email is acknowledged identically but not stored,
    # so the endpoint does not reveal whether an email has registered before.
    if db.scalars(select(TenantRegistration.id).where(TenantRegistration.contact_email == email,
                                                      TenantRegistration.status == "pending")).first():
        return ack
    reg = TenantRegistration(org_name=body.org_name.strip(), requested_slug=slug, contact_name=body.contact_name.strip(),
                             contact_email=email, contact_phone=body.contact_phone.strip(), country=body.country.strip()[:80] or code,
                             country_code=code, message=body.message.strip(), ip=ip)
    db.add(reg)
    db.flush()
    privacy.record_acceptance(db, ip=ip, user_agent=request.headers.get("user-agent", ""), registration_id=reg.id)
    audit(db, request, None, "platform_registration_received", body.org_name[:200], tenant_id=None, scope="platform",
          registration_id=reg.id, slug=slug)
    db.commit()
    return ack


@router.post("/invitations/inspect")
def inspect_invitation(body: TokenIn, db: Session = Depends(get_db)):
    """Token goes in the body (from the URL fragment), never in a URL that could be logged."""
    inv = tenancy.find_invitation(db, body.token)
    if not inv:
        raise HTTPException(404, "This activation link is invalid, already used or expired")
    t = db.get(Tenant, inv.tenant_id)
    return {"tenant": {"slug": t.slug, "name": t.name}, "email": inv.email, "role": inv.role,
            "role_label": permissions.ROLE_LABELS.get(inv.role, inv.role), "expires_at": inv.expires_at}


@router.post("/invitations/accept")
def accept_invitation(body: AcceptIn, request: Request, response: Response, db: Session = Depends(get_db)):
    inv = tenancy.find_invitation(db, body.token)
    if not inv:
        raise HTTPException(404, "This activation link is invalid, already used or expired")
    probs = password_problems(body.password)
    if probs:
        raise HTTPException(422, "Password needs " + " and ".join(probs))
    t = db.get(Tenant, inv.tenant_id)
    try:
        u = tenancy.user_from_invitation(db, inv, body.username, body.display_name, hash_password(body.password))
    except LookupError:
        db.rollback()
        raise HTTPException(404, "This activation link is invalid, already used or expired")
    except ValueError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    now = datetime.now(timezone.utc)
    issue_session(db, request, response, u, t, "invite")
    audit(db, request, u, "invitation_accepted", u.username, invitation_id=inv.id, role=u.role)
    db.commit()
    return user_out(u, t, db)


# ---- sign-in page configuration ---------------------------------------------------------------
def _active_tenant(db: Session, slug: str) -> Tenant:
    s = tenancy.normalize_slug(slug)
    t = db.scalars(select(Tenant).where(Tenant.slug == s)).first() if tenancy.SLUG_RE.match(s) else None
    if not t or t.status != "active":
        raise HTTPException(404, "Organization not found")
    return t


@router.get("/tenants/{slug}/auth-config")
def auth_config(slug: str, request: Request, db: Session = Depends(get_db)):
    """What the sign-in page needs: enabled methods and whether this browser must solve a CAPTCHA.
    Contains no secrets. The server re-checks everything on submit."""
    t = _active_tenant(db, slug)
    m = settings_store.methods(db, t)
    a = settings_store.auth(db)
    from ..models import IdentityProvider
    idp = db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id == t.id,
                                                    IdentityProvider.provider == "microsoft")).first()
    ip = client_ip(request)
    return {
        "password": m["password"],
        "microsoft": bool(m["microsoft"] and idp and idp.status == "active" and idp.client_secret_enc),
        "login_identifier": a["login_identifier"],
        "password_reset": bool(m["password"] and a["password_reset_enabled"] and mailer.configured(db)),
        "captcha": {action: captcha.client_config(db, action, ip, t) for action in ("login", "password_reset")},
    }


@router.get("/registration-config")
def registration_config(request: Request, db: Session = Depends(get_db)):
    return {"enabled": get_settings().registration_enabled,
            "captcha": captcha.client_config(db, "register", client_ip(request))}


# ---- password reset (requires SMTP) -------------------------------------------------------------
class ResetRequestIn(BaseModel):
    tenant: str = Field(min_length=3, max_length=40)
    identifier: str = Field(min_length=1, max_length=254)
    captcha_token: str | None = Field(default=None, max_length=4000)


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=12, max_length=256)


RESET_ACK = {"status": "sent", "message": "If an account matches, a reset link has been e-mailed. It expires shortly."}


@router.post("/password-reset/request")
def reset_request(body: ResetRequestIn, request: Request, db: Session = Depends(get_db)):
    import hashlib
    import secrets as _secrets

    from ..models import PasswordReset
    from .routes_auth import _find_user

    t = _active_tenant(db, body.tenant)
    a = settings_store.auth(db)
    if not (a["password_reset_enabled"] and settings_store.methods(db, t)["password"] and mailer.configured(db)):
        raise HTTPException(403, "Password reset is not available. Contact your administrator.")
    captcha.verify(db, request, "password_reset", body.captcha_token, t,
                   audit=lambda action, target, **d: audit(db, request, None, action, target, tenant_id=t.id, **d))
    db.commit()
    ip = client_ip(request)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    if (db.scalar(select(func.count()).select_from(PasswordReset).where(PasswordReset.ip == ip,
                                                                        PasswordReset.created_at >= since)) or 0) >= 10:
        return RESET_ACK  # quietly rate-limited; same answer, no enumeration
    u = _find_user(db, t, body.identifier, "username_or_email")
    # Same response whether or not the account exists (no enumeration); only accounts with an e-mail.
    if u and u.is_active and u.email:
        token = _secrets.token_urlsafe(32)
        db.add(PasswordReset(tenant_id=t.id, user_id=u.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                             expires_at=datetime.now(timezone.utc) + timedelta(minutes=int(a["password_reset_ttl_minutes"])),
                             ip=ip))
        audit(db, request, u, "password_reset_requested", u.username)
        db.commit()
        link = f"{get_settings().public_url}/{t.slug}/reset-password#{token}"
        mailer.try_send(db, u.email, "Reset your TestAgent password",
                        f"Hello {u.display_name or u.username},\n\nA password reset was requested for your account in "
                        f"{t.name}. Open this link within {a['password_reset_ttl_minutes']} minutes to choose a new "
                        f"password:\n\n{link}\n\nIf you did not ask for this, ignore this e-mail; your password stays "
                        f"unchanged.\n\nTestAgent · Unical Systems")
    else:
        db.commit()
    return RESET_ACK


@router.post("/password-reset/confirm")
def reset_confirm(body: ResetConfirmIn, request: Request, db: Session = Depends(get_db)):
    import hashlib

    from sqlalchemy import update as _update

    from ..models import PasswordReset

    probs = password_problems(body.new_password)
    if probs:
        raise HTTPException(422, "Password needs " + " and ".join(probs))
    now = datetime.now(timezone.utc)
    th = hashlib.sha256(body.token.encode()).hexdigest()
    pr = db.scalars(select(PasswordReset).where(PasswordReset.token_hash == th)).first()
    if not pr or pr.used_at or pr.expires_at < now:
        raise HTTPException(404, "This reset link is invalid, already used or expired")
    claimed = db.execute(_update(PasswordReset).where(PasswordReset.id == pr.id, PasswordReset.used_at.is_(None))
                         .values(used_at=now)).rowcount
    u = db.get(User, pr.user_id)
    t = db.get(Tenant, pr.tenant_id)
    if claimed != 1 or not u or not u.is_active or not t or t.status != "active" \
            or not settings_store.methods(db, t)["password"]:
        db.rollback()
        raise HTTPException(404, "This reset link is invalid, already used or expired")
    u.password_hash = hash_password(body.new_password)
    u.must_change_password = False
    u.failed_logins, u.locked_until = 0, None
    db.execute(delete(Session_).where(Session_.user_id == u.id))  # sign out everywhere
    # other outstanding reset links for this user stop working too
    db.execute(_update(PasswordReset).where(PasswordReset.user_id == u.id, PasswordReset.used_at.is_(None))
               .values(used_at=now))
    audit(db, request, u, "password_reset_completed", u.username)
    db.commit()
    return {"ok": True, "tenant": t.slug}
