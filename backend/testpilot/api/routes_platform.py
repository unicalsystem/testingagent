"""Unical Super Admin: registrations and tenant lifecycle.

These endpoints expose organization metadata only (name, slug, status, counts). They deliberately
offer no way to read a tenant's projects, test cases, runs, evidence, reports or secrets.
Reachable only at /api/platform/... with a session of the default (Unical) tenant.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import mailer, settings_store, tenancy
from ..db import get_db
from ..models import AuditLog, Project, Run, Session_, Tenant, TenantRegistration, User
from .deps import audit, require_platform_admin

router = APIRouter(prefix="/platform", tags=["platform"])


class ApproveIn(BaseModel):
    slug: str | None = Field(default=None, max_length=40)  # override the requested slug
    org_name: str | None = Field(default=None, max_length=160)
    note: str = Field(default="", max_length=1000)


class DecisionIn(BaseModel):
    note: str = Field(default="", max_length=1000)


class StatusIn(BaseModel):
    reason: str = Field(default="", max_length=500)


def _reg(r: TenantRegistration) -> dict:
    return {"id": r.id, "org_name": r.org_name, "requested_slug": r.requested_slug, "contact_name": r.contact_name,
            "contact_email": r.contact_email, "contact_phone": r.contact_phone, "country": r.country,
            "message": r.message, "status": r.status, "decision_note": r.decision_note, "decided_at": r.decided_at,
            "created_at": r.created_at}


def _tenant(db: Session, t: Tenant) -> dict:
    users = db.scalar(select(func.count()).select_from(User).where(User.tenant_id == t.id)) or 0
    projects = db.scalar(select(func.count()).select_from(Project).where(Project.tenant_id == t.id)) or 0
    runs = db.scalar(select(func.count()).select_from(Run).where(Run.tenant_id == t.id)) or 0
    return {"id": t.id, "slug": t.slug, "name": t.name, "status": t.status, "status_reason": t.status_reason,
            "created_at": t.created_at, "usage": {"users": users, "projects": projects, "runs": runs}}


@router.get("/registrations")
def list_registrations(status: str = "pending", db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    q = select(TenantRegistration).order_by(TenantRegistration.id.desc()).limit(500)
    if status != "all":
        q = q.where(TenantRegistration.status == status)
    return [_reg(r) for r in db.scalars(q).all()]


def _pending(db: Session, rid: int) -> TenantRegistration:
    r = db.scalars(select(TenantRegistration).where(TenantRegistration.id == rid).with_for_update()).first()
    if not r:
        raise HTTPException(404, "Registration not found")
    if r.status != "pending":
        raise HTTPException(409, f"Registration already {r.status}")
    return r


@router.post("/registrations/{rid}/approve")
def approve(rid: int, body: ApproveIn, request: Request, db: Session = Depends(get_db),
            me: User = Depends(require_platform_admin)):
    r = _pending(db, rid)
    slug = tenancy.normalize_slug(body.slug or r.requested_slug)
    # The registration's own pending slug must not block itself.
    problem = tenancy.slug_problem(slug)
    if not problem and db.scalars(select(Tenant.id).where(Tenant.slug == slug)).first():
        problem = "This address is not available"
    if not problem and db.scalars(select(TenantRegistration.id).where(
            TenantRegistration.requested_slug == slug, TenantRegistration.status == "pending",
            TenantRegistration.id != r.id)).first():
        problem = "Another pending registration requested this address"
    if problem:
        raise HTTPException(409, f"Slug '{slug}': {problem}")
    name = (body.org_name or r.org_name).strip()
    t = tenancy.provision(db, r, slug, name)
    inv, token = tenancy.create_invitation(db, t, r.contact_email, "tenant_owner", me.id)
    r.status, r.decision_note, r.decided_by, r.decided_at = "approved", body.note, me.id, datetime.now(timezone.utc)
    audit(db, request, me, "platform_registration_approved", name, tenant_id=t.id, scope="platform",
          registration_id=r.id, slug=slug)
    db.commit()
    link = tenancy.invitation_link(t, token)
    emailed = mailer.try_send(db, inv.email, f"Your TestAgent organization {name} is approved",
                              f"Hello {r.contact_name},\n\nYour organization {name} has been approved on TestAgent.\n"
                              f"Activate the owner account within 72 hours with this one-time link:\n\n{link}\n\n"
                              f"Your sign-in address: {tenancy.get_settings().public_url}/{t.slug}/\n\nUnical Systems")
    return {"registration": _reg(r), "tenant": _tenant(db, t),
            "owner_invitation": {"email": inv.email, "expires_at": inv.expires_at, "link": link, "emailed": emailed}}


@router.post("/registrations/{rid}/reject")
def reject(rid: int, body: DecisionIn, request: Request, db: Session = Depends(get_db),
           me: User = Depends(require_platform_admin)):
    r = _pending(db, rid)
    r.status, r.decision_note, r.decided_by, r.decided_at = "rejected", body.note, me.id, datetime.now(timezone.utc)
    audit(db, request, me, "platform_registration_rejected", r.org_name, tenant_id=None, scope="platform",
          registration_id=r.id, slug=r.requested_slug)
    db.commit()
    return _reg(r)


@router.get("/tenants")
def list_tenants(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    return [_tenant(db, t) for t in db.scalars(select(Tenant).order_by(Tenant.name)).all()]


@router.post("/tenants/{tid}/owner-invitation")
def reissue_owner_invite(tid: int, request: Request, db: Session = Depends(get_db),
                         me: User = Depends(require_platform_admin)):
    """New owner activation link (e.g. the first one expired). Revokes earlier unused owner links."""
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "Organization not found")
    if t.status != "active":
        raise HTTPException(409, "Reactivate the organization first")
    if db.scalars(select(User.id).where(User.tenant_id == t.id, User.role == "tenant_owner")).first():
        # Once an owner exists, access is managed by the organization itself, never by Unical.
        raise HTTPException(409, "This organization already has an owner. Its owner manages further access.")
    reg = db.get(TenantRegistration, t.registration_id) if t.registration_id else None
    if not reg:
        raise HTTPException(409, "This organization has no registration contact")
    from ..models import Invitation
    for old in db.scalars(select(Invitation).where(Invitation.tenant_id == t.id, Invitation.role == "tenant_owner",
                                                   Invitation.used_at.is_(None), Invitation.revoked.is_(False))).all():
        old.revoked = True
    inv, token = tenancy.create_invitation(db, t, reg.contact_email, "tenant_owner", me.id)
    audit(db, request, me, "platform_owner_invitation_reissued", t.slug, tenant_id=t.id, scope="platform")
    db.commit()
    return {"email": inv.email, "expires_at": inv.expires_at, "link": tenancy.invitation_link(t, token)}


def _set_status(db: Session, request: Request, me: User, tid: int, status: str, reason: str) -> dict:
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "Organization not found")
    if t.id == me.tenant_id:
        raise HTTPException(400, "The Unical platform organization cannot be suspended")
    if t.status == status:
        raise HTTPException(409, f"Organization already {status}")
    t.status, t.status_reason = status, reason
    if status == "suspended":
        db.execute(delete(Session_).where(Session_.tenant_id == t.id))  # sign everyone out
    audit(db, request, me, "platform_tenant_suspended" if status == "suspended" else "platform_tenant_reactivated",
          t.slug, tenant_id=t.id, scope="platform", reason=reason)
    db.commit()
    return _tenant(db, t)


@router.post("/tenants/{tid}/suspend")
def suspend(tid: int, body: StatusIn, request: Request, db: Session = Depends(get_db),
            me: User = Depends(require_platform_admin)):
    return _set_status(db, request, me, tid, "suspended", body.reason)


@router.post("/tenants/{tid}/reactivate")
def reactivate(tid: int, body: StatusIn, request: Request, db: Session = Depends(get_db),
               me: User = Depends(require_platform_admin)):
    return _set_status(db, request, me, tid, "active", body.reason)


@router.get("/audit")
def platform_audit(limit: int = 200, db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    rows = db.scalars(select(AuditLog).where(AuditLog.scope == "platform").order_by(AuditLog.id.desc())
                      .limit(max(1, min(limit, 1000)))).all()
    names = {u.id: u.username for u in db.scalars(select(User).where(User.is_platform_admin.is_(True))).all()}
    return [{"id": r.id, "at": r.created_at, "user": names.get(r.user_id), "action": r.action, "target": r.target,
             "tenant_id": r.tenant_id, "detail": r.detail, "ip": r.ip} for r in rows]


# ---- Platform Settings: Authentication & Security --------------------------------------------
class AuthSettingsIn(BaseModel):
    platform_password_enabled: bool
    platform_microsoft_enabled: bool
    tenant_password_allowed: bool
    tenant_microsoft_allowed: bool
    login_identifier: str = Field(pattern="^(username|username_or_email)$")
    max_failed_logins: int = Field(ge=3, le=50)
    lockout_minutes: int = Field(ge=1, le=1440)
    session_ttl_hours: int = Field(ge=1, le=720)
    session_idle_minutes: int = Field(ge=5, le=10080)
    password_reset_enabled: bool
    password_reset_ttl_minutes: int = Field(ge=5, le=1440)
    confirm: bool = False


class RecaptchaIn(BaseModel):
    enabled: bool
    site_key: str = Field(default="", max_length=200)
    secret: str | None = Field(default=None, max_length=200)  # omit to keep; never returned
    min_score: float = Field(ge=0.0, le=1.0)
    actions: list[str] = Field(default_factory=list)
    unavailable_policy: str = Field(pattern="^(deny|allow_and_audit)$")


class TrustedIpsIn(BaseModel):
    cidrs: list[str] = Field(default_factory=list, max_length=200)
    note: str = Field(default="", max_length=500)


class SmtpIn(BaseModel):
    enabled: bool
    host: str = Field(default="", max_length=253)
    port: int = Field(ge=1, le=65535)
    security: str = Field(pattern="^(starttls|ssl|none)$")
    username: str = Field(default="", max_length=254)
    password: str | None = Field(default=None, max_length=500)
    from_address: str = Field(default="", max_length=254)
    from_name: str = Field(default="TestAgent", max_length=120)


class SmtpTestIn(BaseModel):
    to: str = Field(min_length=3, max_length=254)


def _settings_view(db: Session) -> dict:
    from .. import mailer, oidc

    return {
        "auth": settings_store.public_view(settings_store.auth(db)),
        "recaptcha": settings_store.public_view(settings_store.get(db, "recaptcha")),
        "trusted_ips": settings_store.get(db, "trusted_ips"),
        "smtp": settings_store.public_view(settings_store.get(db, "smtp")),
        "smtp_ready": mailer.configured(db),
        "captcha_actions": list(settings_store.CAPTCHA_ACTIONS),
        "microsoft_redirect_uri": oidc.redirect_uri(),
    }


@router.get("/settings")
def get_platform_settings(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    return _settings_view(db)


@router.put("/settings/auth")
def put_auth(body: AuthSettingsIn, request: Request, db: Session = Depends(get_db),
             me: User = Depends(require_platform_admin)):
    from ..models import IdentityProvider, Session_

    from .. import mailer
    if not body.platform_password_enabled and not body.platform_microsoft_enabled:
        raise HTTPException(409, "At least one sign-in method must stay enabled for the Unical organization")
    t = db.get(Tenant, me.tenant_id)
    idp = db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id == t.id,
                                                    IdentityProvider.provider == "microsoft")).first()
    if body.platform_microsoft_enabled and not (idp and idp.status == "active"):
        raise HTTPException(409, "Configure and successfully test Unical's Microsoft connection first")
    if not body.platform_password_enabled and not body.confirm:
        raise HTTPException(409, "Unical users will only be able to sign in with Microsoft. Make sure your own "
                                 "account is linked. Recovery: `python -m testpilot.cli auth-recovery` on the "
                                 "server. Confirm to continue.")
    if body.password_reset_enabled and not mailer.configured(db):
        raise HTTPException(409, "Configure and enable SMTP before enabling password reset")
    old = settings_store.auth(db)
    new = settings_store.put(db, "auth", body.model_dump(exclude={"confirm"}), me.id)
    revoked = 0
    if old["platform_microsoft_enabled"] and not new["platform_microsoft_enabled"]:
        revoked += db.execute(delete(Session_).where(Session_.tenant_id == t.id, Session_.auth_method == "microsoft")).rowcount
    if old["tenant_microsoft_allowed"] and not new["tenant_microsoft_allowed"]:
        revoked += db.execute(delete(Session_).where(Session_.tenant_id != t.id, Session_.auth_method == "microsoft")).rowcount
    pw = ("password", "invite")
    if old["platform_password_enabled"] and not new["platform_password_enabled"]:
        revoked += db.execute(delete(Session_).where(Session_.tenant_id == t.id, Session_.auth_method.in_(pw))).rowcount
    if old["tenant_password_allowed"] and not new["tenant_password_allowed"]:
        revoked += db.execute(delete(Session_).where(Session_.tenant_id != t.id, Session_.auth_method.in_(pw))).rowcount
    changes = {k: [old.get(k), v] for k, v in body.model_dump(exclude={"confirm"}).items() if old.get(k) != v}
    audit(db, request, me, "platform_auth_settings_updated", "auth", tenant_id=t.id, scope="platform",
          changes={k: [str(x) for x in v] for k, v in changes.items()}, microsoft_sessions_revoked=revoked)
    db.commit()
    return _settings_view(db)


@router.put("/settings/recaptcha")
def put_recaptcha(body: RecaptchaIn, request: Request, db: Session = Depends(get_db),
                  me: User = Depends(require_platform_admin)):
    from .. import captcha
    from ..security import SecretsUnavailable

    actions = sorted(set(body.actions))
    if any(a not in settings_store.CAPTCHA_ACTIONS for a in actions):
        raise HTTPException(422, "Unknown protected action")
    cur = settings_store.get(db, "recaptcha")
    upd = {"enabled": body.enabled, "site_key": body.site_key.strip(), "min_score": round(body.min_score, 2),
           "actions": actions, "unavailable_policy": body.unavailable_policy}
    rotated = False
    if body.secret:
        try:
            upd["secret_enc"], upd["secret_hint"] = settings_store.seal(body.secret.strip())
        except SecretsUnavailable as e:
            raise HTTPException(503, str(e))
        upd.update(last_test_at=None, last_test_ok=None, last_test_message="")
        rotated = True
    if body.enabled:
        if not upd["site_key"] or not (body.secret or cur["secret_enc"]):
            raise HTTPException(422, "Enter the site key and secret key before enabling reCAPTCHA")
        if not actions:
            raise HTTPException(422, "Choose at least one page to protect")
        secret = body.secret.strip() if body.secret else settings_store.unseal(cur["secret_enc"])
        ok, msg = captcha.test_secret(secret)
        upd.update(last_test_at=datetime.now(timezone.utc).isoformat(), last_test_ok=ok, last_test_message=msg)
        if not ok:
            db.rollback()
            raise HTTPException(409, f"reCAPTCHA not enabled: {msg}")
    settings_store.put(db, "recaptcha", upd, me.id)
    audit(db, request, me, "platform_recaptcha_updated", "recaptcha", tenant_id=me.tenant_id, scope="platform",
          enabled=body.enabled, actions=actions, min_score=upd["min_score"], secret_rotated=rotated,
          unavailable_policy=body.unavailable_policy)
    db.commit()
    return _settings_view(db)


@router.post("/settings/recaptcha/test")
def test_recaptcha(request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    from .. import captcha

    cur = settings_store.get(db, "recaptcha")
    if not cur["secret_enc"]:
        raise HTTPException(409, "Save a secret key first")
    ok, msg = captcha.test_secret(settings_store.unseal(cur["secret_enc"]))
    settings_store.put(db, "recaptcha", {"last_test_at": datetime.now(timezone.utc).isoformat(),
                                         "last_test_ok": ok, "last_test_message": msg}, me.id)
    audit(db, request, me, "platform_recaptcha_tested", "recaptcha", tenant_id=me.tenant_id, scope="platform", ok=ok)
    db.commit()
    return {"ok": ok, "message": msg, **_settings_view(db)}


@router.put("/settings/trusted-ips")
def put_trusted_ips(body: TrustedIpsIn, request: Request, db: Session = Depends(get_db),
                    me: User = Depends(require_platform_admin)):
    try:
        cidrs = settings_store.parse_cidrs(body.cidrs)
    except ValueError as e:
        raise HTTPException(422, str(e))
    old = settings_store.get(db, "trusted_ips")["cidrs"]
    settings_store.put(db, "trusted_ips", {"cidrs": cidrs, "note": body.note}, me.id)
    audit(db, request, me, "platform_trusted_ips_updated", "trusted_ips", tenant_id=me.tenant_id, scope="platform",
          added=sorted(set(cidrs) - set(old)), removed=sorted(set(old) - set(cidrs)))
    db.commit()
    return _settings_view(db)


@router.put("/settings/smtp")
def put_smtp(body: SmtpIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    from ..security import SecretsUnavailable
    from .routes_admin import EMAIL_RE

    if body.from_address and not EMAIL_RE.match(body.from_address):
        raise HTTPException(422, "Invalid sender address")
    if body.enabled and not (body.host and body.from_address):
        raise HTTPException(422, "Host and sender address are required to enable e-mail")
    upd = body.model_dump(exclude={"password"})
    upd["host"] = body.host.strip()
    cur = settings_store.get(db, "smtp")
    moved = (cur["host"], int(cur["port"]), cur["security"], cur["username"]) != (upd["host"], body.port, body.security, body.username)
    if moved and cur["password_enc"] and not body.password:
        if body.username:
            raise HTTPException(422, "The server or account changed: enter the SMTP password again")
        upd["password_enc"], upd["password_hint"] = "", ""
    if body.password:
        try:
            upd["password_enc"], upd["password_hint"] = settings_store.seal(body.password)
        except SecretsUnavailable as e:
            raise HTTPException(503, str(e))
    if not body.enabled and settings_store.auth(db)["password_reset_enabled"]:
        settings_store.put(db, "auth", {"password_reset_enabled": False}, me.id)
    settings_store.put(db, "smtp", upd, me.id)
    audit(db, request, me, "platform_smtp_updated", "smtp", tenant_id=me.tenant_id, scope="platform",
          enabled=body.enabled, host=upd["host"], port=body.port, password_rotated=bool(body.password))
    db.commit()
    return _settings_view(db)


@router.post("/settings/smtp/test")
def test_smtp(body: SmtpTestIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    from .. import mailer

    cfg = settings_store.get(db, "smtp")
    try:
        mailer.send(db, body.to.strip(), "TestAgent test e-mail",
                    "This is a test message from TestAgent. Outgoing e-mail is configured correctly.", cfg=cfg)
        ok, msg = True, f"Test e-mail sent to {body.to.strip()}"
    except mailer.MailError as e:
        ok, msg = False, str(e)
    settings_store.put(db, "smtp", {"last_test_at": datetime.now(timezone.utc).isoformat(), "last_test_ok": ok,
                                    "last_test_message": msg[:300]}, me.id)
    audit(db, request, me, "platform_smtp_tested", "smtp", tenant_id=me.tenant_id, scope="platform", ok=ok)
    db.commit()
    return {"ok": ok, "message": msg, **_settings_view(db)}
