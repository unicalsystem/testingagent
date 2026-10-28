"""End-to-end worker tests: real Chromium, real egress proxy, real PostgreSQL."""
from __future__ import annotations

import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from testpilot import storage, worker
from testpilot.cli import SAMPLE_CASES
from testpilot.db import session_scope
from testpilot.models import Evidence, Run
from testpilot.urlguard import Policy, TargetRejected, check_ip, check_url

from .conftest import allow


def run_case(c, pid: int, steps: list[dict], name: str = "case") -> dict:
    case = c.post(f"/api/projects/{pid}/cases", json={"name": name, "steps": steps})
    assert case.status_code == 201, case.text
    r = c.post(f"/api/cases/{case.json()['id']}/runs")
    assert r.status_code == 201, r.text
    assert worker.run_once("pytest") == r.json()["id"]
    return c.get(f"/api/runs/{r.json()['id']}").json()


@pytest.fixture
def sample_project(admin, sample_server):
    allow("127.0.0.1")
    p = admin.post("/api/projects", json={"name": "Sample", "base_url": sample_server}).json()
    admin.put(f"/api/projects/{p['id']}/secrets", json={"name": "SAMPLE_PASSWORD", "value": "demo-pass-123"})
    return p


# ---- URL guard unit tests --------------------------------------------------------------
@pytest.mark.parametrize("ip", ["169.254.169.254", "::ffff:169.254.169.254", "0.0.0.0", "fe80::1", "224.0.0.1"])
def test_metadata_and_special_ranges_always_blocked(ip):
    with pytest.raises(TargetRejected):
        check_ip(ip, private_ok=True)


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.17.0.1", "192.168.1.1", "127.0.0.1", "::1", "100.64.0.1", "::ffff:10.0.0.1"])
def test_private_blocked_unless_operator_approved(ip):
    with pytest.raises(TargetRejected):
        check_ip(ip, private_ok=False)
    check_ip(ip, private_ok=True)


def test_allowlist_is_exact_match():
    pol = Policy(frozenset({"app.example.com"}), frozenset())
    for u in ("https://evil.app.example.com/", "https://app.example.com.evil.net/", "https://example.com/"):
        with pytest.raises(TargetRejected):
            check_url(u, pol, resolve_dns=False)
    check_url("https://APP.example.com./x", pol, resolve_dns=False)


# ---- execution ------------------------------------------------------------------------------
def test_sample_cases_pass_with_evidence(admin, sample_project):
    for case in SAMPLE_CASES:
        run = run_case(admin, sample_project["id"], case["steps"], case["name"])
        assert run["status"] == "passed", (case["name"], run["error"], [s["error"] for s in run["steps"]])
        assert all(s["status"] == "passed" for s in run["steps"])
        assert all(any(e["kind"] == "screenshot" for e in s["evidence"]) for s in run["steps"])
        kinds = {e["kind"] for e in run["evidence"]}
        assert {"trace", "log", "report_html", "report_json"} <= kinds


def test_failure_is_reported_with_screenshot_and_skips(admin, sample_project):
    run = run_case(admin, sample_project["id"], [
        {"action": "goto", "url": "/"},
        {"action": "expect_text", "selector": "h1", "text": "Nonexistent heading", "timeout_ms": 800, "retries": 1},
        {"action": "click", "selector": "#never"},
    ])
    assert run["status"] == "failed"
    s = run["steps"]
    assert [x["status"] for x in s] == ["passed", "failed", "skipped"]
    assert s[1]["attempts"] == 2 and "Nonexistent heading" in s[1]["error"]
    assert any(e["kind"] == "screenshot" for e in s[1]["evidence"])
    rep = admin.get(f"/api/runs/{run['id']}/report?format=json").json()
    assert rep["run"]["status"] == "failed" and rep["steps"][1]["status"] == "failed"


def test_secret_is_masked_everywhere(admin, sample_project):
    run = run_case(admin, sample_project["id"], SAMPLE_CASES[0]["steps"])
    assert run["status"] == "passed"
    with session_scope() as db:
        files = [storage.resolve_evidence(e.rel_path) for e in db.scalars(select(Evidence).where(Evidence.run_id == run["id"]))]
    for f in files:
        if f.suffix == ".zip":
            with zipfile.ZipFile(f) as z:
                assert not any(b"demo-pass-123" in z.read(n) for n in z.namelist()), f
        elif f.suffix in (".log", ".json", ".html"):
            assert b"demo-pass-123" not in f.read_bytes(), f


class _Redirector(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        if self.path.startswith("/go"):
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        if self.path.startswith("/meta"):
            body = b'<h1>page</h1><img src="http://169.254.169.254/latest/meta-data/">'
        else:
            body = b"<h1>internal secret page</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def redirector():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_redirect_to_unlisted_internal_host_is_blocked(admin, redirector):
    port = redirector.server_address[1]
    _Redirector.target = f"http://127.0.0.2:{port}/secret"
    allow("127.0.0.1")
    p = admin.post("/api/projects", json={"name": "R", "base_url": f"http://127.0.0.1:{port}"}).json()
    run = run_case(admin, p["id"], [{"action": "goto", "url": "/go"},
                                    {"action": "expect_text", "selector": "h1", "text": "internal secret", "timeout_ms": 800}])
    assert run["status"] == "failed"
    assert "blocked by egress policy" in run["steps"][0]["error"].lower()
    assert run["summary"]["blocked_requests"] >= 1


def test_metadata_subresource_blocked(admin, redirector):
    port = redirector.server_address[1]
    allow("127.0.0.1")
    allow("169.254.169.254")  # even if an admin allowlists it, metadata stays blocked
    p = admin.post("/api/projects", json={"name": "M", "base_url": f"http://127.0.0.1:{port}",
                                         "extra_hosts": ["169.254.169.254"]}).json()
    run = run_case(admin, p["id"], [{"action": "goto", "url": "/meta"}, {"action": "expect_visible", "selector": "h1"}])
    assert run["status"] == "passed"
    assert run["summary"]["blocked_requests"] >= 1
    log = admin.get(next(e["url"] for e in run["evidence"] if e["kind"] == "log")).text
    assert "169.254.169.254" in log and "BLOCKED" in log


def test_allowlisted_name_resolving_to_private_ip_is_blocked(admin, sample_server):
    # "localhost" is allowlisted but not operator-approved for private access -> DNS check blocks it.
    port = sample_server.rsplit(":", 1)[1]
    allow("localhost")
    p = admin.post("/api/projects", json={"name": "L", "base_url": f"http://localhost:{port}"}).json()
    run = run_case(admin, p["id"], [{"action": "goto", "url": "/"}])
    assert run["status"] == "failed"
    assert "private" in run["steps"][0]["error"].lower()


def test_run_timeout_enforced(admin, sample_project, monkeypatch):
    from testpilot.config import get_settings

    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 3)
    run = run_case(admin, sample_project["id"], [{"action": "goto", "url": "/"}] + [{"action": "wait", "ms": 1000}] * 8)
    assert run["status"] == "error" and "execution limit" in run["error"]
    assert any(s["status"] == "skipped" for s in run["steps"])


def test_cancel_running_run(admin, sample_project, monkeypatch):
    from testpilot.config import get_settings

    monkeypatch.setattr(get_settings(), "worker_heartbeat_seconds", 1)
    case = admin.post(f"/api/projects/{sample_project['id']}/cases", json={
        "name": "slow", "steps": [{"action": "goto", "url": "/"}] + [{"action": "wait", "ms": 1000}] * 15}).json()
    rid = admin.post(f"/api/cases/{case['id']}/runs").json()["id"]
    t = threading.Thread(target=worker.run_once, args=("pytest",))
    t.start()
    for _ in range(50):
        if admin.get(f"/api/runs/{rid}").json()["status"] == "running":
            break
        time.sleep(0.2)
    admin.post(f"/api/runs/{rid}/cancel")
    t.join(60)
    run = admin.get(f"/api/runs/{rid}").json()
    assert run["status"] == "cancelled"


def test_stale_running_run_is_reaped(admin, sample_project):
    from datetime import timedelta

    from testpilot.models import utcnow

    with session_scope() as db:
        r = Run(tenant_id=1, project_id=sample_project["id"], status="running", case_snapshot={"name": "x", "steps": []},
                started_at=utcnow() - timedelta(hours=1), heartbeat_at=utcnow() - timedelta(hours=1))
        db.add(r)
        db.flush()
        rid = r.id
    assert worker.reap_stale() == 1
    assert admin.get(f"/api/runs/{rid}").json()["status"] == "error"


def test_evidence_requires_login(admin, sample_project, app):
    from fastapi.testclient import TestClient

    run = run_case(admin, sample_project["id"], [{"action": "goto", "url": "/"}])
    url = run["steps"][0]["evidence"][0]["url"]
    assert admin.get(url).status_code == 200
    anon = TestClient(app, base_url="https://testingagent.unical.in")
    assert anon.get(url).status_code == 401
    assert anon.get(f"/api/runs/{run['id']}/report").status_code == 401


# ---- fixes from the pre-release review ------------------------------------------------------
def test_proxy_prefers_ipv4_and_falls_back(monkeypatch, sample_server):
    """Dual-stack host whose IPv6 address is unreachable must still connect over IPv4."""
    import asyncio

    from testpilot import egress_proxy

    port = int(sample_server.rsplit(":", 1)[1])
    monkeypatch.setattr(egress_proxy, "resolve", lambda h, p: ["2a02:4780::dead", "127.0.0.1"])
    px = egress_proxy.EgressProxy(Policy(frozenset({"dual.example"}), frozenset({"dual.example"})), {port})
    ips = px._vet("dual.example", port)
    assert ips[0] == "127.0.0.1"
    # unreachable first address is skipped
    r, w = asyncio.run(_open(px, ["::1", "127.0.0.1"], port))


async def _open(px, ips, port):
    r, w = await px._connect(ips, port)
    w.close()
    return r, w


def test_secrets_only_in_fill_values(admin, sample_project):
    r = admin.post(f"/api/projects/{sample_project['id']}/cases", json={"name": "x", "steps": [
        {"action": "goto", "url": "/?q={{secret:SAMPLE_PASSWORD}}"}]})
    assert r.status_code == 422 and "only allowed in the value of a fill" in r.json()["detail"]


def test_secret_cannot_be_filled_into_visible_field(admin, sample_project):
    run = run_case(admin, sample_project["id"], [
        {"action": "goto", "url": "/"},
        {"action": "fill", "selector": "#username", "value": "{{secret:SAMPLE_PASSWORD}}"},
    ])
    assert run["status"] == "failed" and "password fields" in run["steps"][1]["error"]


def test_tester_cannot_retarget_project_with_secrets(tester, admin, sample_project):
    body = {"name": "Sample", "base_url": "https://attacker.example.com"}
    assert tester.put(f"/api/projects/{sample_project['id']}", json=body).status_code == 403
    ok = {"name": "Renamed", "base_url": sample_project["base_url"]}
    assert tester.put(f"/api/projects/{sample_project['id']}", json=ok).status_code == 200
    assert admin.put(f"/api/projects/{sample_project['id']}", json=body).status_code == 200


def test_webrtc_udp_restricted():
    import inspect

    src = inspect.getsource(worker.Execution._browse)
    assert "disable_non_proxied_udp" in src


def test_cancel_does_not_override_running(admin, sample_project):
    case = admin.post(f"/api/projects/{sample_project['id']}/cases", json={"name": "c", "steps": [{"action": "goto", "url": "/"}]}).json()
    rid = admin.post(f"/api/cases/{case['id']}/runs").json()["id"]
    with session_scope() as db:
        db.get(Run, rid).status = "running"  # simulate the worker claiming it first
    r = admin.post(f"/api/runs/{rid}/cancel").json()
    assert r["status"] == "running" and r["cancel_requested"] is True


def test_customer_tenant_run_end_to_end(app, sample_server, monkeypatch):
    """A run in a non-default tenant executes, stores evidence under tenants/<id>/ and serves it only
    to that tenant. (The local sample server needs private access, granted here by the operator setting.)"""
    from testpilot.config import get_settings
    from .conftest import login, make_tenant

    monkeypatch.setattr(get_settings(), "private_target_tenants", "acme")

    tid = make_tenant("acme")
    allow("127.0.0.1", tenant="acme")
    c = login(app, "acme-qa", "qa_engineer", tenant="acme", platform=False)
    base = "/api/t/acme"
    p = c.post(f"{base}/projects", json={"name": "Sample", "base_url": sample_server}).json()
    c.put(f"{base}/projects/{p['id']}/secrets", json={"name": "SAMPLE_PASSWORD", "value": "demo-pass-123"})
    case = c.post(f"{base}/projects/{p['id']}/cases", json=SAMPLE_CASES[0]).json()
    rid = c.post(f"{base}/cases/{case['id']}/runs").json()["id"]
    assert worker.run_once("pytest") == rid
    run = c.get(f"{base}/runs/{rid}").json()
    assert run["status"] == "passed", run
    with session_scope() as db:
        evs = db.scalars(select(Evidence).where(Evidence.run_id == rid)).all()
        assert evs and all(e.rel_path.startswith(f"tenants/{tid}/projects/{p['id']}/runs/{rid}/") for e in evs)
        assert all(e.tenant_id == tid for e in evs)
    shot = next(e for s in run["steps"] for e in s["evidence"] if e["kind"] == "screenshot")
    assert c.get(f"{base}/evidence/{shot['id']}").status_code == 200
    assert c.get(f"{base}/runs/{rid}/report").status_code == 200
    other = login(app, "boss", "admin")  # Unical tenant: no access
    assert other.get(f"/api/evidence/{shot['id']}").status_code == 404
    assert other.get(f"/api/runs/{rid}/report").status_code == 404
