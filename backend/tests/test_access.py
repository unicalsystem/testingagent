"""Groups and project assignments: scopes, dates, expiry at request time, escalation rules, worker re-check."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from testpilot import worker
from testpilot.db import session_scope
from testpilot.models import AuditLog, ProjectAssignment, Run

from .conftest import allow, login, make_tenant, make_user, tenant_id

BASE = "/api/t/acme"


@pytest.fixture
def org(app):
    make_tenant("acme")
    make_tenant("globex")
    allow("app.example.com", tenant="acme")
    allow("app.example.com", tenant="globex")
    adm = login(app, "boss", "tenant_admin", tenant="acme", platform=False)
    p1 = adm.post(f"{BASE}/projects", json={"name": "Portal", "base_url": "https://app.example.com/"}).json()["id"]
    p2 = adm.post(f"{BASE}/projects", json={"name": "Billing", "base_url": "https://app.example.com/"}).json()["id"]
    c1 = adm.post(f"{BASE}/projects/{p1}/cases", json={"name": "c", "steps": [{"action": "goto", "url": "/"}]}).json()["id"]
    gadm = login(app, "gboss", "tenant_admin", tenant="globex", platform=False)
    gp = gadm.post("/api/t/globex/projects", json={"name": "G", "base_url": "https://app.example.com/"}).json()["id"]
    return {"adm": adm, "p1": p1, "p2": p2, "c1": c1, "gp": gp, "gadm": gadm}


def user(app, name, role="qa_engineer"):
    c = login(app, name, role, tenant="acme", platform=False, assign_all=False)
    uid = c.get(f"{BASE}/auth/me").json()["id"]
    return c, uid


def assign(adm, **body):
    body.setdefault("scope", "project")
    r = adm.post(f"{BASE}/access/assignments", json=body)
    return r


def visible(c):
    return sorted(p["name"] for p in c.get(f"{BASE}/projects").json())


# ---------------------------------------------------------------------------------- assignments
def test_new_users_have_no_project_access_until_assigned(app, org):
    c, uid = user(app, "qa1")
    assert visible(c) == []
    assert c.get(f"{BASE}/projects/{org['p1']}").status_code == 404
    assert assign(org["adm"], user_id=uid, project_id=org["p1"], role="qa_engineer").status_code == 201
    assert visible(c) == ["Portal"]
    assert c.get(f"{BASE}/projects/{org['p2']}").status_code == 404
    assert c.get(f"{BASE}/cases/{org['c1']}").status_code == 200
    assert c.post(f"{BASE}/cases/{org['c1']}/runs").status_code == 201


def test_all_projects_scope_needs_confirmation_and_stays_in_the_tenant(app, org):
    c, uid = user(app, "qa1")
    r = assign(org["adm"], user_id=uid, scope="all", role="auditor")
    assert r.status_code == 409
    r = assign(org["adm"], user_id=uid, scope="all", role="auditor", confirm_all_projects=True)
    assert r.status_code == 201
    assert visible(c) == ["Billing", "Portal"]
    # future projects are included
    org["adm"].post(f"{BASE}/projects", json={"name": "Later", "base_url": "https://app.example.com/"})
    assert visible(c) == ["Billing", "Later", "Portal"]
    # never another tenant's
    assert c.get(f"/api/t/globex/projects/{org['gp']}").status_code == 401
    assert c.get(f"{BASE}/projects/{org['gp']}").status_code == 404
    # auditor role: read but not run
    assert c.post(f"{BASE}/cases/{org['c1']}/runs").status_code == 403


def test_future_and_expired_assignments_are_enforced_at_request_time(app, org):
    c, uid = user(app, "qa1")
    now = datetime.now(timezone.utc)
    a = assign(org["adm"], user_id=uid, project_id=org["p1"], role="qa_engineer",
               starts_at=(now + timedelta(hours=1)).isoformat()).json()
    assert a["state"] == "future" and visible(c) == []
    with session_scope() as db:
        db.execute(update(ProjectAssignment).where(ProjectAssignment.id == a["id"])
                   .values(starts_at=now - timedelta(minutes=1), ends_at=now + timedelta(minutes=5)))
    assert visible(c) == ["Portal"]
    rid = c.post(f"{BASE}/cases/{org['c1']}/runs").json()["id"]
    with session_scope() as db:
        db.execute(update(Run).where(Run.id == rid).values(status="passed", finished_at=now))
    assert c.get(f"{BASE}/runs/{rid}/report").status_code == 200
    # the end passes while the session is still signed in
    with session_scope() as db:
        db.execute(update(ProjectAssignment).where(ProjectAssignment.id == a["id"]).values(ends_at=now - timedelta(seconds=1)))
    assert c.get(f"{BASE}/auth/me").status_code == 200          # still signed in ...
    assert visible(c) == []                                      # ... but nothing visible
    for method, path in (("GET", f"/projects/{org['p1']}"), ("GET", f"/cases/{org['c1']}"), ("GET", f"/runs/{rid}"),
                         ("GET", f"/runs/{rid}/report"), ("POST", f"/cases/{org['c1']}/runs"),
                         ("GET", f"/projects/{org['p1']}/runs")):
        assert c.request(method, BASE + path).status_code == 404, path
    # bookkeeping job marks it expired and audits it
    assert worker.expire_assignments() == 1
    with session_scope() as db:
        assert db.get(ProjectAssignment, a["id"]).status == "expired"
        assert db.scalars(select(AuditLog).where(AuditLog.action == "assignment_expired")).first()
    # extending renews it
    r = org["adm"].patch(f"{BASE}/access/assignments/{a['id']}", json={"clear_end": True})
    assert r.json()["state"] == "active" and visible(c) == ["Portal"]


def test_revoked_assignment_stops_access_immediately(app, org):
    c, uid = user(app, "qa1")
    a = assign(org["adm"], user_id=uid, project_id=org["p1"], role="qa_engineer").json()
    assert visible(c) == ["Portal"]
    assert org["adm"].post(f"{BASE}/access/assignments/{a['id']}/revoke").json()["state"] == "revoked"
    assert visible(c) == [] and c.get(f"{BASE}/cases/{org['c1']}").status_code == 404
    assert org["adm"].patch(f"{BASE}/access/assignments/{a['id']}", json={"role": "auditor"}).status_code == 409


def test_date_only_end_lasts_until_the_end_of_that_day_in_its_time_zone(app, org):
    c, uid = user(app, "qa1")
    a = assign(org["adm"], user_id=uid, project_id=org["p1"], role="qa_engineer", end_date="2026-12-31",
               timezone="Asia/Kolkata").json()
    # 1 Jan 2027 00:00 IST == 31 Dec 2026 18:30 UTC
    assert a["ends_at"].startswith("2026-12-31T18:30:00")
    b = assign(org["adm"], user_id=uid, project_id=org["p2"], role="qa_engineer", start_date="2026-10-15",
               start_time="09:00", timezone="America/New_York").json()
    assert b["starts_at"].startswith("2026-10-15T13:00:00")  # EDT, UTC-4
    assert assign(org["adm"], user_id=uid, project_id=org["p2"], role="qa_engineer", start_date="2026-11-02",
                  end_date="2026-11-01").status_code == 422
    assert assign(org["adm"], user_id=uid, project_id=org["p2"], role="qa_engineer", timezone="Mars/Base").status_code == 422


# --------------------------------------------------------------------------------------- groups
def test_group_assignments_and_membership(app, org):
    adm = org["adm"]
    c, uid = user(app, "dev1", "developer")
    g = adm.post(f"{BASE}/access/groups", json={"name": "QA Team", "description": "regression"}).json()
    assert adm.post(f"{BASE}/access/groups", json={"name": "QA Team"}).status_code == 409
    assign(adm, group_id=g["id"], project_id=org["p2"], role="qa_engineer")
    assert visible(c) == []
    adm.post(f"{BASE}/access/groups/{g['id']}/members", json={"user_ids": [uid]})
    assert visible(c) == ["Billing"]
    # group role (QA) applies: QA may edit secrets, a developer alone may not
    assert c.put(f"{BASE}/projects/{org['p2']}/secrets", json={"name": "PW", "value": "x"}).status_code == 200
    # deactivating the group removes group-derived access
    adm.put(f"{BASE}/access/groups/{g['id']}", json={"name": "QA Team", "is_active": False})
    assert visible(c) == []
    adm.put(f"{BASE}/access/groups/{g['id']}", json={"name": "QA Team", "is_active": True})
    adm.delete(f"{BASE}/access/groups/{g['id']}/members/{uid}")
    assert visible(c) == []
    eff = adm.get(f"{BASE}/access/users/{uid}/effective").json()
    assert eff["projects"] == []


def test_direct_and_group_access_combine_as_a_union(app, org):
    adm = org["adm"]
    c, uid = user(app, "dev1", "developer")
    g = adm.post(f"{BASE}/access/groups", json={"name": "Auditors"}).json()
    adm.post(f"{BASE}/access/groups/{g['id']}/members", json={"user_ids": [uid]})
    assign(adm, group_id=g["id"], project_id=org["p1"], role="auditor")
    assign(adm, user_id=uid, project_id=org["p1"], role="developer")
    eff = adm.get(f"{BASE}/access/users/{uid}/effective").json()["projects"][0]
    assert eff["roles"] == ["auditor", "developer"] and "run.execute" in eff["permissions"]


def test_cross_tenant_groups_users_and_projects_are_refused(app, org):
    adm, gadm = org["adm"], org["gadm"]
    gg = gadm.post("/api/t/globex/access/groups", json={"name": "Globex QA"}).json()
    gu = make_user("gus", "tester", tenant="globex", platform=False, assign_all=False)
    g = adm.post(f"{BASE}/access/groups", json={"name": "Acme QA"}).json()
    assert adm.post(f"{BASE}/access/groups/{g['id']}/members", json={"user_ids": [gu]}).status_code == 404
    assert adm.post(f"{BASE}/access/groups/{gg['id']}/members", json={"user_ids": [gu]}).status_code == 404
    assert assign(adm, group_id=gg["id"], project_id=org["p1"], role="auditor").status_code == 404
    assert assign(adm, group_id=g["id"], project_id=org["gp"], role="auditor").status_code == 404
    assert assign(adm, user_id=gu, project_id=org["p1"], role="auditor").status_code == 404
    assert [x["name"] for x in adm.get(f"{BASE}/access/groups").json()] == ["Acme QA"]
    # the database refuses it too
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(ProjectAssignment(tenant_id=tenant_id("acme"), group_id=gg["id"], scope="project",
                                     project_id=org["p1"], role="auditor"))


# ----------------------------------------------------------------------------- escalation rules
def test_project_manager_manages_only_their_project_and_cannot_escalate(app, org):
    adm = org["adm"]
    pm, pm_id = user(app, "pm1", "project_manager")
    assign(adm, user_id=pm_id, project_id=org["p1"], role="project_manager")
    qa, qa_id = user(app, "qa1")
    assert pm.post(f"{BASE}/access/assignments", json={"user_id": qa_id, "scope": "project", "project_id": org["p1"],
                                                       "role": "qa_engineer"}).status_code == 201
    assert visible(qa) == ["Portal"]
    # not another project, not all projects, not themselves
    assert pm.post(f"{BASE}/access/assignments", json={"user_id": qa_id, "scope": "project", "project_id": org["p2"],
                                                       "role": "qa_engineer"}).status_code == 403
    assert pm.post(f"{BASE}/access/assignments", json={"user_id": qa_id, "scope": "all", "role": "auditor",
                                                       "confirm_all_projects": True}).status_code == 403
    assert pm.post(f"{BASE}/access/assignments", json={"user_id": pm_id, "scope": "project", "project_id": org["p1"],
                                                       "role": "qa_engineer"}).status_code == 403
    # group and user administration are not for project managers
    assert pm.post(f"{BASE}/access/groups", json={"name": "x"}).status_code == 403
    assert pm.get(f"{BASE}/admin/users").status_code == 403
    assert pm.get(f"{BASE}/access/assignments").status_code == 403
    assert pm.get(f"{BASE}/access/assignments?project_id={org['p1']}").status_code == 200
    # tenant-admin rights cannot be assigned through assignments at all
    assert adm.post(f"{BASE}/access/assignments", json={"user_id": qa_id, "scope": "project", "project_id": org["p1"],
                                                        "role": "tenant_admin"}).status_code == 422


def test_queued_run_does_not_execute_after_requester_loses_access(app, org, sample_server):
    allow("127.0.0.1", tenant="acme")
    c, uid = user(app, "qa1")
    a = assign(org["adm"], user_id=uid, project_id=org["p1"], role="qa_engineer").json()
    rid = c.post(f"{BASE}/cases/{org['c1']}/runs").json()["id"]
    org["adm"].post(f"{BASE}/access/assignments/{a['id']}/revoke")
    assert worker.run_once("pytest") == rid
    with session_scope() as db:
        r = db.get(Run, rid)
        assert r.status == "error" and "no longer has permission" in r.error


def test_project_creator_keeps_access_to_the_new_project(app, org):
    c, uid = user(app, "qa1")
    p = c.post(f"{BASE}/projects", json={"name": "Mine", "base_url": "https://app.example.com/"})
    assert p.status_code == 201
    assert visible(c) == ["Mine"] and "case.write" in p.json()["my_permissions"]


def test_project_manager_cannot_extend_own_access_through_a_group(app, org):
    adm = org["adm"]
    pm, pm_id = user(app, "pm1", "project_manager")
    end = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assign(adm, user_id=pm_id, project_id=org["p1"], role="project_manager", ends_at=end)
    g = adm.post(f"{BASE}/access/groups", json={"name": "Everyone"}).json()
    adm.post(f"{BASE}/access/groups/{g['id']}/members", json={"user_ids": [pm_id]})
    r = pm.post(f"{BASE}/access/assignments", json={"group_id": g["id"], "scope": "project", "project_id": org["p1"],
                                                    "role": "project_manager"})
    assert r.status_code == 403
    ga = assign(adm, group_id=g["id"], project_id=org["p1"], role="auditor", ends_at=end).json()
    assert pm.patch(f"{BASE}/access/assignments/{ga['id']}", json={"clear_end": True}).status_code == 403
    assert pm.get(f"{BASE}/access/groups").status_code == 403  # group list is for group managers
