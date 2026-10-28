"""Tenant slugs, provisioning and invitations."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Invitation, Tenant, TenantBranding, TenantRegistration

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")

# Path segments the app, nginx or the platform use (or may use) at the top level, plus names that
# could be used to impersonate Unical. A tenant can never take one of these.
RESERVED_SLUGS = frozenset("""
api assets static public platform admin administrator superadmin super-admin root system
login logout signin sign-in signup sign-up register registration activate activation invite invitations
auth oauth sso saml oidc callback callbacks webhook webhooks health healthz ready status metrics
favicon favicon-svg robots sitemap well-known projects cases runs evidence reports report settings
help support docs documentation terms privacy legal security billing pricing plans account accounts
app apps www mail email ftp cdn dev test testing staging stage prod production demo sample
unical unical-systems unicalsystems testpilot testagent testingagent anthropic claude null undefined
""".split())


def normalize_slug(slug: str) -> str:
    return (slug or "").strip().lower()


def slug_problem(slug: str, db: Session | None = None, *, allow_default: bool = False) -> str | None:
    s = normalize_slug(slug)
    if not SLUG_RE.match(s):
        return "Use 3-40 lowercase letters, digits or hyphens, starting and ending with a letter or digit"
    if "--" in s:
        return "Use single hyphens only"
    if s in RESERVED_SLUGS and not (allow_default and s == get_settings().default_tenant_slug):
        return "This address is reserved"
    if db is not None:
        if db.scalars(select(Tenant.id).where(Tenant.slug == s)).first():
            return "This address is not available"
        if db.scalars(select(TenantRegistration.id).where(TenantRegistration.requested_slug == s,
                                                          TenantRegistration.status == "pending")).first():
            return "This address is not available"
    return None


def hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invitation(db: Session, tenant: Tenant, email: str, role: str, created_by: int | None,
                      ttl_hours: int | None = None) -> tuple[Invitation, str]:
    token = secrets.token_urlsafe(32)
    inv = Invitation(tenant_id=tenant.id, token_hash=hash_invite_token(token), email=email.strip().lower(),
                     role=role, created_by=created_by,
                     expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours or get_settings().invitation_ttl_hours))
    db.add(inv)
    db.flush()
    return inv, token


def invitation_link(tenant: Tenant, token: str) -> str:
    return f"{get_settings().public_url}/{tenant.slug}/activate#{token}"


def find_invitation(db: Session, token: str) -> Invitation | None:
    """Valid = exists, not used, not revoked, not expired, and its tenant is active."""
    if not token or len(token) > 200:
        return None
    inv = db.scalars(select(Invitation).where(Invitation.token_hash == hash_invite_token(token))).first()
    if not inv or inv.used_at or inv.revoked or inv.expires_at < datetime.now(timezone.utc):
        return None
    t = db.get(Tenant, inv.tenant_id)
    if not t or t.status != "active":
        return None
    return inv


def provision(db: Session, reg: TenantRegistration, slug: str, name: str) -> Tenant:
    t = Tenant(slug=slug, name=name, status="active", registration_id=reg.id)
    db.add(t)
    db.flush()
    db.add(TenantBranding(tenant_id=t.id, display_name=name))
    from . import storage_policy
    storage_policy.row(db, t.id)  # storage not chosen yet: runs wait for the administrators' choice
    from . import billing
    billing.on_tenant_created(db, t.id)  # no plan yet: the owner chooses one (or the free trial) first
    return t


def claim_invitation(db: Session, inv_id: int) -> bool:
    """Atomically marks an invitation used. A concurrent claim blocks on the row lock, re-evaluates the
    WHERE clause and updates nothing, so a link can be redeemed exactly once."""
    from sqlalchemy import update

    now = datetime.now(timezone.utc)
    res = db.execute(update(Invitation).where(Invitation.id == inv_id, Invitation.used_at.is_(None),
                                              Invitation.revoked.is_(False), Invitation.expires_at > now)
                     .values(used_at=now))
    return res.rowcount == 1


def user_from_invitation(db: Session, inv: Invitation, username: str, display_name: str, password_hash: str):
    """Claims the invitation and creates its user (in the caller's transaction). Raises LookupError if the
    link is no longer valid, ValueError if the username or e-mail is taken."""
    from sqlalchemy.exc import IntegrityError

    from .models import User

    if not claim_invitation(db, inv.id):
        raise LookupError("invitation no longer valid")
    clash = identity_conflict(db, inv.tenant_id, username=username, email=inv.email)
    if clash:
        raise ValueError(clash)
    u = User(tenant_id=inv.tenant_id, username=username.strip().lower(), display_name=display_name.strip(),
             email=inv.email, role=inv.role, password_hash=password_hash, must_change_password=False)
    try:
        with db.begin_nested():
            db.add(u)
    except IntegrityError:
        raise ValueError("That username or e-mail address is already used in this organization")
    inv.used_by = u.id
    return u


def identity_conflict(db: Session, tenant_id: int, *, username: str | None = None, email: str | None = None,
                      exclude_user_id: int | None = None) -> str | None:
    """Sign-in accepts a username or an e-mail, so a username may not equal another member's e-mail and an
    e-mail may not equal another member's username (prevents one account shadowing another)."""
    from sqlalchemy import func

    from .models import User

    q = select(User.id).where(User.tenant_id == tenant_id)
    if exclude_user_id:
        q = q.where(User.id != exclude_user_id)
    if username and db.scalars(q.where(func.lower(User.email) == username.strip().lower())).first():
        return "This username is another member's e-mail address"
    if email and db.scalars(q.where(User.username == email.strip().lower())).first():
        return "This e-mail address is another member's username"
    return None
