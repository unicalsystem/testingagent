"""Privacy, legal notices and data-subject requests.

  public   GET  /public/legal                    legal entity, contacts, document versions (for the policy pages)
           POST /public/privacy-requests         request from someone without an account (e.g. a registrant)
  member   GET  /privacy/me                      my requests + documents I still have to accept
           GET  /privacy/me/export               my personal data (JSON download)
           POST /privacy/requests                raise a request
           POST /privacy/accept                  accept the current Terms and Privacy Policy
  admin    GET  /admin/privacy-requests          organization requests (tenant.users)
           PATCH /admin/privacy-requests/{id}    update status / response
           POST /admin/users/{uid}/erase         anonymise an account
  platform GET/PUT /platform/legal, /platform/privacy, GET/PATCH /platform/privacy-requests
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import captcha, mailer, privacy, settings_store
from ..db import get_db
from ..models import PRIVACY_REQUEST_KINDS, PRIVACY_REQUEST_STATUSES, PrivacyRequest, Tenant, User, utcnow
from .deps import audit, client_ip, current_tenant, current_user, member, require_perm, require_platform_admin

public = APIRouter(prefix="/public", tags=["privacy"])
scoped = APIRouter(tags=["privacy"])
platform = APIRouter(prefix="/platform", tags=["privacy"])

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[a-zA-Z]{2,24}$")


class RequestIn(BaseModel):
    kind: str
    details: str = Field(default="", max_length=4000)


class PublicRequestIn(RequestIn):
    name: str = Field(min_length=2, max_length=160)
    email: str = Field(min_length=5, max_length=254)
    organization: str = Field(default="", max_length=160)
    captcha_token: str | None = Field(default=None, max_length=4000)


class UpdateIn(BaseModel):
    status: str
    response: str = Field(default="", max_length=4000)


class LegalIn(BaseModel):
    entity_name: str = Field(min_length=2, max_length=160)
    address: str = Field(default="", max_length=400)
    contact_email: str = Field(default="", max_length=254)
    grievance_officer_name: str = Field(default="", max_length=160)
    grievance_officer_email: str = Field(default="", max_length=254)
    grievance_officer_phone: str = Field(default="", max_length=40)
    dpo_email: str = Field(default="", max_length=254)
    uk_representative: str = Field(default="", max_length=400)
    us_contact: str = Field(default="", max_length=400)
    hosting_location: str = Field(default="", max_length=160)
    publish_new_version: bool = False  # everyone accepts the Terms and Privacy Policy again


class PrivacySettingsIn(BaseModel):
    request_response_days: int = Field(ge=1, le=90)
    audit_retention_days: int = Field(ge=0, le=3650)
    registration_retention_days: int = Field(ge=0, le=3650)
    session_retention_days: int = Field(ge=1, le=365)


def _kind(k: str) -> str:
    if k not in PRIVACY_REQUEST_KINDS:
        raise HTTPException(422, "Unknown request type")
    return k


def _notify(db: Session, r: PrivacyRequest) -> None:
    l = privacy.legal(db)
    to = l["grievance_officer_email"] or l["dpo_email"] or l["contact_email"]
    if to:
        mailer.try_send(db, to, f"TestAgent privacy request #{r.id}: {privacy.KIND_LABELS[r.kind]}",
                        f"A privacy request was received.\n\nType: {privacy.KIND_LABELS[r.kind]}\nFrom: {r.requester_name} "
                        f"<{r.requester_email}>\nDue by: {r.due_at:%d %b %Y}\n\nDetails:\n{r.details}\n\n"
                        "Handle it in TestAgent (Administration → Privacy requests, or Platform → Legal & privacy).")


# ------------------------------------------------------------------------------ public
@public.get("/legal")
def legal_info(db: Session = Depends(get_db)):
    l = privacy.legal(db)
    return {**l, "request_kinds": [{"kind": k, "label": v} for k, v in privacy.KIND_LABELS.items()],
            "response_days": settings_store.get(db, "privacy")["request_response_days"],
            "retention": {k: v for k, v in settings_store.get(db, "privacy").items() if k.endswith("_days")}}


@public.post("/privacy-requests", status_code=201)
def public_request(body: PublicRequestIn, request: Request, db: Session = Depends(get_db)):
    captcha.verify(db, request, "register", body.captcha_token,
                   audit=lambda action, target, **d: audit(db, request, None, action, target, tenant_id=None,
                                                          scope="platform", **d))
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    ip = client_ip(request)
    recent = db.scalar(select(func.count()).select_from(PrivacyRequest).where(
        PrivacyRequest.ip == ip, PrivacyRequest.created_at >= utcnow() - timedelta(days=1))) or 0
    if ip and recent >= 5:
        raise HTTPException(429, "Too many requests from this network today. Email us instead.")
    details = (f"Organization: {body.organization}\n" if body.organization else "") + body.details
    r = PrivacyRequest(kind=_kind(body.kind), requester_name=body.name.strip(), requester_email=email,
                       details=details, due_at=privacy.due_date(db), ip=ip)
    db.add(r)
    db.flush()
    audit(db, request, None, "privacy_request_received", f"request {r.id}", tenant_id=None, scope="platform",
          kind=r.kind)
    _notify(db, r)
    db.commit()
    return {"id": r.id, "message": f"Request received (reference #{r.id}). We will verify your identity by email and "
                                   f"respond by {r.due_at:%d %b %Y}."}


# ------------------------------------------------------------------------------ signed-in member
@scoped.get("/privacy/me")
def my_privacy(db: Session = Depends(get_db), me: User = Depends(current_user)):
    reqs = db.scalars(select(PrivacyRequest).where(PrivacyRequest.user_id == me.id).order_by(PrivacyRequest.id.desc())).all()
    return {"requests": [privacy.request_out(r, full=False) | {"details": r.details} for r in reqs],
            "needs_acceptance": privacy.needs_acceptance(db, me), "versions": privacy.versions(db),
            "kinds": [{"kind": k, "label": v} for k, v in privacy.KIND_LABELS.items()]}


@scoped.get("/privacy/me/export")
def export_me(request: Request, db: Session = Depends(get_db), me: User = Depends(current_user)):
    data = privacy.export_user(db, me)
    audit(db, request, me, "privacy_export", me.username)
    db.commit()
    body = json.dumps(data, indent=2, default=lambda o: o.isoformat() if isinstance(o, (datetime, date)) else str(o))
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="testpilot-my-data-{date.today():%Y%m%d}.json"',
        "Cache-Control": "private, no-store"})


@scoped.post("/privacy/requests", status_code=201)
def raise_request(body: RequestIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    open_n = db.scalar(select(func.count()).select_from(PrivacyRequest).where(
        PrivacyRequest.user_id == me.id, PrivacyRequest.status.in_(("open", "in_progress")))) or 0
    if open_n >= 10:
        raise HTTPException(429, "You already have 10 open requests")
    r = PrivacyRequest(tenant_id=me.tenant_id, user_id=me.id, kind=_kind(body.kind),
                       requester_name=me.display_name or me.username, requester_email=me.email,
                       details=body.details, due_at=privacy.due_date(db), ip=client_ip(request))
    db.add(r)
    db.flush()
    audit(db, request, me, "privacy_request_raised", f"request {r.id}", kind=r.kind)
    _notify(db, r)
    db.commit()
    return privacy.request_out(r, full=False)


@scoped.post("/privacy/accept")
def accept_documents(request: Request, db: Session = Depends(get_db), me: User = Depends(current_user)):
    if privacy.needs_acceptance(db, me):
        privacy.record_acceptance(db, ip=client_ip(request), user_agent=request.headers.get("user-agent", ""), user=me)
        audit(db, request, me, "legal_accepted", me.username, **privacy.versions(db))
        db.commit()
    return {"needs_acceptance": []}


# ------------------------------------------------------------------------------ organization admin
@scoped.get("/admin/privacy-requests")
def org_requests(db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.users")),
                 tenant: Tenant = Depends(current_tenant)):
    rows = db.scalars(select(PrivacyRequest).where(PrivacyRequest.tenant_id == me.tenant_id)
                      .order_by(PrivacyRequest.id.desc()).limit(500)).all()
    return [privacy.request_out(r, tenant) for r in rows]


def _update(db: Session, r: PrivacyRequest, body: UpdateIn, me: User) -> None:
    if body.status not in PRIVACY_REQUEST_STATUSES:
        raise HTTPException(422, "Unknown status")
    if body.status in ("completed", "rejected") and not body.response.strip():
        raise HTTPException(422, "Write the response sent to the person (it is kept with the request)")
    r.status, r.response = body.status, body.response.strip()
    if body.status in ("completed", "rejected"):
        r.handled_by, r.handled_at = me.id, utcnow()


@scoped.patch("/admin/privacy-requests/{rid}")
def update_org_request(rid: int, body: UpdateIn, request: Request, db: Session = Depends(get_db),
                       me: User = Depends(require_perm("tenant.users")), tenant: Tenant = Depends(current_tenant)):
    r = db.get(PrivacyRequest, rid)
    if not r or r.tenant_id != me.tenant_id:
        raise HTTPException(404, "Request not found")
    _update(db, r, body, me)
    audit(db, request, me, "privacy_request_updated", f"request {r.id}", status=r.status)
    db.commit()
    return privacy.request_out(r, tenant)


@scoped.post("/admin/users/{uid}/erase")
def erase_user(uid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.users"))):
    u = db.get(User, uid)
    if not u or u.tenant_id != me.tenant_id:
        raise HTTPException(404, "User not found")
    if u.id == me.id:
        raise HTTPException(409, "You cannot erase your own account here; raise a deletion request instead")
    if u.role == "tenant_owner" and me.role != "tenant_owner":
        raise HTTPException(403, "Only an owner can erase an owner")
    try:
        privacy.erase_user(db, u)
    except privacy.EraseRefused as e:
        raise HTTPException(409, str(e))
    audit(db, request, me, "user_erased", f"user {u.id}", user_id=u.id)
    db.commit()
    return {"id": u.id, "username": u.username, "erased": True}


# ------------------------------------------------------------------------------ platform
@platform.get("/legal")
def get_legal(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    return {"legal": privacy.legal(db), "privacy": settings_store.get(db, "privacy")}


@platform.put("/legal")
def put_legal(body: LegalIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    for k in ("contact_email", "grievance_officer_email", "dpo_email"):
        v = getattr(body, k).strip()
        if v and not EMAIL_RE.match(v):
            raise HTTPException(422, f"{k.replace('_', ' ')} is not a valid email address")
    val = body.model_dump(exclude={"publish_new_version"})
    if body.publish_new_version:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = privacy.legal(db)
        ver = today if cur["terms_version"] != today else f"{today}.{datetime.now(timezone.utc):%H%M}"
        val.update(terms_version=ver, privacy_version=ver)
    settings_store.put(db, "legal", val, me.id)
    audit(db, request, me, "platform_legal_updated", "legal", tenant_id=None, scope="platform",
          new_version=body.publish_new_version)
    db.commit()
    return {"legal": privacy.legal(db), "privacy": settings_store.get(db, "privacy")}


@platform.put("/privacy")
def put_privacy(body: PrivacySettingsIn, request: Request, db: Session = Depends(get_db),
                me: User = Depends(require_platform_admin)):
    settings_store.put(db, "privacy", body.model_dump(), me.id)
    audit(db, request, me, "platform_privacy_settings", "privacy", tenant_id=None, scope="platform", **body.model_dump())
    db.commit()
    return {"legal": privacy.legal(db), "privacy": settings_store.get(db, "privacy")}


@platform.get("/privacy-requests")
def all_requests(status: str = "open", db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    q = select(PrivacyRequest).order_by(PrivacyRequest.id.desc()).limit(500)
    if status == "open":
        q = q.where(PrivacyRequest.status.in_(("open", "in_progress")))
    elif status != "all":
        q = q.where(PrivacyRequest.status == status)
    rows = db.scalars(q).all()
    tenants = {t.id: t for t in db.scalars(select(Tenant)).all()}
    return [privacy.request_out(r, tenants.get(r.tenant_id)) for r in rows]


@platform.patch("/privacy-requests/{rid}")
def update_any_request(rid: int, body: UpdateIn, request: Request, db: Session = Depends(get_db),
                       me: User = Depends(require_platform_admin)):
    r = db.get(PrivacyRequest, rid)
    if not r:
        raise HTTPException(404, "Request not found")
    _update(db, r, body, me)
    audit(db, request, me, "privacy_request_updated", f"request {r.id}", tenant_id=r.tenant_id, scope="platform",
          status=r.status)
    db.commit()
    return privacy.request_out(r, db.get(Tenant, r.tenant_id) if r.tenant_id else None)
