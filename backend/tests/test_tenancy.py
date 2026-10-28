"""Multi-tenancy: registration and approval, tenant isolation, RBAC, branding, suspension.

Two customer tenants ("acme", "globex") are created alongside the default "unical" tenant. Every
cross-tenant probe must fail with 401 (wrong-tenant session) or 404 (other tenant's object id).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from testpilot import worker
from testpilot.db import session_scope
from testpilot.models import AuditLog, Evidence, Invitation, Project, Run, Tenant, TestCase, User

from .conftest import H, allow, login, make_tenant, tenant_id

PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def client(app) -> TestClient:
    c = TestClient(app, base_url="https://testingagent.unical.in")
    c.headers.update(H)
    return c


# ============================================================================ registration
REG = {"org_name": "Acme Technologies", "slug": "acme-technologies", "contact_name": "Asha Rao",
       "contact_email": "asha@acme.example", "accept_terms": True, "accept_privacy": True, "confirm_adult": True,
       "country_code": "IN", "country": "India"}


def test_registration_requires_approval_then_owner_activates(app, admin):
    anon = client(app)
    r = anon.post("/api/public/registrations", json=REG)
    assert r.status_code == 202, r.text
    # Pending: the tenant does not exist yet, its URL is unknown and no one can sign in.
    assert anon.get("/api/public/tenants/acme-technologies/branding").status_code == 404
    assert anon.post("/api/t/acme-technologies/auth/login", json={"username": "x", "password": "y"}).status_code == 404
    with session_scope() as db:
        assert db.scalars(select(Tenant).where(Tenant.slug == "acme-technologies")).first() is None

    regs = admin.get("/api/platform/registrations").json()
    assert [x["requested_slug"] for x in regs] == ["acme-technologies"]
    ok = admin.post(f"/api/platform/registrations/{regs[0]['id']}/approve", json={})
    assert ok.status_code == 200, ok.text
    link = ok.json()["owner_invitation"]["link"]
    assert link.startswith("https://testingagent.unical.in/acme-technologies/activate#")
    token = link.split("#", 1)[1]

    # approval is final
    assert admin.post(f"/api/platform/registrations/{regs[0]['id']}/approve", json={}).status_code == 409
    assert anon.get("/api/public/tenants/acme-technologies/branding").json()["display_name"] == "Acme Technologies"

    info = anon.post("/api/public/invitations/inspect", json={"token": token}).json()
    assert info["role"] == "tenant_owner" and info["email"] == "asha@acme.example"
    owner = client(app)
    r = owner.post("/api/public/invitations/accept",
                   json={"token": token, "username": "asha", "password": "Owner-pass-1234"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "tenant_owner" and r.json()["tenant"]["slug"] == "acme-technologies"
    me = owner.get("/api/t/acme-technologies/auth/me")
    assert me.status_code == 200 and "tenant.users" in me.json()["permissions"]
    # single use
    assert client(app).post("/api/public/invitations/accept",
                            json={"token": token, "username": "mallory", "password": "Mallory-pass-123"}).status_code == 404
    assert client(app).post("/api/public/invitations/inspect", json={"token": token}).status_code == 404

    with session_scope() as db:
        actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.scope == "platform")).all()]
    assert "platform_registration_received" in actions and "platform_registration_approved" in actions


def test_registration_rejects_reserved_and_duplicate_slugs(app, admin):
    anon = client(app)
    for slug in ("api", "platform", "unical", "assets", "login", "x", "Bad Slug", "a--b", "-abc"):
        r = anon.post("/api/public/registrations", json={**REG, "slug": slug})
        assert r.status_code in (409, 422), (slug, r.text)
    assert anon.post("/api/public/registrations", json=REG).status_code == 202
    # same slug again (other email): not available
    r = anon.post("/api/public/registrations", json={**REG, "contact_email": "b@other.example"})
    assert r.status_code == 409
    # same email again (other slug): acknowledged identically but not stored (no enumeration)
    r = anon.post("/api/public/registrations", json={**REG, "slug": "acme-two"})
    assert r.status_code == 202
    assert len(admin.get("/api/platform/registrations").json()) == 1
    # existing tenant slug
    make_tenant("globex")
    assert anon.post("/api/public/registrations",
                     json={**REG, "slug": "globex", "contact_email": "c@globex.example"}).status_code == 409
    assert anon.get("/api/public/slug-check?slug=globex").json()["available"] is False
    assert anon.get("/api/public/slug-check?slug=initech").json()["available"] is True


def test_registration_requires_terms_and_is_rate_limited(app, monkeypatch):
    from testpilot.config import get_settings
    anon = client(app)
    assert anon.post("/api/public/registrations", json={**REG, "accept_terms": False}).status_code == 422
    monkeypatch.setattr(get_settings(), "registrations_per_ip_per_day", 2)
    codes = [anon.post("/api/public/registrations",
                       json={**REG, "slug": f"org-{i}x", "contact_email": f"u{i}@x.example"}).status_code for i in range(3)]
    assert codes == [202, 202, 429]


def test_reject_and_platform_access_is_super_admin_only(app, admin):
    anon = client(app)
    anon.post("/api/public/registrations", json=REG)
    rid = admin.get("/api/platform/registrations").json()[0]["id"]
    # a Unical tenant admin who is not a Super Admin
    ua = login(app, "ops", "admin", platform=False)
    assert ua.get("/api/platform/registrations").status_code == 403
    assert ua.post(f"/api/platform/registrations/{rid}/approve", json={}).status_code == 403
    assert anon.get("/api/platform/tenants").status_code == 401
    # a customer tenant owner cannot reach the platform API (session belongs to another tenant)
    make_tenant("globex")
    gowner = login(app, "gina", "tenant_owner", tenant="globex", platform=False)
    assert gowner.get("/api/platform/registrations").status_code == 401
    r = admin.post(f"/api/platform/registrations/{rid}/reject", json={"note": "duplicate"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert anon.get("/api/public/tenants/acme-technologies/branding").status_code == 404


def test_expired_invitation_is_refused(app, admin):
    client(app).post("/api/public/registrations", json=REG)
    rid = admin.get("/api/platform/registrations").json()[0]["id"]
    token = admin.post(f"/api/platform/registrations/{rid}/approve", json={}).json()["owner_invitation"]["link"].split("#")[1]
    with session_scope() as db:
        db.execute(update(Invitation).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    r = client(app).post("/api/public/invitations/accept", json={"token": token, "username": "asha",
                                                                  "password": "Owner-pass-1234"})
    assert r.status_code == 404


# ============================================================================ isolation
@pytest.fixture
def two_tenants(app):
    """acme and globex, each with an admin, a project, a case, a secret, a finished run and evidence."""
    make_tenant("acme")
    make_tenant("globex")
    out = {}
    for slug in ("acme", "globex"):
        allow("app.example.com", tenant=slug)
        c = login(app, f"{slug}-admin", "tenant_admin", tenant=slug, platform=False)
        base = f"/api/t/{slug}"
        p = c.post(f"{base}/projects", json={"name": f"{slug} portal", "base_url": "https://app.example.com/"})
        assert p.status_code == 201, p.text
        pid = p.json()["id"]
        assert c.put(f"{base}/projects/{pid}/secrets", json={"name": "PW", "value": f"{slug}-secret"}).status_code == 200
        case = c.post(f"{base}/projects/{pid}/cases", json={"name": "home", "steps": [{"action": "goto", "url": "/"}]}).json()
        run = c.post(f"{base}/cases/{case['id']}/runs").json()
        tid = tenant_id(slug)
        with session_scope() as db:
            db.execute(update(Run).where(Run.id == run["id"]).values(status="passed", finished_at=datetime.now(timezone.utc)))
            ev = Evidence(tenant_id=tid, project_id=pid, run_id=run["id"], kind="log", rel_path="tenants/x/run.log",
                          mime="text/plain")
            db.add(ev)
            db.flush()
            eid = ev.id
        out[slug] = {"c": c, "base": base, "pid": pid, "cid": case["id"], "rid": run["id"], "eid": eid, "tid": tid}
    return out


def test_tenant_sees_only_its_own_data(two_tenants):
    a, b = two_tenants["acme"], two_tenants["globex"]
    c, base = a["c"], a["base"]
    assert [p["name"] for p in c.get(f"{base}/projects").json()] == ["acme portal"]
    probes = [
        ("GET", f"/projects/{b['pid']}"), ("PUT", f"/projects/{b['pid']}"), ("DELETE", f"/projects/{b['pid']}"),
        ("GET", f"/projects/{b['pid']}/secrets"), ("PUT", f"/projects/{b['pid']}/secrets"),
        ("DELETE", f"/projects/{b['pid']}/secrets/PW"),
        ("GET", f"/projects/{b['pid']}/cases"), ("POST", f"/projects/{b['pid']}/cases"),
        ("GET", f"/cases/{b['cid']}"), ("PUT", f"/cases/{b['cid']}"), ("DELETE", f"/cases/{b['cid']}"),
        ("POST", f"/cases/{b['cid']}/runs"),
        ("GET", f"/runs/{b['rid']}"), ("POST", f"/runs/{b['rid']}/cancel"), ("POST", f"/runs/{b['rid']}/rerun"),
        ("GET", f"/runs/{b['rid']}/report"), ("GET", f"/runs/{b['rid']}/report?format=json"),
        ("POST", f"/runs/{b['rid']}/ai/analyze"), ("GET", f"/evidence/{b['eid']}"),
        ("POST", f"/projects/{b['pid']}/ai/generate"),
    ]
    bodies = {"PUT": {"name": "x", "base_url": "https://app.example.com/", "value": "v", "steps": []},
              "POST": {"name": "x", "steps": [{"action": "goto", "url": "/"}], "requirement": "Users can sign in"}}
    for method, path in probes:
        r = c.request(method, base + path, json=bodies.get(method))
        assert r.status_code in (404, 422), (method, path, r.status_code, r.text)
        if r.status_code == 422:  # validation may run first; it must never leak the object
            assert "globex" not in r.text
    # runs list for another tenant's project: not found (1.1 returned an empty list)
    assert c.get(f"{base}/projects/{b['pid']}/runs").status_code == 404
    # nothing of globex changed
    with session_scope() as db:
        p = db.get(Project, b["pid"])
        assert p.name == "globex portal" and not p.archived
        assert not db.get(TestCase, b["cid"]).archived
        assert db.get(Run, b["rid"]).status == "passed"


def test_session_is_bound_to_its_tenant(two_tenants):
    a, b = two_tenants["acme"], two_tenants["globex"]
    c = a["c"]
    assert c.get(f"{b['base']}/projects").status_code == 401
    assert c.get(f"{b['base']}/auth/me").status_code == 401
    assert c.get("/api/projects").status_code == 401           # legacy routes == default tenant
    assert c.get("/api/t/does-not-exist/projects").status_code == 404
    assert c.get(f"{a['base']}/auth/me").json()["tenant"]["slug"] == "acme"


def test_username_is_per_tenant_and_login_is_scoped(app, two_tenants):
    from .conftest import make_user
    make_user("sam", "tester", tenant="acme", password="Acme-pass-12345")
    make_user("sam", "tester", tenant="globex", password="Globex-pass-1234")
    c = client(app)
    assert c.post("/api/t/acme/auth/login", json={"username": "sam", "password": "Globex-pass-1234"}).status_code == 401
    assert c.post("/api/t/globex/auth/login", json={"username": "sam", "password": "Globex-pass-1234"}).status_code == 200
    assert c.post("/api/auth/login", json={"username": "sam", "password": "Globex-pass-1234"}).status_code == 401


def test_admin_functions_are_tenant_scoped(two_tenants):
    a, b = two_tenants["acme"], two_tenants["globex"]
    c, base = a["c"], a["base"]
    assert [u["username"] for u in c.get(f"{base}/admin/users").json()] == ["acme-admin"]
    with session_scope() as db:
        gid = db.scalars(select(User.id).where(User.username == "globex-admin")).one()
    assert c.patch(f"{base}/admin/users/{gid}", json={"is_active": False}).status_code == 404
    assert [t["host"] for t in c.get(f"{base}/admin/targets").json()] == ["app.example.com"]
    gt = b["c"].get(f"{b['base']}/admin/targets").json()[0]["id"]
    assert c.delete(f"{base}/admin/targets/{gt}").status_code == 404
    audit = c.get(f"{base}/admin/audit").json()
    assert audit and all("globex" not in (x["user"] or "") for x in audit)
    with session_scope() as db:
        assert db.get(User, gid).is_active


def test_client_supplied_tenant_id_is_ignored(two_tenants):
    a, b = two_tenants["acme"], two_tenants["globex"]
    r = a["c"].post(f"{a['base']}/projects", json={"name": "sneaky", "base_url": "https://app.example.com/",
                                                   "tenant_id": b["tid"]})
    assert r.status_code == 201
    with session_scope() as db:
        assert db.get(Project, r.json()["id"]).tenant_id == a["tid"]


def test_another_tenants_target_approval_does_not_apply(app, two_tenants):
    a = two_tenants["acme"]
    allow("only-globex.example.com", tenant="globex")
    p = a["c"].post(f"{a['base']}/projects", json={"name": "x", "base_url": "https://only-globex.example.com/"}).json()
    assert any("only-globex.example.com" in x for x in p["policy_problems"])
    case = a["c"].post(f"{a['base']}/projects/{p['id']}/cases", json={"name": "c", "steps": [{"action": "goto", "url": "/"}]}).json()
    assert a["c"].post(f"{a['base']}/cases/{case['id']}/runs").status_code == 422


def test_database_refuses_cross_tenant_children(two_tenants):
    a, b = two_tenants["acme"], two_tenants["globex"]
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(TestCase(tenant_id=a["tid"], project_id=b["pid"], name="x", steps=[]))
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(Evidence(tenant_id=a["tid"], project_id=a["pid"], run_id=b["rid"], kind="log", rel_path="x", mime="t"))


def test_system_status_shows_only_own_queue(two_tenants):
    a = two_tenants["acme"]
    st = a["c"].get(f"{a['base']}/system/status").json()
    assert st["queue"] == {"queued": 0, "running": 0}
    assert all("current_run_id" not in w for w in st["workers"])


# ============================================================================ RBAC
@pytest.mark.parametrize("role,allowed", [
    ("tenant_owner", {"project", "case", "run", "users", "targets", "branding", "audit"}),
    ("tenant_admin", {"project", "case", "run", "users", "targets", "branding", "audit"}),
    ("project_manager", {"project", "case", "run"}),
    ("qa_engineer", {"project", "case", "run"}),
    ("developer", {"case", "run"}),
    ("auditor", {"audit"}),
])
def test_role_permissions_enforced_in_backend(app, role, allowed):
    make_tenant("acme")
    allow("app.example.com", tenant="acme")
    boss = login(app, "boss", "tenant_owner", tenant="acme", platform=False)
    base = "/api/t/acme"
    pid = boss.post(f"{base}/projects", json={"name": "p", "base_url": "https://app.example.com/"}).json()["id"]
    cid = boss.post(f"{base}/projects/{pid}/cases", json={"name": "c", "steps": [{"action": "goto", "url": "/"}]}).json()["id"]
    c = login(app, f"u-{role}", role, tenant="acme", platform=False)
    checks = {
        "project": c.post(f"{base}/projects", json={"name": "q", "base_url": "https://app.example.com/"}),
        "case": c.post(f"{base}/projects/{pid}/cases", json={"name": "d", "steps": [{"action": "goto", "url": "/"}]}),
        "run": c.post(f"{base}/cases/{cid}/runs"),
        "users": c.get(f"{base}/admin/users"),
        "targets": c.post(f"{base}/admin/targets", json={"host": f"{role.replace('_', '-')}.example.com"}),
        "branding": c.get(f"{base}/admin/branding"),
        "audit": c.get(f"{base}/admin/audit"),
    }
    for name, r in checks.items():
        if name in allowed:
            assert r.status_code in (200, 201), (role, name, r.text)
        else:
            assert r.status_code == 403, (role, name, r.status_code)
    # everyone can read
    assert c.get(f"{base}/projects/{pid}").status_code == 200


def test_only_owner_can_grant_owner(app):
    make_tenant("acme")
    owner = login(app, "own", "tenant_owner", tenant="acme", platform=False)
    adm = login(app, "adm", "tenant_admin", tenant="acme", platform=False)
    base = "/api/t/acme"
    body = {"username": "newbie", "role": "tenant_owner", "password": "Newbie-pass-123"}
    assert adm.post(f"{base}/admin/users", json=body).status_code == 403
    assert adm.post(f"{base}/admin/invitations", json={"email": "x@acme.example", "role": "tenant_owner"}).status_code == 403
    assert owner.post(f"{base}/admin/users", json=body).status_code == 201
    oid = [u for u in owner.get(f"{base}/admin/users").json() if u["username"] == "own"][0]["id"]
    assert adm.patch(f"{base}/admin/users/{oid}", json={"is_active": False}).status_code == 403
    # legacy role names are still accepted
    assert adm.post(f"{base}/admin/users", json={"username": "old", "role": "tester",
                                                 "password": "Oldrole-pass-123"}).json()["role"] == "qa_engineer"


def test_tenant_invitation_flow(app):
    make_tenant("acme")
    adm = login(app, "adm", "tenant_admin", tenant="acme", platform=False)
    r = adm.post("/api/t/acme/admin/invitations", json={"email": "dev@acme.example", "role": "developer"})
    assert r.status_code == 201
    token = r.json()["link"].split("#")[1]
    new = client(app)
    assert new.post("/api/public/invitations/accept",
                    json={"token": token, "username": "dev1", "password": "Dev-pass-123456"}).json()["role"] == "developer"
    inv2 = adm.post("/api/t/acme/admin/invitations", json={"email": "x@acme.example", "role": "auditor"}).json()
    assert adm.delete(f"/api/t/acme/admin/invitations/{inv2['id']}").json()["state"] == "revoked"
    assert client(app).post("/api/public/invitations/accept",
                            json={"token": inv2["link"].split("#")[1], "username": "aud",
                                  "password": "Aud-pass-1234567"}).status_code == 404


# ============================================================================ branding
def test_branding_validation_and_public_read(app):
    make_tenant("acme", "Acme")
    adm = login(app, "adm", "tenant_admin", tenant="acme", platform=False)
    url = "/api/t/acme/admin/branding"
    good = {"display_name": "Acme Technologies", "primary_color": "#112233", "welcome_text": "Welcome to Acme QA",
            "support_email": "qa@acme.example", "support_url": "https://acme.example/help",
            "logo_data_url": "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()}
    assert adm.put(url, json=good).status_code == 200
    for bad in ({"primary_color": "red"}, {"logo_data_url": "data:image/svg+xml;base64,PHN2Zy8+"},
                {"logo_data_url": "data:image/png;base64," + base64.b64encode(b"<script>").decode()},
                {"support_url": "javascript:alert(1)"}, {"support_email": "nope"}):
        assert adm.put(url, json={**good, **bad}).status_code == 422, bad
    pub = client(app).get("/api/public/tenants/acme/branding").json()
    assert pub["display_name"] == "Acme Technologies" and pub["primary_color"] == "#112233"
    assert set(pub) == {"slug", "name", "display_name", "logo_data_url", "primary_color", "welcome_text",
                        "support_email", "support_url"}


# ============================================================================ suspension
def test_suspended_tenant_is_locked_out_and_its_runs_wait(app, admin, two_tenants):
    a = two_tenants["acme"]
    c, base = a["c"], a["base"]
    allow("127.0.0.1", tenant="acme")
    with session_scope() as db:
        db.execute(update(Run).values(status="passed"))
    case = c.post(f"{base}/projects/{a['pid']}/cases", json={"name": "q", "steps": [{"action": "goto", "url": "/"}]}).json()
    queued = c.post(f"{base}/cases/{case['id']}/runs").json()["id"]
    r = admin.post(f"/api/platform/tenants/{a['tid']}/suspend", json={"reason": "non-payment"})
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert c.get(f"{base}/projects").status_code == 403
    assert client(app).post(f"{base}/auth/login", json={"username": "acme-admin", "password": "Password-12345"}).status_code == 403
    assert client(app).get("/api/public/tenants/acme/branding").status_code == 404
    assert worker.claim("pytest") is None  # acme's queued run is not executed
    with session_scope() as db:
        assert db.get(Run, queued).status == "queued"
    # the platform org itself cannot be suspended
    assert admin.post(f"/api/platform/tenants/{tenant_id('unical')}/suspend", json={}).status_code == 400
    assert admin.post(f"/api/platform/tenants/{a['tid']}/reactivate", json={}).status_code == 200
    assert worker.claim("pytest") == queued
    # sessions were revoked on suspension: sign in again
    assert c.get(f"{base}/projects").status_code == 401


def test_platform_view_has_metadata_only(admin, two_tenants):
    tenants = admin.get("/api/platform/tenants").json()
    acme = next(t for t in tenants if t["slug"] == "acme")
    assert acme["usage"] == {"users": 1, "projects": 1, "runs": 1}
    assert set(acme) == {"id", "slug", "name", "status", "status_reason", "created_at", "usage"}
    # the Super Admin's session is the Unical tenant's: customer data stays out of reach
    assert admin.get(f"/api/t/acme/projects").status_code == 401
    assert admin.get(f"/api/projects/{two_tenants['acme']['pid']}").status_code == 404
    assert admin.get(f"/api/runs/{two_tenants['acme']['rid']}").status_code == 404
    assert admin.get(f"/api/evidence/{two_tenants['acme']['eid']}").status_code == 404


# ============================================================================ review findings
def test_tenant_admin_cannot_take_over_super_admin(app, admin):
    ops = login(app, "ops", "admin", platform=False)  # Unical tenant admin, not a Super Admin
    with session_scope() as db:
        sa_id = db.scalars(select(User.id).where(User.username == "admin")).one()
    for body in ({"password": "Takeover-pass-123"}, {"is_active": False}, {"role": "auditor"}, {"display_name": "x"}):
        assert ops.patch(f"/api/admin/users/{sa_id}", json=body).status_code == 403, body
    with session_scope() as db:
        u = db.get(User, sa_id)
        assert u.is_active and u.role == "tenant_admin" and u.display_name == ""


def test_owner_link_cannot_be_reissued_once_an_owner_exists(app, admin):
    client(app).post("/api/public/registrations", json=REG)
    rid = admin.get("/api/platform/registrations").json()[0]["id"]
    out = admin.post(f"/api/platform/registrations/{rid}/approve", json={}).json()
    tid = out["tenant"]["id"]
    # before activation a fresh link may be issued (e.g. the first expired) ...
    again = admin.post(f"/api/platform/tenants/{tid}/owner-invitation")
    assert again.status_code == 200
    token = again.json()["link"].split("#")[1]
    # ... the earlier link is revoked by the reissue
    old = out["owner_invitation"]["link"].split("#")[1]
    assert client(app).post("/api/public/invitations/inspect", json={"token": old}).status_code == 404
    assert client(app).post("/api/public/invitations/accept",
                            json={"token": token, "username": "asha", "password": "Owner-pass-1234"}).status_code == 200
    # once the organization has an owner, Unical can no longer mint owner access
    assert admin.post(f"/api/platform/tenants/{tid}/owner-invitation").status_code == 409


def test_invitation_accept_is_atomic_under_concurrency(app):
    import threading
    make_tenant("acme")
    adm = login(app, "adm", "tenant_admin", tenant="acme", platform=False)
    token = adm.post("/api/t/acme/admin/invitations", json={"email": "x@acme.example", "role": "developer"}).json()["link"].split("#")[1]
    results, barrier = [], threading.Barrier(4)

    def go(i):
        c = client(app)
        barrier.wait()
        results.append(c.post("/api/public/invitations/accept",
                              json={"token": token, "username": f"racer{i}", "password": "Racer-pass-12345"}).status_code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(results) == [200, 404, 404, 404], results
    with session_scope() as db:
        assert len(db.scalars(select(User).where(User.username.like("racer%"))).all()) == 1


def test_customer_tenants_never_get_private_target_access(app, two_tenants):
    from testpilot import targets
    a = two_tenants["acme"]
    allow("127.0.0.1", tenant="acme")  # 127.0.0.1 is in ALLOWED_PRIVATE_TARGETS for the test server
    listed = {t["host"]: t["private_access"] for t in a["c"].get(f"{a['base']}/admin/targets").json()}
    assert listed["127.0.0.1"] is False
    p = a["c"].post(f"{a['base']}/projects", json={"name": "local", "base_url": "http://127.0.0.1:8080/"}).json()
    with session_scope() as db:
        pol = targets.build(db, db.get(Project, p["id"]))
        assert pol.policy.private_ok == frozenset() and "127.0.0.1" in pol.policy.allowed_hosts
    # the default (Unical) organization keeps operator-approved private access
    allow("127.0.0.1")
    with session_scope() as db:
        uni = Project(tenant_id=tenant_id("unical"), name="u", base_url="http://127.0.0.1:8080/", extra_hosts=[])
        db.add(uni)
        db.flush()
        assert targets.build(db, uni).policy.private_ok == frozenset({"127.0.0.1"})
