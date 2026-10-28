"""AI automation of manual cases: real worker, real Chromium, sample app, fake Claude."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from testpilot import ai, automation, worker

from .conftest import allow

LOGIN = [
    {"action": "goto", "url": "/"},
    {"action": "fill", "selector": "#username", "value": "demo"},
    {"action": "fill", "selector": "#password", "value": "{{secret:SAMPLE_PASSWORD}}"},
    {"action": "click", "selector": "role=button[name=\"Sign in\"]"},
    {"action": "expect_url", "contains": "board.html"},
]


class FakeClaude:
    """Answers submit_automation calls like Claude would, per case title."""

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        text = "".join(c.get("text", "") for c in kw["messages"][0]["content"] if c["type"] == "text")
        repair = "FAILED" in text
        if "Title: Sign-in page renders" in text:
            sel = "#login-form" if repair else "#does-not-exist"  # first draft is wrong, the repair fixes it
            out = {"automatable": True, "reason": "", "use_setup": False,
                   "steps": [{"action": "goto", "url": "/"}, {"action": "expect_visible", "selector": sel}]}
        elif "Title: Board greets the user" in text:
            out = {"automatable": True, "reason": "uses setup", "use_setup": True,
                   "steps": [{"action": "expect_text", "selector": "[data-testid=welcome]", "text": "Welcome, demo"}]}
        elif "Title: Always broken" in text:
            out = {"automatable": True, "reason": "", "use_setup": False,
                   "steps": [{"action": "goto", "url": "/"}, {"action": "expect_visible", "selector": "#nope"}]}
        else:
            out = {"automatable": False, "reason": "Needs a mailbox", "steps": []}
        return NS(content=[NS(type="tool_use", name="submit_automation", input=out)])


@pytest.fixture(autouse=True)
def _clean_evidence():
    """Trial runs write evidence under ids that restart for every test; leave no files for later tests."""
    import shutil

    from testpilot import storage

    yield
    shutil.rmtree(storage.root() / "tenants", ignore_errors=True)
    shutil.rmtree(storage.spool_root() / "tenants", ignore_errors=True)


@pytest.fixture
def fake(monkeypatch):
    f = FakeClaude()
    monkeypatch.setattr(ai, "_client_override", NS(messages=f))
    monkeypatch.setitem(ai._state, "last_error_at", 0.0)
    return f


def drain(limit: int = 40) -> None:
    for _ in range(limit):
        if worker.run_once("pytest"):
            continue
        if automation.tick("pytest"):
            continue
        break


def setup_project(admin, sample_server) -> dict:
    allow("127.0.0.1")
    p = admin.post("/api/projects", json={"name": "Sample", "base_url": sample_server}).json()
    admin.put(f"/api/projects/{p['id']}/secrets", json={"name": "SAMPLE_PASSWORD", "value": "demo-pass-123"})
    r = admin.put(f"/api/projects/{p['id']}/automation-settings", json={"setup_steps": LOGIN, "app_notes": "Sample app"})
    assert r.status_code == 200, r.text
    return p


def manual(admin, pid, name, **kw):
    r = admin.post(f"/api/projects/{pid}/cases", json={"name": name, "procedure": "1. Do it.", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_requires_consent_and_ai(admin, sample_server, monkeypatch):
    p = setup_project(admin, sample_server)
    c = manual(admin, p["id"], "Sign-in page renders")
    assert admin.post(f"/api/projects/{p['id']}/automation-jobs", json={"case_ids": [c["id"]], "confirm": True}).status_code == 503
    monkeypatch.setattr(ai, "_client_override", NS(messages=FakeClaude()))
    assert admin.post(f"/api/projects/{p['id']}/automation-jobs", json={"case_ids": [c["id"]]}).status_code == 422


def test_explore_draft_verify_repair_and_record(admin, sample_server, fake):
    p = setup_project(admin, sample_server)
    cases = [manual(admin, p["id"], n) for n in
             ("Sign-in page renders", "Board greets the user", "Password reset e-mail", "Always broken")]
    auto = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "Already automated", "steps": [{"action": "goto", "url": "/"}]}).json()
    tr = admin.post(f"/api/projects/{p['id']}/test-runs", json={"name": "Cycle 1"}).json()[0]
    r = admin.post(f"/api/projects/{p['id']}/automation-jobs",
                   json={"test_run_id": tr["id"], "confirm": True, "options": {"max_attempts": 2}})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["total"] == 4  # the already-automated case is skipped
    drain()
    j = admin.get(f"/api/automation-jobs/{job['id']}").json()
    assert j["status"] == "done", j
    assert "## Page /board.html" in j["app_map"] and "#username" in j["app_map"]  # explored signed-in pages
    st = {i["case"]["name"]: i for i in j["items"]}
    assert st["Sign-in page renders"]["status"] == "applied" and st["Sign-in page renders"]["attempts"] == 2
    assert st["Board greets the user"]["status"] == "applied" and st["Board greets the user"]["use_setup"]
    assert st["Password reset e-mail"]["status"] == "manual_only"
    assert st["Always broken"]["status"] == "failed" and "#nope" in st["Always broken"]["error"]
    # the repair prompt carried the failure
    assert any("FAILED" in "".join(c.get("text", "") for c in k["messages"][0]["content"] if c["type"] == "text")
               for k in fake.calls)
    # applied cases now run with the setup steps prepended
    board = admin.get(f"/api/cases/{cases[1]['id']}").json()
    assert board["use_setup"] and board["steps"][0]["action"] == "expect_text"
    run = admin.post(f"/api/cases/{cases[1]['id']}/runs").json()
    assert run["steps"][0]["description"].startswith("Setup:")
    # verified passes were recorded in the test run; the others stay Not run
    items = {i["case"]["name"]: i for i in admin.get(f"/api/test-runs/{tr['id']}").json()["items"]}
    assert items["Sign-in page renders"]["status"] == "passed"
    assert items["Always broken"]["status"] == "untested"
    assert items["Already automated"]["status"] == "untested"
    _ = auto


def test_review_mode_then_approve_and_reject(admin, sample_server, fake):
    p = setup_project(admin, sample_server)
    c1 = manual(admin, p["id"], "Sign-in page renders")
    c2 = manual(admin, p["id"], "Always broken")
    job = admin.post(f"/api/projects/{p['id']}/automation-jobs",
                     json={"case_ids": [c1["id"], c2["id"]], "confirm": True,
                           "options": {"verify": False, "auto_apply": False}}).json()
    drain()
    items = admin.get(f"/api/automation-jobs/{job['id']}").json()["items"]
    assert {i["status"] for i in items} == {"drafted"}
    assert admin.get(f"/api/cases/{c1['id']}").json()["steps"] == []  # nothing saved without review
    edited = [{"action": "goto", "url": "/"}, {"action": "expect_visible", "selector": "#login-form"}]
    assert admin.post(f"/api/automation-items/{items[0]['id']}/approve", json={"steps": edited}).status_code == 200
    assert admin.get(f"/api/cases/{c1['id']}").json()["steps"][1]["selector"] == "#login-form"
    bad = admin.post(f"/api/automation-items/{items[1]['id']}/approve", json={"steps": [{"action": "evaluate"}]})
    assert bad.status_code == 422
    assert admin.post(f"/api/automation-items/{items[1]['id']}/reject").status_code == 200
    assert admin.post(f"/api/automation-items/{items[1]['id']}/retry").status_code == 200
    assert admin.get(f"/api/automation-jobs/{job['id']}").json()["status"] == "running"
    assert admin.post(f"/api/automation-jobs/{job['id']}/cancel").json()["status"] == "cancelled"


# ---- multi-tenancy, project access and the AI switch (release 1.3 integration) ---------------
def test_automation_is_tenant_scoped_and_role_gated(app, fake):
    from sqlalchemy import update as _update

    from testpilot.db import session_scope
    from testpilot.models import TenantStorage

    from .conftest import login, make_tenant, tenant_id

    make_tenant("acme")
    make_tenant("globex")
    a = login(app, "acme-admin", "tenant_admin", tenant="acme", platform=False)
    g = login(app, "globex-admin", "tenant_admin", tenant="globex", platform=False)
    p = a.post("/api/t/acme/projects", json={"name": "A", "base_url": "https://a.example.com"}).json()
    c = a.post(f"/api/t/acme/projects/{p['id']}/cases", json={"name": "Manual", "procedure": "1. Open."}).json()
    r = a.post(f"/api/t/acme/projects/{p['id']}/automation-jobs",
               json={"case_ids": [c["id"]], "confirm": True, "options": {"verify": False, "auto_apply": False}})
    assert r.status_code == 201, r.text
    job = r.json()
    # another organization sees nothing and can change nothing
    assert g.get(f"/api/t/globex/automation-jobs/{job['id']}").status_code == 404
    assert g.post(f"/api/t/globex/automation-jobs/{job['id']}/cancel").status_code == 404
    assert g.get(f"/api/t/globex/projects/{p['id']}/automation-jobs").status_code == 404
    assert g.get(f"/api/t/globex/projects/{p['id']}/automation-settings").status_code == 404
    gp = g.post("/api/t/globex/projects", json={"name": "G", "base_url": "https://g.example.com"}).json()
    assert g.post(f"/api/t/globex/projects/{gp['id']}/automation-jobs",
                  json={"case_ids": [c["id"]], "confirm": True}).status_code == 422  # acme's case is not in globex
    item = a.get(f"/api/t/acme/automation-jobs/{job['id']}").json()["items"][0]
    assert g.post(f"/api/t/globex/automation-items/{item['id']}/approve", json={}).status_code == 404
    # an auditor may look, not start or approve
    aud = login(app, "aud", "auditor", tenant="acme", platform=False)
    assert aud.get(f"/api/t/acme/automation-jobs/{job['id']}").status_code == 200
    assert aud.post(f"/api/t/acme/projects/{p['id']}/automation-jobs",
                    json={"case_ids": [c["id"]], "confirm": True}).status_code == 403
    assert aud.put(f"/api/t/acme/projects/{p['id']}/automation-settings", json={"setup_steps": []}).status_code == 403
    assert aud.post(f"/api/t/acme/automation-items/{item['id']}/reject").status_code == 403
    # organization switches AI off: no new jobs, and queued items are not sent to Claude
    a.post(f"/api/t/acme/automation-jobs/{job['id']}/cancel")
    c2 = a.post(f"/api/t/acme/projects/{p['id']}/cases", json={"name": "Manual 2", "procedure": "1. Open."}).json()
    j2 = a.post(f"/api/t/acme/projects/{p['id']}/automation-jobs", json={"case_ids": [c2["id"]], "confirm": True}).json()
    with session_scope() as db:
        db.execute(_update(TenantStorage).where(TenantStorage.tenant_id == tenant_id("acme")).values(ai_enabled=False))
    assert a.post(f"/api/t/acme/projects/{p['id']}/automation-jobs",
                  json={"case_ids": [c2["id"]], "confirm": True}).status_code == 403
    calls = len(fake.calls) if hasattr(fake, "calls") else None
    drain()
    it2 = a.get(f"/api/t/acme/automation-jobs/{j2['id']}").json()["items"][0]
    assert it2["status"] == "error" and "turned off" in it2["error"]
    if calls is not None:
        assert len(fake.calls) == calls
