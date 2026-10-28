"""Tenant administration: users, invitations, approved targets, branding and the tenant audit log.
Every query is scoped to the authenticated user's tenant (== the URL tenant, see deps.py)."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import oidc, permissions, settings_store, tenancy
from .. import targets as targets_mod
from ..config import get_settings
from ..db import get_db
from ..models import AllowedTarget, AuditLog, IdentityProvider, Invitation, Session_, Tenant, TenantBranding, User
from ..security import SecretsUnavailable, hash_password, password_problems
from ..urlguard import normalize_host, valid_hostname
from .deps import audit, current_tenant, require_perm

router = APIRouter(prefix="/admin", tags=["admin"])
users_perm = require_perm("tenant.users")
targets_perm = require_perm("tenant.targets")
settings_perm = require_perm("tenant.settings")

ROLE_PATTERN = "^(tenant_owner|tenant_admin|project_manager|qa_engineer|developer|auditor|admin|tester|viewer)$"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LOGO_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)$")
MAX_LOGO_BYTES = 200 * 1024


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._@-]+$")
    display_name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=254)
    role: str = Field(pattern=ROLE_PATTERN)
    password: str = Field(min_length=12, max_length=256)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    role: str | None = Field(default=None, pattern=ROLE_PATTERN)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(pattern=ROLE_PATTERN)


class TargetIn(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    note: str = Field(default="", max_length=300)


class BrandingIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)
    logo_data_url: str = Field(default="", max_length=300_000)
    primary_color: str = Field(default="#2a63d4")
    welcome_text: str = Field(default="", max_length=1000)
    support_email: str = Field(default="", max_length=254)
    support_url: str = Field(default="", max_length=500)

    @field_validator("primary_color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not HEX_RE.match(v):
            raise ValueError("must be a hex colour like #2a63d4")
        return v.lower()

    @field_validator("logo_data_url")
    @classmethod
    def _logo(cls, v: str) -> str:
        if not v:
            return v
        m = LOGO_RE.match(v)
        if not m:
            raise ValueError("logo must be a PNG, JPEG or WebP image")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            raise ValueError("logo is not valid base64")
        if len(raw) > MAX_LOGO_BYTES:
            raise ValueError("logo must be 200 KB or smaller")
        sig = {"png": raw[:8] == b"\x89PNG\r\n\x1a\n", "jpeg": raw[:3] == b"\xff\xd8\xff",
               "webp": raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"}
        if not sig[m.group(1)]:
            raise ValueError("logo content does not match its image type")
        return v

    @field_validator("support_email")
    @classmethod
    def _email(cls, v: str) -> str:
        if v and not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @field_validator("support_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if v and not re.match(r"^https://[^\s<>\"']+$", v):
            raise ValueError("support URL must start with https://")
        return v


def _role(value: str, me: User) -> str:
    try:
        r = permissions.normalize_role(value)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if r not in permissions.ASSIGNABLE_BY.get(me.role, frozenset()):
        raise HTTPException(403, "Only a Tenant Owner can assign the Tenant Owner role")
    return r


def _email(value: str) -> str:
    v = value.strip().lower()
    if v and not EMAIL_RE.match(v):
        raise HTTPException(422, "Invalid email address")
    return v


def _u(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "email": u.email, "role": u.role,
            "role_label": permissions.ROLE_LABELS.get(u.role, u.role), "is_active": u.is_active,
            "must_change_password": u.must_change_password, "is_platform_admin": u.is_platform_admin,
            "last_login_at": u.last_login_at, "created_at": u.created_at}


def _tenant_user(db: Session, me: User, user_id: int) -> User:
    u = db.get(User, user_id)
    if not u or u.tenant_id != me.tenant_id:
        raise HTTPException(404, "User not found")
    return u


@router.get("/roles")
def roles(_: User = Depends(require_perm("tenant.member"))):
    return [{"role": r, "label": permissions.ROLE_LABELS[r], "permissions": permissions.permissions_for(r)}
            for r in permissions.ROLE_LABELS]


@router.get("/users")
def list_users(db: Session = Depends(get_db), me: User = Depends(users_perm)):
    return [_u(u) for u in db.scalars(select(User).where(User.tenant_id == me.tenant_id).order_by(User.username)).all()]


@router.post("/users", status_code=201)
def create_user(body: UserIn, request: Request, db: Session = Depends(get_db), me: User = Depends(users_perm)):
    role = _role(body.role, me)
    probs = password_problems(body.password)
    if probs:
        raise HTTPException(422, "Password needs " + " and ".join(probs))
    clash = tenancy.identity_conflict(db, me.tenant_id, username=body.username, email=body.email)
    if clash:
        raise HTTPException(409, clash)
    u = User(tenant_id=me.tenant_id, username=body.username.lower(), display_name=body.display_name,
             email=_email(body.email), role=role, password_hash=hash_password(body.password), must_change_password=True)
    db.add(u)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username or e-mail already exists")
    audit(db, request, me, "user_created", u.username, role=u.role)
    db.commit()
    return _u(u)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, db: Session = Depends(get_db),
                me: User = Depends(users_perm)):
    u = _tenant_user(db, me, user_id)
    if u.role == "tenant_owner" and me.role != "tenant_owner":
        raise HTTPException(403, "Only a Tenant Owner can change another Tenant Owner")
    if u.is_platform_admin and u.id != me.id:
        # Super Admin accounts are managed only with the operator CLI on the server.
        raise HTTPException(403, "Unical Super Admin accounts can only be changed by the server operator")
    new_role = _role(body.role, me) if body.role is not None else None
    if u.id == me.id and ((new_role is not None and new_role != u.role) or body.is_active is False):
        raise HTTPException(400, "You cannot change your own role or disable your own account")
    changes = {}
    if body.display_name is not None:
        u.display_name = body.display_name
    if body.email is not None:
        clash = tenancy.identity_conflict(db, me.tenant_id, email=body.email, exclude_user_id=u.id)
        if clash:
            raise HTTPException(409, clash)
        u.email = _email(body.email)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            raise HTTPException(409, "E-mail already used by another member")
    if new_role is not None and new_role != u.role:
        changes["role"] = [u.role, new_role]
        u.role = new_role
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


# ---- invitations -----------------------------------------------------------------------------
def _inv(i: Invitation) -> dict:
    now = datetime.now(timezone.utc)
    state = "used" if i.used_at else "revoked" if i.revoked else "expired" if i.expires_at < now else "pending"
    return {"id": i.id, "email": i.email, "role": i.role, "state": state, "created_at": i.created_at,
            "expires_at": i.expires_at, "used_at": i.used_at}


@router.get("/invitations")
def list_invitations(db: Session = Depends(get_db), me: User = Depends(users_perm)):
    return [_inv(i) for i in db.scalars(select(Invitation).where(Invitation.tenant_id == me.tenant_id)
                                         .order_by(Invitation.id.desc()).limit(200)).all()]


@router.post("/invitations", status_code=201)
def create_invitation(body: InviteIn, request: Request, db: Session = Depends(get_db),
                      me: User = Depends(users_perm), tenant: Tenant = Depends(current_tenant)):
    email = _email(body.email)
    if not email:
        raise HTTPException(422, "Email is required")
    inv, token = tenancy.create_invitation(db, tenant, email, _role(body.role, me), me.id)
    audit(db, request, me, "invitation_created", email, role=inv.role, invitation_id=inv.id)
    db.commit()
    from .. import mailer
    link = tenancy.invitation_link(tenant, token)
    emailed = mailer.try_send(db, email, f"You are invited to {tenant.name} on TestAgent",
                              f"Hello,\n\n{me.display_name or me.username} invited you to {tenant.name} on TestAgent "
                              f"as {permissions.ROLE_LABELS.get(inv.role, inv.role)}.\nActivate your account within "
                              f"{get_settings().invitation_ttl_hours} hours with this one-time link:\n\n{link}\n")
    # The link is also shown once to the inviting admin (in case e-mail is not configured or delayed).
    return {**_inv(inv), "link": link, "emailed": emailed}


@router.delete("/invitations/{inv_id}")
def revoke_invitation(inv_id: int, request: Request, db: Session = Depends(get_db), me: User = Depends(users_perm)):
    inv = db.get(Invitation, inv_id)
    if not inv or inv.tenant_id != me.tenant_id:
        raise HTTPException(404, "Invitation not found")
    inv.revoked = True
    audit(db, request, me, "invitation_revoked", inv.email, invitation_id=inv.id)
    db.commit()
    return _inv(inv)


# ---- approved test targets (per tenant) -------------------------------------------------------
@router.get("/targets")
def list_targets(db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.member"))):
    priv = get_settings().private_targets if me.tenant_id in targets_mod.private_tenant_ids(db) else set()
    return [{"id": t.id, "host": t.host, "note": t.note, "created_at": t.created_at, "private_access": t.host in priv}
            for t in db.scalars(select(AllowedTarget).where(AllowedTarget.tenant_id == me.tenant_id)
                                .order_by(AllowedTarget.host)).all()]


@router.post("/targets", status_code=201)
def add_target(body: TargetIn, request: Request, db: Session = Depends(get_db), me: User = Depends(targets_perm)):
    host = normalize_host(body.host)
    is_ipv6 = host.count(":") > 1
    if "://" in body.host or "/" in host or (":" in host and not is_ipv6):
        raise HTTPException(422, "Enter a bare hostname, e.g. app.example.com (no scheme, path or port)")
    if not valid_hostname(host):
        raise HTTPException(422, "Invalid hostname")
    t = AllowedTarget(tenant_id=me.tenant_id, host=host, note=body.note, created_by=me.id)
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
def remove_target(target_id: int, request: Request, db: Session = Depends(get_db), me: User = Depends(targets_perm)):
    t = db.get(AllowedTarget, target_id)
    if not t or t.tenant_id != me.tenant_id:
        raise HTTPException(404, "Not found")
    audit(db, request, me, "target_removed", t.host)
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---- branding -------------------------------------------------------------------------------
def branding_out(t: Tenant, b: TenantBranding | None) -> dict:
    return {"slug": t.slug, "name": t.name,
            "display_name": (b.display_name if b and b.display_name else t.name),
            "logo_data_url": b.logo_data_url if b else "", "primary_color": b.primary_color if b else "#2a63d4",
            "welcome_text": b.welcome_text if b else "", "support_email": b.support_email if b else "",
            "support_url": b.support_url if b else ""}


@router.get("/branding")
def get_branding(db: Session = Depends(get_db), me: User = Depends(settings_perm), tenant: Tenant = Depends(current_tenant)):
    return branding_out(tenant, db.get(TenantBranding, tenant.id))


@router.put("/branding")
def put_branding(body: BrandingIn, request: Request, db: Session = Depends(get_db), me: User = Depends(settings_perm),
                 tenant: Tenant = Depends(current_tenant)):
    b = db.get(TenantBranding, tenant.id)
    if not b:
        b = TenantBranding(tenant_id=tenant.id)
        db.add(b)
    for k, v in body.model_dump().items():
        setattr(b, k, v)
    audit(db, request, me, "branding_updated", tenant.slug)
    db.commit()
    return branding_out(tenant, b)


# ---- audit -----------------------------------------------------------------------------------
@router.get("/audit")
def audit_log(limit: int = 200, db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.audit"))):
    rows = db.scalars(select(AuditLog).where(AuditLog.tenant_id == me.tenant_id, AuditLog.scope == "tenant")
                      .order_by(AuditLog.id.desc()).limit(max(1, min(limit, 1000)))).all()
    names = {u.id: u.username for u in db.scalars(select(User).where(User.tenant_id == me.tenant_id)).all()}
    return [{"id": r.id, "at": r.created_at, "user": names.get(r.user_id), "action": r.action, "target": r.target,
             "detail": r.detail, "ip": r.ip} for r in rows]


# ---- Identity & SSO (organization) -----------------------------------------------------------
class SsoPolicyIn(BaseModel):
    password_enabled: bool
    microsoft_enabled: bool
    trusted_ips: list[str] = Field(default_factory=list, max_length=200)
    confirm: bool = False


class MicrosoftIn(BaseModel):
    entra_tenant_id: str = Field(max_length=64)
    client_id: str = Field(max_length=64)
    client_secret: str | None = Field(default=None, max_length=512)  # omit to keep the stored secret
    cloud_host: str = Field(default="login.microsoftonline.com", max_length=100)
    allowed_domains: list[str] = Field(default_factory=list, max_length=50)
    allowed_group_ids: list[str] = Field(default_factory=list, max_length=50)
    provisioning: str = Field(default="invite_only", pattern="^(invite_only|auto_domain)$")
    auto_role: str = Field(default="auditor", pattern="^(project_manager|qa_engineer|developer|auditor)$")


def _sso_guard(me: User, tenant: Tenant) -> None:
    # The Unical organization's identity configuration is platform configuration: Super Admin only.
    if settings_store.is_platform_tenant(tenant) and not me.is_platform_admin:
        raise HTTPException(403, "Unical sign-in settings are managed by the Unical Super Admin")


def _idp(db: Session, tenant: Tenant) -> IdentityProvider | None:
    return db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id == tenant.id,
                                                     IdentityProvider.provider == "microsoft")).first()


def _idp_out(idp: IdentityProvider | None) -> dict | None:
    if not idp:
        return None
    return {"entra_tenant_id": idp.entra_tenant_id, "client_id": idp.client_id, "secret_set": bool(idp.client_secret_enc),
            "secret_hint": idp.secret_hint, "secret_updated_at": idp.secret_updated_at, "cloud_host": idp.cloud_host,
            "allowed_domains": idp.allowed_domains, "allowed_group_ids": idp.allowed_group_ids,
            "provisioning": idp.provisioning, "auto_role": idp.auto_role, "status": idp.status,
            "last_validated_at": idp.last_validated_at, "last_error": idp.last_error, "updated_at": idp.updated_at}


def _revoke_password_sessions(db: Session, tenant_id: int) -> int:
    return db.execute(delete(Session_).where(Session_.tenant_id == tenant_id,
                                             Session_.auth_method.in_(("password", "invite")))).rowcount


def _revoke_microsoft_sessions(db: Session, tenant_id: int) -> int:
    return db.execute(delete(Session_).where(Session_.tenant_id == tenant_id, Session_.auth_method == "microsoft")).rowcount


@router.get("/sso")
def get_sso(db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.sso")),
            tenant: Tenant = Depends(current_tenant)):
    _sso_guard(me, tenant)
    a = settings_store.auth(db)
    ta = settings_store.tenant_auth(db, tenant)
    platform = settings_store.is_platform_tenant(tenant)
    db.commit()
    return {"platform_organization": platform, "redirect_uri": oidc.redirect_uri(),
            "allowed": {"password": True if platform else a["tenant_password_allowed"],
                        "microsoft": True if platform else a["tenant_microsoft_allowed"]},
            "policy": {"password_enabled": a["platform_password_enabled"] if platform else ta.password_enabled,
                       "microsoft_enabled": a["platform_microsoft_enabled"] if platform else ta.microsoft_enabled,
                       "trusted_ips": ta.trusted_ips or []},
            "effective": settings_store.methods(db, tenant),
            "microsoft": _idp_out(_idp(db, tenant))}


@router.put("/sso/policy")
def put_sso_policy(body: SsoPolicyIn, request: Request, db: Session = Depends(get_db),
                   me: User = Depends(require_perm("tenant.sso")), tenant: Tenant = Depends(current_tenant)):
    _sso_guard(me, tenant)
    try:
        cidrs = settings_store.parse_cidrs(body.trusted_ips)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ta = settings_store.tenant_auth(db, tenant)
    platform = settings_store.is_platform_tenant(tenant)
    a = settings_store.auth(db)
    idp = _idp(db, tenant)
    ms_ready = bool(idp and idp.status == "active")
    if body.microsoft_enabled and not ms_ready:
        raise HTTPException(409, "Test the Microsoft connection successfully before enabling Microsoft sign-in")
    if not platform:
        if body.microsoft_enabled and not a["tenant_microsoft_allowed"]:
            raise HTTPException(403, "Microsoft sign-in is not permitted for organizations by the platform policy")
        if body.password_enabled and not a["tenant_password_allowed"]:
            raise HTTPException(403, "Password sign-in is not permitted for organizations by the platform policy")
    if not body.password_enabled and not body.microsoft_enabled:
        raise HTTPException(409, "At least one sign-in method must stay enabled")
    if not body.password_enabled and not body.confirm:
        raise HTTPException(409, "Only Microsoft sign-in will remain. Users without a linked Microsoft account "
                                 "will not be able to sign in. Confirm to continue.")
    old = settings_store.methods(db, tenant)
    old_ms = old["microsoft"]
    if platform:
        settings_store.put(db, "auth", {"platform_password_enabled": body.password_enabled,
                                        "platform_microsoft_enabled": body.microsoft_enabled}, me.id)
    else:
        ta.password_enabled, ta.microsoft_enabled = body.password_enabled, body.microsoft_enabled
    ta.trusted_ips, ta.updated_by = cidrs, me.id
    revoked = _revoke_microsoft_sessions(db, tenant.id) if old_ms and not body.microsoft_enabled else 0
    if old["password"] and not body.password_enabled:
        revoked += _revoke_password_sessions(db, tenant.id)
    audit(db, request, me, "sso_policy_updated", tenant.slug, password=body.password_enabled,
          microsoft=body.microsoft_enabled, trusted_ips=cidrs, microsoft_sessions_revoked=revoked)
    db.commit()
    return get_sso(db, me, tenant)


@router.put("/sso/microsoft")
def put_microsoft(body: MicrosoftIn, request: Request, db: Session = Depends(get_db),
                  me: User = Depends(require_perm("tenant.sso")), tenant: Tenant = Depends(current_tenant)):
    _sso_guard(me, tenant)
    tid, cid = body.entra_tenant_id.strip().lower(), body.client_id.strip().lower()
    try:
        oidc.validate_config(tid, cid, body.cloud_host.strip().lower())
    except oidc.OidcError as e:
        raise HTTPException(422, e.detail)
    domains = sorted({d.strip().lower().lstrip("@") for d in body.allowed_domains if d.strip()})
    if any(not valid_hostname(d) for d in domains):
        raise HTTPException(422, "Allowed domains must be domain names like example.com")
    if body.provisioning == "auto_domain" and not domains:
        raise HTTPException(422, "Automatic provisioning needs at least one allowed e-mail domain")
    groups = sorted({g.strip().lower() for g in body.allowed_group_ids if g.strip()})
    if any(not oidc.GUID_RE.match(g) for g in groups):
        raise HTTPException(422, "Allowed groups must be Microsoft Entra group object IDs (GUIDs)")
    idp = _idp(db, tenant)
    if not idp:
        idp = IdentityProvider(tenant_id=tenant.id, provider="microsoft", allowed_domains=[], allowed_group_ids=[])
        db.add(idp)
    changed_identity = (idp.entra_tenant_id, idp.client_id, idp.cloud_host) != (tid, cid, body.cloud_host.strip().lower())
    idp.entra_tenant_id, idp.client_id, idp.cloud_host = tid, cid, body.cloud_host.strip().lower()
    idp.allowed_domains, idp.allowed_group_ids = domains, groups
    idp.provisioning, idp.auto_role, idp.updated_by = body.provisioning, body.auto_role, me.id
    rotated = False
    if body.client_secret:
        try:
            idp.client_secret_enc, idp.secret_hint = settings_store.seal(body.client_secret.strip())
        except SecretsUnavailable as e:
            raise HTTPException(503, str(e))
        idp.secret_updated_at, rotated = datetime.now(timezone.utc), True
    if changed_identity or rotated:
        # must be re-tested before it can be used again
        idp.status, idp.last_error = "unverified", ""
    audit(db, request, me, "sso_microsoft_saved", tenant.slug, entra_tenant_id=tid, client_id=cid,
          secret_rotated=rotated, domains=domains, groups=groups, provisioning=body.provisioning)
    db.commit()
    return get_sso(db, me, tenant)


@router.post("/sso/microsoft/test")
def test_microsoft(request: Request, db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.sso")),
                   tenant: Tenant = Depends(current_tenant)):
    _sso_guard(me, tenant)
    idp = _idp(db, tenant)
    if not idp or not idp.client_secret_enc:
        raise HTTPException(409, "Save the Microsoft configuration with a client secret first")
    try:
        ok, msg = oidc.test_connection(idp, settings_store.unseal(idp.client_secret_enc))
    except SecretsUnavailable as e:
        ok, msg = False, str(e)
    idp.status, idp.last_error = ("active", "") if ok else ("error", msg)
    if ok:
        idp.last_validated_at = datetime.now(timezone.utc)
    audit(db, request, me, "sso_microsoft_tested", tenant.slug, ok=ok, message=msg[:300])
    db.commit()
    return {"ok": ok, "message": msg, **get_sso(db, me, tenant)}


@router.delete("/sso/microsoft")
def disconnect_microsoft(request: Request, db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.sso")),
                         tenant: Tenant = Depends(current_tenant)):
    _sso_guard(me, tenant)
    methods = settings_store.methods(db, tenant)
    if not methods["password"]:
        raise HTTPException(409, "Enable password sign-in first, otherwise nobody could sign in")
    idp = _idp(db, tenant)
    if idp:
        db.delete(idp)
    if settings_store.is_platform_tenant(tenant):
        settings_store.put(db, "auth", {"platform_microsoft_enabled": False}, me.id)
    else:
        settings_store.tenant_auth(db, tenant).microsoft_enabled = False
    revoked = _revoke_microsoft_sessions(db, tenant.id)
    audit(db, request, me, "sso_microsoft_disconnected", tenant.slug, microsoft_sessions_revoked=revoked)
    db.commit()
    return get_sso(db, me, tenant)
