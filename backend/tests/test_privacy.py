"""Release 1.3: notices, consent records, data-subject requests, export, erasure, retention."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from testpilot import privacy
from testpilot.db import session_scope
from testpilot.models import AuditLog, LegalAcceptance, PrivacyRequest, TenantRegistration, User, utcnow

from .conftest import H, login, make_tenant

REG = {"org_name": "Globex", "slug": "globex", "contact_name": "Hank", "contact_email": "hank@globex.example",
       "accept_terms": True, "accept_privacy": True, "confirm_adult": True, "country_code": "US", "country": "United States"}


def anon(app) -> TestClient:
    c = TestClient(app, base_url="https://testingagent.unical.in")
    c.headers.update(H)
    return c


def test_registration_needs_separate_consents_and_records_them(app):
    a = anon(app)
    for k in ("accept_terms", "accept_privacy", "confirm_adult"):
        assert a.post("/api/public/registrations", json={**REG, k: False}).status_code == 422, k
    assert a.post("/api/public/registrations", json={**REG, "country_code": "XX"}).status_code == 422
    assert a.post("/api/public/registrations", json=REG).status_code == 202
    with session_scope() as db:
        reg = db.scalars(select(TenantRegistration)).one()
        assert reg.country_code == "US" and reg.country == "United States"
        acc = db.scalars(select(LegalAcceptance).where(LegalAcceptance.registration_id == reg.id)).all()
        assert {x.document for x in acc} == {"terms", "privacy"} and all(x.version for x in acc)


def test_users_accept_current_documents_and_again_after_a_new_version(app, admin):
    u = login(app, "tess", "tester")
    assert set(u.get("/api/auth/me").json()["legal_pending"]) == {"terms", "privacy"}
    assert u.post("/api/privacy/accept").status_code == 200
    assert u.get("/api/auth/me").json()["legal_pending"] == []
    legal = admin.get("/api/platform/legal").json()["legal"]
    body = {k: legal[k] for k in ("entity_name", "address", "contact_email", "grievance_officer_name",
                                  "grievance_officer_email", "grievance_officer_phone", "dpo_email",
                                  "uk_representative", "us_contact", "hosting_location")}
    assert admin.put("/api/platform/legal", json={**body, "entity_name": "Unical Systems"}).status_code == 200
    assert u.get("/api/auth/me").json()["legal_pending"] == []  # wording edit only
    assert admin.put("/api/platform/legal", json={**body, "publish_new_version": True}).status_code == 200
    assert set(u.get("/api/auth/me").json()["legal_pending"]) == {"terms", "privacy"}
    pub = anon(app).get("/api/public/legal").json()
    assert pub["entity_name"] == "Unical Systems" and pub["terms_version"] == pub["privacy_version"]


def test_export_contains_my_data_only(app, admin):
    u = login(app, "tess", "tester")
    u.post("/api/privacy/accept")
    r = u.get("/api/privacy/me/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["account"]["username"] == "tess" and d["legal_acceptances"]
    assert "password_hash" not in r.text and "$argon2" not in r.text and "$2b$" not in r.text
    assert "token" not in d["active_sessions"][0]


def test_requests_flow_to_organization_and_platform(app, admin):
    u = login(app, "tess", "tester")
    assert u.post("/api/privacy/requests", json={"kind": "bogus"}).status_code == 422
    r = u.post("/api/privacy/requests", json={"kind": "correction", "details": "Fix my name"})
    assert r.status_code == 201 and r.json()["status"] == "open"
    assert u.get("/api/admin/privacy-requests").status_code == 403
    rows = admin.get("/api/admin/privacy-requests").json()
    assert rows[0]["kind"] == "correction" and rows[0]["requester_name"] == "tess"
    assert admin.patch(f"/api/admin/privacy-requests/{rows[0]['id']}", json={"status": "completed"}).status_code == 422
    assert admin.patch(f"/api/admin/privacy-requests/{rows[0]['id']}",
                       json={"status": "completed", "response": "Updated."}).json()["status"] == "completed"
    assert u.get("/api/privacy/me").json()["requests"][0]["response"] == "Updated."
    assert admin.get("/api/platform/privacy-requests?status=all").json()[0]["organization"]["slug"] == "unical"
    # another organization never sees it
    make_tenant("acme")
    other = login(app, "o", "tenant_admin", tenant="acme")
    assert other.get("/api/t/acme/admin/privacy-requests").json() == []
    assert other.patch(f"/api/t/acme/admin/privacy-requests/{rows[0]['id']}",
                       json={"status": "rejected", "response": "x"}).status_code == 404


def test_public_request_and_rate_limit(app, admin):
    a = anon(app)
    body = {"name": "Hank", "email": "hank@globex.example", "kind": "erasure", "details": "Delete my registration"}
    r = a.post("/api/public/privacy-requests", json=body)
    assert r.status_code == 201 and "reference" in r.json()["message"]
    for _ in range(4):
        a.post("/api/public/privacy-requests", json=body)
    assert a.post("/api/public/privacy-requests", json=body).status_code == 429
    assert len(admin.get("/api/platform/privacy-requests").json()) == 5


def test_erasure_anonymises_account(app, admin):
    u = login(app, "tess", "tester")
    uid = u.get("/api/auth/me").json()["id"]
    assert admin.post(f"/api/admin/users/{uid}/erase").json()["username"] == f"erased-{uid}"
    assert u.get("/api/auth/me").status_code == 401  # sessions gone
    with session_scope() as db:
        x = db.get(User, uid)
        assert not x.is_active and x.email == "" and x.display_name == ""
    me = admin.get("/api/auth/me").json()
    assert admin.post(f"/api/admin/users/{me['id']}/erase").status_code == 409  # not yourself


def test_only_owner_cannot_be_erased(app):
    make_tenant("acme")
    owner = login(app, "own", "tenant_owner", tenant="acme")
    adm = login(app, "adm", "tenant_admin", tenant="acme")
    oid = owner.get("/api/t/acme/auth/me").json()["id"]
    assert adm.post(f"/api/t/acme/admin/users/{oid}/erase").status_code == 403


def test_retention_purge(admin):
    with session_scope() as db:
        db.add(AuditLog(action="old", scope="tenant", tenant_id=1, created_at=utcnow() - timedelta(days=800)))
        db.add(AuditLog(action="recent", scope="tenant", tenant_id=1))
    out = privacy.purge_expired()
    assert out["audit"] >= 1
    with session_scope() as db:
        acts = {a.action for a in db.scalars(select(AuditLog)).all()}
        assert "old" not in acts and "recent" in acts


def test_privacy_settings_validation(admin):
    r = admin.put("/api/platform/privacy", json={"request_response_days": 200, "audit_retention_days": 1,
                                                 "registration_retention_days": 1, "session_retention_days": 1})
    assert r.status_code == 422
    r = admin.put("/api/platform/privacy", json={"request_response_days": 30, "audit_retention_days": 365,
                                                 "registration_retention_days": 180, "session_retention_days": 30})
    assert r.status_code == 200 and r.json()["privacy"]["audit_retention_days"] == 365
