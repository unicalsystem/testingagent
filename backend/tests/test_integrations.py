"""Connectors (release 1.7): Azure DevOps Test Plans + bugs, Jira, GitHub, GitLab, webhooks — against local mocks."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import allow, tenant_id
from .test_apitest import KEY, fake_api, project, reset_state  # noqa: F401  (fixtures)

LOG: list[dict] = []
STATE = {"ado_state": "Active", "jira_done": False}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        b = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw and raw[:1] in b"[{" else raw
        except ValueError:
            body = raw
        p = self.path.split("?")[0]
        LOG.append({"m": self.command, "p": p, "q": self.path, "body": body, "h": dict(self.headers)})
        m = self.command
        # ---- Azure DevOps
        if p == "/org/_apis/projects/Proj":
            return self._send(200, {"name": "Proj"})
        if p.endswith("/_apis/test/Plans/5/Suites/6/points"):
            return self._send(200, {"value": [{"id": 900, "testCase": {"id": "1234"}}]})
        if p == "/org/Proj/_apis/test/runs" and m == "POST":
            return self._send(200, {"id": 77, "webAccessUrl": "https://ado.example/run/77"})
        if p == "/org/Proj/_apis/test/Runs/77/results" and m == "GET":
            return self._send(200, {"value": [{"id": 100000, "testCase": {"id": "1234"}}]})
        if p.startswith("/org/Proj/_apis/test/Runs/77"):
            return self._send(200, {"value": []})
        if p == "/org/Proj/_apis/wit/attachments":
            return self._send(201, {"url": "https://ado.example/att/1"})
        if p.startswith("/org/Proj/_apis/wit/workitems/$"):
            return self._send(200, {"id": 501, "_links": {"html": {"href": "https://ado.example/wi/501"}}})
        if p == "/org/Proj/_apis/wit/workitems/501":
            return self._send(200, {"id": 501, "fields": {"System.State": STATE["ado_state"]}})
        # ---- Jira
        if p == "/rest/api/2/myself":
            return self._send(200, {"displayName": "QA Bot"})
        if p == "/rest/api/2/project/QA":
            return self._send(200, {"key": "QA", "name": "QA", "issueTypes": [{"name": "Bug"}, {"name": "Task"}]})
        if p == "/rest/api/2/issue" and m == "POST":
            return self._send(201, {"key": "QA-1"})
        if p == "/rest/api/2/issue/QA-1" and m == "GET":
            return self._send(200, {"fields": {"status": {"statusCategory": {"key": "done" if STATE["jira_done"] else "indeterminate"}}}})
        if p.startswith("/rest/api/2/issue/QA-"):
            return self._send(201, {"id": "c1"})
        # ---- GitHub
        if p == "/repos/acme/app":
            return self._send(200, {"full_name": "acme/app", "has_issues": True})
        if p == "/repos/acme/app/issues" and m == "POST":
            return self._send(201, {"number": 3, "html_url": "https://github.example/acme/app/issues/3"})
        if p == "/repos/acme/app/issues/3":
            return self._send(200, {"state": "open"})
        if p.startswith("/repos/acme/app/issues/3/"):
            return self._send(201, {"id": 1})
        # ---- GitLab
        if p == "/api/v4/projects/grp%2Fapp":
            return self._send(200, {"path_with_namespace": "grp/app"})
        if p == "/api/v4/projects/grp%2Fapp/issues" and m == "POST":
            return self._send(201, {"iid": 4, "web_url": "https://gitlab.example/grp/app/-/issues/4"})
        if p == "/api/v4/projects/grp%2Fapp/issues/4":
            return self._send(200, {"state": "opened"})
        if p.startswith("/api/v4/projects/grp%2Fapp/"):
            return self._send(201, {"id": 1, "markdown": "![shot](/uploads/x.jpg)"})
        # ---- webhook
        if p == "/hook":
            return self._send(204)
        self._send(404, {"error": "unmocked " + p})

    do_GET = do_POST = do_PATCH = do_PUT = _handle


@pytest.fixture(scope="module")
def mock():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def allow_mocks(monkeypatch):
    from testpilot import integrations

    monkeypatch.setattr(integrations, "ALLOW_PRIVATE_FOR_TESTS", True)
    LOG.clear()
    STATE.update(ado_state="Active", jira_done=False)


def calls(method, pattern):
    return [c for c in LOG if c["m"] == method and re.search(pattern, c["p"])]


def api_run(tester, base, pid, failing=True):
    r = tester.post(f"/api/projects/{pid}/api-suites", json={
        "name": "Pets", "spec_source": "manual", "base_url": base, "requires_auth": False,
        "endpoints_text": "GET /pets?limit=5\nGET /pets/{id} -> " + ("418" if failing else "200"),
        "auth": {"type": "api_key", "config": {"name": "X-API-Key"}, "secrets": {"api_key": KEY}}, "run": True})
    assert r.status_code == 201, r.text
    from testpilot import apitesting

    while apitesting.tick("w"):
        pass
    return r.json()


def rerun(tester, suite_id):
    from testpilot import apitesting

    rid = tester.post(f"/api/api-suites/{suite_id}/runs", json={}).json()["id"]
    while apitesting.tick("w"):
        pass
    return rid


def connector(c, pid, kind, config, secrets, **kw):
    r = c.post(f"/api/projects/{pid}/integrations", json={"kind": kind, "name": kind, "config": config, "secrets": secrets,
                                                          "run_types": ["api", "browser", "manual"], **kw})
    assert r.status_code == 201, r.text
    return r.json()


def drain():
    from testpilot import integrations

    for _ in range(10):
        if not integrations.tick():
            return


def test_all_connectors_push_failures_once_and_update_on_repeat(tester, fake_api, mock):
    allow("127.0.0.1")
    pid = project(tester, fake_api)
    ado = connector(tester, pid, "azure_devops", {"org_url": f"{mock}/org", "project": "Proj"}, {"pat": "ado-pat-secret-123"})
    jira = connector(tester, pid, "jira", {"base_url": mock, "email": "qa@example.com", "project_key": "QA"}, {"token": "jira-token-xyz"})
    gh = connector(tester, pid, "github", {"api_url": mock, "repo": "acme/app"}, {"token": "ghp_secret"})
    gl = connector(tester, pid, "gitlab", {"base_url": mock, "project": "grp/app"}, {"token": "glpat-secret"})
    wh = connector(tester, pid, "webhook", {}, {"url": f"{mock}/hook", "signing_secret": "s3cret"})
    listing = json.dumps(tester.get(f"/api/projects/{pid}/integrations").json())
    for secret in ("ado-pat-secret-123", "jira-token-xyz", "ghp_secret", "glpat-secret", "s3cret"):
        assert secret not in listing
    assert tester.post(f"/api/integrations/{ado['id']}/test").json() == {"ok": True, "message": "Connected to project 'Proj'"}
    assert "QA Bot" in tester.post(f"/api/integrations/{jira['id']}/test").json()["message"]

    s = api_run(tester, fake_api, pid)
    LOG.clear()
    drain()
    # Azure DevOps: automated test run with one result per case, report attached, run completed, one bug
    run_post = calls("POST", r"/test/runs$")[0]
    assert run_post["body"]["automated"] is True and "plan" not in run_post["body"]
    results = calls("POST", r"/test/Runs/77/results$")[0]["body"]
    assert {r["outcome"] for r in results} == {"Passed", "Failed"}
    assert calls("POST", r"/test/Runs/77/attachments$")
    assert calls("PATCH", r"/test/Runs/77$")[0]["body"]["state"] == "Completed"
    bugs = calls("POST", r"/wit/workitems/\$Bug")
    assert len(bugs) == 1 and bugs[0]["h"]["Content-Type"].startswith("application/json-patch+json")
    assert bugs[0]["h"]["Authorization"] == "Basic " + __import__("base64").b64encode(b":ado-pat-secret-123").decode()
    # Jira / GitHub / GitLab: one issue each
    assert len(calls("POST", r"^/rest/api/2/issue$")) == 1
    assert len(calls("POST", r"^/repos/acme/app/issues$")) == 1
    assert len(calls("POST", r"^/api/v4/projects/grp%2Fapp/issues$")) == 1
    # webhook: signed payload with the failure
    hook = calls("POST", r"^/hook$")[0]
    sig = hmac.new(b"s3cret", json.dumps(hook["body"]).encode(), hashlib.sha256).hexdigest()
    assert hook["h"]["X-TestAgent-Signature"].startswith("sha256=") and hook["body"]["event"] == "testagent.results"
    assert hook["h"]["X-TestAgent-Signature"] == "sha256=" + sig and [i["outcome"] for i in hook["body"]["items"]] == ["failed"]
    syncs = tester.get(f"/api/integrations/{ado['id']}/syncs").json()
    assert syncs[0]["status"] == "done" and syncs[0]["result"]["ado_run_id"] == 77
    issues = tester.get(f"/api/integrations/{jira['id']}/issues").json()
    assert issues[0]["external_id"] == "QA-1" and issues[0]["state"] == "failing"

    # same failure again: comments, no new issues
    rerun(tester, s["id"])
    LOG.clear()
    drain()
    assert not calls("POST", r"/wit/workitems/\$Bug") and calls("PATCH", r"/wit/workitems/501$")
    assert not calls("POST", r"^/rest/api/2/issue$") and calls("POST", r"/rest/api/2/issue/QA-1/comment")
    assert not calls("POST", r"^/repos/acme/app/issues$") and calls("POST", r"/repos/acme/app/issues/3/comments")
    assert calls("POST", r"/projects/grp%2Fapp/issues/4/notes")

    # closed in Jira -> next failure opens a new issue
    STATE["jira_done"] = True
    rerun(tester, s["id"])
    LOG.clear()
    drain()
    assert len(calls("POST", r"^/rest/api/2/issue$")) == 1
    _ = (gh, gl, wh)


def test_manual_trigger_and_failures_only(tester, fake_api, mock):
    allow("127.0.0.1")
    pid = project(tester, fake_api)
    man = connector(tester, pid, "github", {"api_url": mock, "repo": "acme/app"}, {"token": "t0ken"}, trigger="manual")
    fail_only = connector(tester, pid, "gitlab", {"base_url": mock, "project": "grp/app"}, {"token": "t0ken"}, trigger="failures")
    s = api_run(tester, fake_api, pid, failing=False)
    LOG.clear()
    drain()
    assert LOG == []  # manual: nothing automatic; failures-only: run passed
    rid = s["generation"]["run"]
    targets = tester.get(f"/api/integrations/for-source?source_type=api&source_id={rid}").json()
    assert {t["id"] for t in targets} == {man["id"], fail_only["id"]}
    r = tester.post("/api/integrations/push", json={"source_type": "api", "source_id": rid, "integration_ids": [man["id"]]})
    assert r.status_code == 200 and r.json()[0]["status"] == "pending"
    drain()
    assert tester.get(f"/api/integrations/{man['id']}/syncs").json()[0]["status"] == "done"
    assert not calls("POST", r"^/repos/acme/app/issues$")  # nothing failed -> no issue


def test_ado_test_plan_points_from_manual_test_run(tester, fake_api, mock):
    from testpilot import integrations as ig
    from testpilot.db import session_scope
    from testpilot.models import TestCase, TestRun, TestRunItem, utcnow

    pid = project(tester, fake_api)
    ado = connector(tester, pid, "azure_devops", {"org_url": f"{mock}/org", "project": "Proj", "plan_id": 5, "suite_id": 6,
                                                  "case_map": "TC-001, 1234", "create_bugs": False}, {"pat": "pat-123456789012"})
    tid = tenant_id()
    with session_scope() as db:
        c1 = TestCase(tenant_id=tid, project_id=pid, name="Login works", external_id="TC-001", expected_result="Dashboard shown")
        c2 = TestCase(tenant_id=tid, project_id=pid, name="Logout works", external_id="TC-002")
        db.add_all([c1, c2])
        db.flush()
        tr = TestRun(tenant_id=tid, project_id=pid, name="Sprint 9", status="closed", closed_at=utcnow())
        db.add(tr)
        db.flush()
        db.add_all([TestRunItem(tenant_id=tid, test_run_id=tr.id, test_case_id=c1.id, position=0, status="failed", actual_result="Error 500"),
                    TestRunItem(tenant_id=tid, test_run_id=tr.id, test_case_id=c2.id, position=1, status="blocked")])
        trid = tr.id
    LOG.clear()
    drain()
    body = calls("POST", r"/test/runs$")[0]["body"]
    assert body["plan"] == {"id": "5"} and body["pointIds"] == [900] and body["automated"] is False
    upd = calls("PATCH", r"/test/Runs/77/results$")[0]["body"]
    assert upd == [{"id": 100000, "outcome": "Failed", "state": "Completed", "durationInMs": 0,
                    "comment": upd[0]["comment"], "errorMessage": "Error 500"}]
    extra = calls("POST", r"/test/Runs/77/results$")[0]["body"]
    assert [r["outcome"] for r in extra] == ["Blocked"] and extra[0]["testCaseTitle"] == "TC-002 · Logout works"
    assert not calls("POST", r"/wit/workitems")
    assert tester.get(f"/api/integrations/{ado['id']}/syncs").json()[0]["source_id"] == trid
    with session_scope() as db:
        rs = ig.load_source(db, "test_run", trid)
        assert rs.failed and rs.summary["failed"] == 1


def test_validation_and_permissions(tester, viewer, fake_api, mock, monkeypatch):
    from testpilot import integrations

    pid = project(tester, fake_api)
    bad = [("azure_devops", {"org_url": f"{mock}/org"}, {"pat": "x"}),               # project missing
           ("azure_devops", {"org_url": f"{mock}/org", "project": "P"}, {}),          # PAT missing
           ("jira", {"base_url": mock, "project_key": "QA"}, {"token": "t"}),         # cloud needs e-mail
           ("github", {"repo": "not a repo"}, {"token": "t"}),
           ("azure_devops", {"org_url": f"{mock}/org", "project": "P", "plan_id": 3}, {"pat": "x"})]  # plan without suite
    for kind, cfg, sec in bad:
        r = tester.post(f"/api/projects/{pid}/integrations", json={"kind": kind, "name": "x", "config": cfg, "secrets": sec})
        assert r.status_code == 422, (kind, cfg, r.text)
    assert viewer.post(f"/api/projects/{pid}/integrations", json={"kind": "webhook", "name": "x", "secrets": {"url": f"{mock}/hook"}}).status_code == 403
    monkeypatch.setattr(integrations, "ALLOW_PRIVATE_FOR_TESTS", False)
    for url in (f"{mock}/hook", "https://127.0.0.1/hook", "https://169.254.169.254/latest"):
        r = tester.post(f"/api/projects/{pid}/integrations", json={"kind": "webhook", "name": "x", "secrets": {"url": url}})
        assert r.status_code == 422, url
