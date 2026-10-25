from __future__ import annotations

from fastapi.testclient import TestClient

from testpilot import ai
from testpilot.db import session_scope
from testpilot.models import Project, ProjectSecret

from .conftest import H, allow, make_user

STEPS = [{"action": "goto", "url": "/"}, {"action": "expect_visible", "selector": "h1"}]


def mk_project(c: TestClient, url: str = "https://app.example.com") -> dict:
    r = c.post("/api/projects", json={"name": "Demo", "base_url": url})
    assert r.status_code == 201, r.text
    return r.json()


# ---- authentication ---------------------------------------------------------------
def test_login_logout_and_cookie_flags(app):
    make_user("amy", "tester")
    c = TestClient(app, base_url="https://testingagent.unical.in")
    r = c.post("/api/auth/login", json={"username": "AMY", "password": "Password-12345"}, headers=H)
    assert r.status_code == 200
    ck = r.headers["set-cookie"]
    assert "HttpOnly" in ck and "SameSite=strict" in ck and "Path=/" in ck
    assert c.get("/api/auth/me").json()["username"] == "amy"
    c.post("/api/auth/logout", headers=H)
    assert c.get("/api/auth/me").status_code == 401


def test_wrong_password_and_lockout(app):
    make_user("bob", "tester")
    c = TestClient(app, base_url="https://testingagent.unical.in")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "bob", "password": "nope"}, headers=H).status_code == 401
    # locked now, even with the right password
    assert c.post("/api/auth/login", json={"username": "bob", "password": "Password-12345"}, headers=H).status_code == 429


def test_unknown_user_same_error(app):
    c = TestClient(app, base_url="https://testingagent.unical.in")
    r = c.post("/api/auth/login", json={"username": "ghost", "password": "x"}, headers=H)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid username or password"


def test_csrf_header_and_origin_required(app):
    make_user("cat", "admin")
    c = TestClient(app, base_url="https://testingagent.unical.in")
    assert c.post("/api/auth/login", json={"username": "cat", "password": "Password-12345"}).status_code == 403
    r = c.post("/api/auth/login", json={"username": "cat", "password": "Password-12345"},
               headers={**H, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_untrusted_host_rejected(app):
    c = TestClient(app, base_url="https://evil.example")
    assert c.get("/api/health").status_code == 400


def test_password_change_revokes_other_sessions(app):
    make_user("dan", "tester")
    a = TestClient(app, base_url="https://testingagent.unical.in")
    b = TestClient(app, base_url="https://testingagent.unical.in")
    for c in (a, b):
        c.post("/api/auth/login", json={"username": "dan", "password": "Password-12345"}, headers=H)
    r = a.post("/api/auth/password", json={"current_password": "Password-12345", "new_password": "NewPassword-2026"}, headers=H)
    assert r.status_code == 200
    assert a.get("/api/auth/me").status_code == 200  # fresh session issued
    assert b.get("/api/auth/me").status_code == 401  # other session revoked


# ---- authorisation -------------------------------------------------------------------
def test_viewer_cannot_modify(viewer):
    assert viewer.get("/api/projects").status_code == 200
    assert viewer.post("/api/projects", json={"name": "x", "base_url": "https://a.example.com"}).status_code == 403
    assert viewer.get("/api/admin/users").status_code == 403


def test_tester_cannot_administer(tester):
    assert tester.post("/api/admin/targets", json={"host": "a.example.com"}).status_code == 403
    assert tester.get("/api/admin/audit").status_code == 403


def test_admin_cannot_demote_self(admin):
    me = admin.get("/api/auth/me").json()
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"role": "viewer"}).status_code == 400


def test_new_user_must_change_password(admin, app):
    r = admin.post("/api/admin/users", json={"username": "newbie", "role": "tester", "password": "Initial-pass-99"})
    assert r.status_code == 201
    c = TestClient(app, base_url="https://testingagent.unical.in")
    assert c.post("/api/auth/login", json={"username": "newbie", "password": "Initial-pass-99"}, headers=H).json()["must_change_password"]
    c.headers.update(H)
    assert c.post("/api/projects", json={"name": "x", "base_url": "https://a.example.com"}).status_code == 403


# ---- projects, allowlist, steps ----------------------------------------------------------
def test_project_requires_allowlisted_target(admin):
    p = mk_project(admin)
    assert p["policy_problems"]
    case = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "t", "steps": STEPS}).json()
    r = admin.post(f"/api/cases/{case['id']}/runs")
    assert r.status_code == 422 and "allowlist" in r.json()["detail"]
    admin.post("/api/admin/targets", json={"host": "app.example.com"})
    assert admin.get(f"/api/projects/{p['id']}").json()["policy_problems"] == []
    assert admin.post(f"/api/cases/{case['id']}/runs").status_code == 201


def test_bad_urls_rejected(admin):
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.example.com", "http://user:pw@a.example.com/",
                "gopher://127.0.0.1"):
        assert admin.post("/api/projects", json={"name": "x", "base_url": url}).status_code == 422, url


def test_allowlist_rejects_urls_and_ports(admin):
    for h in ("https://a.example.com", "a.example.com/path", "a.example.com:8080", "bad host"):
        assert admin.post("/api/admin/targets", json={"host": h}).status_code == 422, h
    assert admin.post("/api/admin/targets", json={"host": "A.Example.com."}).json()["host"] == "a.example.com"


def test_step_validation_blocks_scripts(admin):
    p = mk_project(admin)
    for bad in ([{"action": "evaluate", "script": "fetch('/')"}],
                [{"action": "click", "selector": "js=document.body"}],
                [{"action": "goto", "url": "/", "script": "x"}],
                [{"action": "wait", "ms": 999999}]):
        r = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "t", "steps": bad})
        assert r.status_code == 422, bad


def test_secrets_are_write_only_and_encrypted(admin):
    p = mk_project(admin)
    admin.put(f"/api/projects/{p['id']}/secrets", json={"name": "PW", "value": "hunter2-secret"})
    listed = admin.get(f"/api/projects/{p['id']}/secrets").json()
    assert listed[0]["name"] == "PW" and "value" not in listed[0]
    with session_scope() as db:
        stored = db.query(ProjectSecret).one().value_encrypted
    assert "hunter2" not in stored
    # a case referencing a missing secret cannot run
    admin.post("/api/admin/targets", json={"host": "app.example.com"})
    c = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "t", "steps": [
        {"action": "goto", "url": "/"}, {"action": "fill", "selector": "#p", "value": "{{secret:OTHER}}"}]}).json()
    r = admin.post(f"/api/cases/{c['id']}/runs")
    assert r.status_code == 422 and "OTHER" in r.json()["detail"]


def test_archiving_keeps_history(admin):
    p = mk_project(admin)
    c = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "t", "steps": STEPS}).json()
    assert admin.delete(f"/api/cases/{c['id']}").status_code == 200
    assert admin.get(f"/api/cases/{c['id']}").status_code == 404
    assert admin.delete(f"/api/projects/{p['id']}").status_code == 200
    with session_scope() as db:
        assert db.get(Project, p["id"]).archived is True  # row retained


def test_cancel_queued_run(admin):
    allow("app.example.com")
    p = mk_project(admin)
    c = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "t", "steps": STEPS}).json()
    run = admin.post(f"/api/cases/{c['id']}/runs").json()
    r = admin.post(f"/api/runs/{run['id']}/cancel").json()
    assert r["status"] == "cancelled"
    assert admin.post(f"/api/runs/{run['id']}/cancel").status_code == 409


def test_report_not_available_until_finished(admin):
    allow("app.example.com")
    p = mk_project(admin)
    c = admin.post(f"/api/projects/{p['id']}/cases", json={"name": "t", "steps": STEPS}).json()
    run = admin.post(f"/api/cases/{c['id']}/runs").json()
    assert admin.get(f"/api/runs/{run['id']}/report").status_code == 409


# ---- AI degradation -------------------------------------------------------------------------
def test_ai_disabled_without_key_but_app_works(admin):
    st = admin.get("/api/system/status").json()
    assert st["ai"] == {"configured": False, "available": False, "model": st["ai"]["model"], "last_error": ""}
    p = mk_project(admin)
    r = admin.post(f"/api/projects/{p['id']}/ai/generate", json={"requirement": "Users can sign in to the portal"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert admin.get("/api/projects").status_code == 200


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeMessages:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            e = RuntimeError("overloaded")
            e.status_code = 529
            raise e
        return _Block(content=[_Block(type="tool_use", name="submit_test_cases", input={"test_cases": [
            {"name": "Login works", "steps": [{"action": "goto", "url": "/"}, {"action": "expect_visible", "selector": "#u"}]},
            {"name": "Sneaky", "steps": [{"action": "evaluate", "script": "steal()"}]},
        ]})])


def test_ai_generation_validates_claude_output(admin, monkeypatch):
    fake = _Block(messages=_FakeMessages())
    monkeypatch.setattr(ai, "_client_override", fake)
    monkeypatch.setitem(ai._state, "last_error_at", 0.0)
    p = mk_project(admin)
    r = admin.post(f"/api/projects/{p['id']}/ai/generate", json={"requirement": "Users can sign in to the portal"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [c["name"] for c in out["test_cases"]] == ["Login works"]
    assert len(out["rejected"]) == 1  # the script step from "Claude" was refused
    sent = fake.messages.calls[0]
    assert sent["tool_choice"]["name"] == "submit_test_cases"


def test_ai_outage_degrades_gracefully(admin, monkeypatch):
    monkeypatch.setattr(ai, "_client_override", _Block(messages=_FakeMessages(fail=True)))
    monkeypatch.setitem(ai._state, "last_error_at", 0.0)
    p = mk_project(admin)
    r = admin.post(f"/api/projects/{p['id']}/ai/generate", json={"requirement": "Users can sign in to the portal"})
    assert r.status_code == 503
    st = admin.get("/api/system/status").json()["ai"]
    assert st["configured"] and not st["available"] and "529" in st["last_error"]
    monkeypatch.setitem(ai._state, "last_error_at", 0.0)
