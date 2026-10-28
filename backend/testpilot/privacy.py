"""Personal-data rights and retention (India DPDP Act 2023 / DPDP Rules 2025, UK GDPR & DPA 2018,
US state privacy laws such as CCPA/CPRA).

  * notice & consent: versioned Terms and Privacy Policy; every acceptance is recorded (who, version,
    when, IP) and a new version makes signed-in users accept again
  * rights: access/portability (self-service export), correction, erasure, withdrawal of consent,
    restriction/objection, opt-out of sale/sharing, nomination (DPDP s.14), grievance — tracked as
    privacy requests with a response deadline
  * erasure: an account is anonymised (identifiers removed, sign-in impossible); audit entries keep
    the numeric id only
  * retention: audit log, rejected registrations, expired sessions and sign-in transactions are
    deleted after configurable periods
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import settings_store
from .db import session_scope
from .models import (
    AuditLog,
    CaptchaTokenSeen,
    ExternalIdentity,
    GroupMember,
    Invitation,
    LegalAcceptance,
    OidcTransaction,
    PasswordReset,
    PrivacyRequest,
    ProjectAssignment,
    Session_,
    Tenant,
    TenantRegistration,
    User,
    utcnow,
)
from .security import hash_password

log = logging.getLogger("testpilot.privacy")

KIND_LABELS = {
    "access": "Access my personal data",
    "export": "Receive a copy of my data (portability)",
    "correction": "Correct or update my data",
    "erasure": "Delete my account and personal data",
    "withdraw_consent": "Withdraw consent",
    "restrict": "Restrict or object to processing",
    "opt_out": "Opt out of sale / sharing / targeted advertising",
    "nominate": "Nominate a person to exercise my rights (DPDP Act)",
    "grievance": "Raise a grievance or complaint",
}
DOCUMENTS = ("terms", "privacy")


def legal(db: Session) -> dict[str, Any]:
    return settings_store.get(db, "legal")


def versions(db: Session) -> dict[str, str]:
    l = legal(db)
    return {"terms": l["terms_version"], "privacy": l["privacy_version"]}


def record_acceptance(db: Session, *, ip: str, user_agent: str = "", user: User | None = None,
                      registration_id: int | None = None, tenant_id: int | None = None) -> None:
    for doc, ver in versions(db).items():
        db.add(LegalAcceptance(user_id=user.id if user else None, tenant_id=user.tenant_id if user else tenant_id,
                               registration_id=registration_id, document=doc, version=ver, ip=ip[:64],
                               user_agent=(user_agent or "")[:300]))


def needs_acceptance(db: Session, user: User) -> list[str]:
    """Documents whose current version this user has not accepted yet."""
    out = []
    for doc, ver in versions(db).items():
        if not db.scalar(select(LegalAcceptance.id).where(LegalAcceptance.user_id == user.id,
                                                          LegalAcceptance.document == doc,
                                                          LegalAcceptance.version == ver).limit(1)):
            out.append(doc)
    return out


def due_date(db: Session):
    return utcnow() + timedelta(days=int(settings_store.get(db, "privacy")["request_response_days"]))


def request_out(r: PrivacyRequest, tenant: Tenant | None = None, full: bool = True) -> dict[str, Any]:
    out = {"id": r.id, "kind": r.kind, "kind_label": KIND_LABELS.get(r.kind, r.kind), "status": r.status,
           "created_at": r.created_at, "due_at": r.due_at, "handled_at": r.handled_at, "response": r.response,
           "overdue": r.status in ("open", "in_progress") and r.due_at < utcnow()}
    if full:
        out.update(requester_name=r.requester_name, requester_email=r.requester_email, details=r.details,
                   user_id=r.user_id, organization=({"slug": tenant.slug, "name": tenant.name} if tenant else None))
    return out


def export_user(db: Session, user: User) -> dict[str, Any]:
    """Everything TestAgent holds about the signed-in person (account, identities, access, consents,
    their own activity log and requests). Test artifacts belong to the organization and are excluded."""
    t = db.get(Tenant, user.tenant_id)
    ids = db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)).all()
    groups = db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user.id)).all()
    assigns = db.scalars(select(ProjectAssignment).where(ProjectAssignment.user_id == user.id)).all()
    acc = db.scalars(select(LegalAcceptance).where(LegalAcceptance.user_id == user.id).order_by(LegalAcceptance.id)).all()
    sess = db.scalars(select(Session_).where(Session_.user_id == user.id)).all()
    reqs = db.scalars(select(PrivacyRequest).where(PrivacyRequest.user_id == user.id)).all()
    audit = db.scalars(select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.id.desc()).limit(1000)).all()
    return {
        "format": "testpilot.personal_data_export", "generated_at": utcnow().isoformat(timespec="seconds"),
        "controller_notice": "Exported at your request. Test cases, runs and evidence belong to your organization "
                             "and can be exported by its administrators.",
        "account": {"id": user.id, "username": user.username, "display_name": user.display_name, "email": user.email,
                    "role": user.role, "active": user.is_active, "created_at": user.created_at,
                    "last_login_at": user.last_login_at,
                    "organization": {"slug": t.slug, "name": t.name} if t else None},
        "linked_identities": [{"provider": i.provider, "issuer": i.issuer, "email_at_link": i.email_at_link} for i in ids],
        "group_ids": list(groups),
        "project_assignments": [{"scope": a.scope, "project_id": a.project_id, "role": a.role, "starts_at": a.starts_at,
                                 "ends_at": a.ends_at, "status": a.status} for a in assigns],
        "legal_acceptances": [{"document": a.document, "version": a.version, "accepted_at": a.accepted_at, "ip": a.ip}
                              for a in acc],
        "active_sessions": [{"created_at": s.created_at, "last_seen_at": s.last_seen_at, "ip": s.ip,
                             "user_agent": s.user_agent, "method": s.auth_method} for s in sess],
        "privacy_requests": [request_out(r, full=False) | {"details": r.details} for r in reqs],
        "activity_log": [{"at": a.created_at, "action": a.action, "target": a.target, "ip": a.ip} for a in audit],
    }


class EraseRefused(ValueError):
    pass


def erase_user(db: Session, user: User) -> None:
    """Anonymises an account: identifiers removed, sign-in impossible, links and access revoked.
    Numeric ids stay so that audit history remains consistent."""
    if user.role == "tenant_owner":
        owners = db.scalar(select(func.count()).select_from(User).where(
            User.tenant_id == user.tenant_id, User.role == "tenant_owner", User.is_active.is_(True))) or 0
        if owners <= 1:
            raise EraseRefused("This is the organization's only owner. Make someone else owner first.")
    if user.is_platform_admin:
        raise EraseRefused("Revoke this person's Unical Super Admin role first (python -m testpilot.cli platform-admins).")
    db.execute(delete(Session_).where(Session_.user_id == user.id))
    db.execute(delete(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
    db.execute(delete(GroupMember).where(GroupMember.user_id == user.id))
    db.execute(delete(PasswordReset).where(PasswordReset.user_id == user.id))
    for a in db.scalars(select(ProjectAssignment).where(ProjectAssignment.user_id == user.id,
                                                        ProjectAssignment.status == "active")).all():
        a.status = "revoked"
    for a in db.scalars(select(AuditLog).where(AuditLog.user_id == user.id, AuditLog.target == user.username)).all():
        a.target = f"erased-{user.id}"
    for a in db.scalars(select(AuditLog).where(AuditLog.tenant_id == user.tenant_id, AuditLog.target == user.username)).all():
        a.target = f"erased-{user.id}"
    if user.email:
        for inv in db.scalars(select(Invitation).where(Invitation.tenant_id == user.tenant_id,
                                                       func.lower(Invitation.email) == user.email.lower())).all():
            inv.email = ""
    user.username = f"erased-{user.id}"
    user.display_name, user.email = "", ""
    user.password_hash = hash_password(secrets.token_urlsafe(48))
    user.is_active, user.must_change_password = False, False
    for r in db.scalars(select(PrivacyRequest).where(PrivacyRequest.user_id == user.id)).all():
        r.requester_name, r.requester_email = "", ""
    for a in db.scalars(select(LegalAcceptance).where(LegalAcceptance.user_id == user.id)).all():
        a.ip, a.user_agent = "", ""


def purge_expired() -> dict[str, int]:
    """Daily retention job."""
    out: dict[str, int] = {}
    with session_scope() as db:
        p = settings_store.get(db, "privacy")
        now = utcnow()
        if int(p["audit_retention_days"]) > 0:
            out["audit"] = db.execute(delete(AuditLog).where(
                AuditLog.created_at < now - timedelta(days=int(p["audit_retention_days"])))).rowcount or 0
        if int(p["registration_retention_days"]) > 0:
            old = now - timedelta(days=int(p["registration_retention_days"]))
            out["registrations"] = db.execute(delete(TenantRegistration).where(
                TenantRegistration.status == "rejected", TenantRegistration.created_at < old)).rowcount or 0
        sess_days = int(p["session_retention_days"]) or 30
        out["sessions"] = db.execute(delete(Session_).where(Session_.expires_at < now - timedelta(days=sess_days))).rowcount or 0
        out["oidc"] = db.execute(delete(OidcTransaction).where(OidcTransaction.expires_at < now - timedelta(days=1))).rowcount or 0
        out["resets"] = db.execute(delete(PasswordReset).where(PasswordReset.expires_at < now - timedelta(days=7))).rowcount or 0
        out["captcha"] = db.execute(delete(CaptchaTokenSeen).where(CaptchaTokenSeen.seen_at < now - timedelta(days=2))).rowcount or 0
        out["invitations"] = db.execute(delete(Invitation).where(Invitation.expires_at < now - timedelta(days=90))).rowcount or 0
    if any(out.values()):
        log.info("retention purge: %s", out)
    return out
