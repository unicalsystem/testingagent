"""Microsoft sign-in endpoints. See testpilot/oidc.py for the security model."""
from __future__ import annotations

import html
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import oidc, settings_store, tenancy
from ..config import get_settings
from ..db import get_db
from ..models import ExternalIdentity, IdentityProvider, Invitation, Tenant, User
from .deps import audit, current_tenant, current_user
from .routes_auth import issue_session

log = logging.getLogger("testpilot.oidc")
public = APIRouter(prefix="/public/oidc/microsoft", tags=["sso"])
scoped = APIRouter(prefix="/auth/microsoft", tags=["sso"])
PROVISION_ROLES = ("project_manager", "qa_engineer", "developer", "auditor")


class StartIn(BaseModel):
    tenant: str = Field(min_length=3, max_length=40)
    purpose: str = Field(pattern="^(login|invite)$")
    invite_token: str | None = Field(default=None, max_length=200)


def _provider(db: Session, tenant: Tenant) -> IdentityProvider:
    idp = db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id == tenant.id,
                                                    IdentityProvider.provider == "microsoft")).first()
    if not settings_store.methods(db, tenant)["microsoft"] or not idp or idp.status != "active" or not idp.client_secret_enc:
        raise HTTPException(403, "Microsoft sign-in is not enabled for this organization")
    return idp


def _bind(response: Response, binding: str) -> None:
    s = get_settings()
    # Lax: the cookie must accompany the top-level redirect back from Microsoft (a cross-site GET).
    response.set_cookie(oidc.BINDING_COOKIE, binding, max_age=int(oidc.TX_TTL.total_seconds()), httponly=True,
                        secure=s.cookie_secure, samesite="lax", path="/api/public/oidc/microsoft")


@public.post("/start")
def start(body: StartIn, response: Response, db: Session = Depends(get_db)):
    s = tenancy.normalize_slug(body.tenant)
    t = db.scalars(select(Tenant).where(Tenant.slug == s)).first() if tenancy.SLUG_RE.match(s) else None
    if not t or t.status != "active":
        raise HTTPException(404, "Organization not found")
    idp = _provider(db, t)
    inv_id, hint = None, ""
    if body.purpose == "invite":
        inv = tenancy.find_invitation(db, body.invite_token or "")
        if not inv or inv.tenant_id != t.id:
            raise HTTPException(404, "This activation link is invalid, already used or expired")
        inv_id, hint = inv.id, inv.email
    try:
        url, binding = oidc.start(db, idp, body.purpose, invitation_id=inv_id, login_hint=hint)
    except oidc.OidcError as e:
        raise HTTPException(502, "Microsoft sign-in is unavailable right now") from e
    db.commit()
    _bind(response, binding)
    return {"url": url}


@scoped.post("/link-start")
def link_start(response: Response, db: Session = Depends(get_db), me: User = Depends(current_user),
               tenant: Tenant = Depends(current_tenant)):
    idp = _provider(db, tenant)
    try:
        url, binding = oidc.start(db, idp, "link", user_id=me.id, login_hint=me.email or "")
    except oidc.OidcError as e:
        raise HTTPException(502, "Microsoft sign-in is unavailable right now") from e
    db.commit()
    _bind(response, binding)
    return {"url": url}


def _page(target: str) -> HTMLResponse:
    """Same-site hop so the SameSite=Strict session cookie is used on the next navigation."""
    t = html.escape(target, quote=True)
    resp = HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={t}">'
                        f'<title>Signing in…</title><a href="{t}">Continue</a>')
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'none'"
    resp.delete_cookie(oidc.BINDING_COOKIE, path="/api/public/oidc/microsoft")
    return resp


def _username(db: Session, tenant_id: int, email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower())[:60] or "user"
    name, i = base, 1
    while db.scalars(select(User.id).where(User.tenant_id == tenant_id, User.username == name)).first():
        i += 1
        name = f"{base}{i}"
    return name


@public.get("/callback")
def callback(request: Request, db: Session = Depends(get_db), code: str = "", state: str = "", error: str = ""):
    binding = request.cookies.get(oidc.BINDING_COOKIE)
    tenant = None
    try:
        tx = oidc.consume(db, state, binding)
        db.commit()  # the transaction is spent even if anything below fails
        tenant = db.get(Tenant, tx.tenant_id)
        if not tenant or tenant.status != "active":
            raise oidc.OidcError("organization_inactive")
        if error:
            raise oidc.OidcError("cancelled", error[:100])
        if not settings_store.methods(db, tenant)["microsoft"]:
            raise oidc.OidcError("disabled")
        claims = oidc.redeem(db, tx, code)
        idp = db.get(IdentityProvider, tx.provider_id)
        oidc.check_restrictions(idp, claims)
        issuer, subject, email = claims["iss"], str(claims["oid"]), oidc.email_of(claims)
        linked = db.scalars(select(ExternalIdentity).where(
            ExternalIdentity.tenant_id == tenant.id, ExternalIdentity.provider == "microsoft",
            ExternalIdentity.issuer == issuer, ExternalIdentity.subject == subject)).first()

        if tx.purpose == "link":
            u = db.get(User, tx.user_id) if tx.user_id else None
            if not u or u.tenant_id != tenant.id or not u.is_active:
                raise oidc.OidcError("account_inactive")
            if linked and linked.user_id != u.id:
                raise oidc.OidcError("already_linked", "Microsoft account linked to another user")
            if not linked:
                db.add(ExternalIdentity(tenant_id=tenant.id, user_id=u.id, provider="microsoft", issuer=issuer,
                                        subject=subject, email_at_link=email))
            audit(db, request, u, "microsoft_linked", u.username, email=email)
            db.commit()
            return _page(f"/{tenant.slug}/projects?notice=microsoft_linked")

        if tx.purpose == "invite":
            inv = db.get(Invitation, tx.invitation_id) if tx.invitation_id else None
            if not inv or inv.tenant_id != tenant.id:
                raise oidc.OidcError("invitation_invalid")
            if inv.email.lower() not in oidc.emails_of(claims):
                raise oidc.OidcError("email_mismatch", "Microsoft account e-mail differs from the invitation")
            if linked:
                raise oidc.OidcError("already_linked")
            try:
                u = tenancy.user_from_invitation(db, inv, _username(db, tenant.id, email), claims.get("name", ""), "")
            except LookupError:
                raise oidc.OidcError("invitation_invalid")
            except ValueError:
                raise oidc.OidcError("account_exists")
            db.add(ExternalIdentity(tenant_id=tenant.id, user_id=u.id, provider="microsoft", issuer=issuer,
                                    subject=subject, email_at_link=email))
            audit(db, request, u, "invitation_accepted", u.username, invitation_id=inv.id, role=u.role, method="microsoft")
        else:  # login
            if linked:
                u = db.get(User, linked.user_id)
            elif idp.provisioning == "auto_domain" and idp.allowed_domains and email:
                # Organization-approved provisioning: a new account with the configured role and NO
                # project access (assignments are still required). Never links to an existing account
                # just because the e-mail matches.
                if db.scalars(select(User.id).where(User.tenant_id == tenant.id, User.email == email)).first():
                    raise oidc.OidcError("account_exists", "an account with this e-mail exists; sign in and link it")
                role = idp.auto_role if idp.auto_role in PROVISION_ROLES else "auditor"
                u = User(tenant_id=tenant.id, username=_username(db, tenant.id, email), email=email,
                         display_name=str(claims.get("name", ""))[:120], role=role, password_hash="")
                try:
                    with db.begin_nested():
                        db.add(u)
                        db.flush()
                        db.add(ExternalIdentity(tenant_id=tenant.id, user_id=u.id, provider="microsoft",
                                                issuer=issuer, subject=subject, email_at_link=email))
                except IntegrityError:
                    raise oidc.OidcError("account_exists")
                audit(db, request, u, "user_provisioned", u.username, method="microsoft", role=role)
            else:
                raise oidc.OidcError("not_linked", "no TestAgent account is linked to this Microsoft account")
        if not u or not u.is_active or u.tenant_id != tenant.id:
            raise oidc.OidcError("account_inactive")
        resp = _page(f"/{tenant.slug}/projects")
        token = issue_session(db, request, None, u, tenant, "microsoft")
        from .routes_auth import set_cookie
        set_cookie(resp, token, settings_store.auth(db)["session_ttl_hours"])
        audit(db, request, u, "login", u.username, method="microsoft")
        db.commit()
        return resp
    except oidc.OidcError as e:
        db.rollback()
        log.info("microsoft sign-in failed: %s", e)
        if tenant is not None:
            audit(db, request, None, "microsoft_login_failed", e.code, tenant_id=tenant.id, detail_msg=e.detail[:300])
            db.commit()
            return _page(f"/{tenant.slug}/?sso_error={e.code}")
        return _page("/?sso_error=" + e.code)
