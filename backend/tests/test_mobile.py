"""Mobile app testing (release 1.8): providers, app uploads, cases, cross-device runs on a mock Appium / device
cloud, the recorder and the AI agent over the worker-held device, the built-in phone emulation, reports,
permissions and tenant isolation."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import allow, login, make_tenant
from .test_apitest import project

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
CALLS: list[tuple[str, str, dict]] = []
DEV: dict = {}

SCREEN_LOGIN = """<?xml version="1.0" encoding="UTF-8"?><hierarchy index="0" width="1080" height="2400">
<android.widget.FrameLayout bounds="[0,0][1080,2400]" displayed="true">
 <android.widget.TextView text="Acme Mobile" bounds="[100,200][980,300]" displayed="true"/>
 <android.widget.EditText resource-id="com.acme:id/user" text="" bounds="[100,500][980,620]" clickable="true" displayed="true"/>
 <android.widget.EditText resource-id="com.acme:id/pass" text="" bounds="[100,700][980,820]" clickable="true" displayed="true"/>
 <android.widget.Button content-desc="login" text="Sign in" bounds="[100,900][980,1020]" clickable="true" displayed="true"/>
 <android.widget.Button text="Help" bounds="[100,1100][500,1200]" clickable="true" displayed="true"/>
 <android.widget.Button text="Help" bounds="[600,1100][980,1200]" clickable="true" displayed="true"/>
</android.widget.FrameLayout></hierarchy>"""
SCREEN_HOME = """<?xml version="1.0" encoding="UTF-8"?><hierarchy width="1080" height="2400">
<android.widget.FrameLayout bounds="[0,0][1080,2400]">
 <android.widget.TextView resource-id="com.acme:id/welcome" text="Welcome, tess" bounds="[100,200][980,300]"/>
</android.widget.FrameLayout></hierarchy>"""
# element ids -> (strategy, value) the mock understands
ELEMENTS = {"user": ("id", "com.acme:id/user"), "pass": ("id", "com.acme:id/pass"), "login": ("accessibility id", "login"),
            "welcome": ("id", "com.acme:id/welcome")}


def _match(using: str, value: str) -> str | None:
    screen = DEV.get("screen", "login")
    for k, (st, v) in ELEMENTS.items():
        if (k == "welcome") != (screen == "home"):
            continue
        if st == using and v == value:
            return k
        if using == "xpath" and k == "login" and "'Sign in'" in value:
            return k
        if using == "xpath" and k == "welcome" and "Welcome" in value:
            return k
    return None


class Hub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if "multipart" in (self.headers.get("Content-Type") or ""):
            return {"_multipart": len(raw)}
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {"_raw": raw.decode(errors="replace")}

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        body = self._body() if method == "POST" else {}
        path = self.path.split("?")[0]
        CALLS.append((method, path, body))
        auth = self.headers.get("Authorization", "")
        # ---- vendor REST APIs
        if path == "/app-automate/plan.json":
            return self._send(200 if auth else 401, {"parallel_sessions_max_allowed": 5})
        if path == "/app-automate/upload":
            return self._send(200, {"app_url": "bs://abc123"})
        if path == "/app-automate/devices.json":
            return self._send(200, [{"device": "Google Pixel 8", "os_version": "14.0", "os": "android", "realMobile": True},
                                    {"device": "iPhone 15", "os_version": "17", "os": "ios", "realMobile": True}])
        if path.startswith("/app-automate/sessions/"):
            return self._send(200, {"automation_session": {"public_url": "https://example.test/s/" + path.split("/")[-1]}})
        if path == "/wd/hub/status":
            return self._send(200, {"value": {"ready": True, "build": {"version": "2.11.0"}}})
        # ---- W3C WebDriver
        if path == "/wd/hub/session" and method == "POST":
            caps = body["capabilities"]["alwaysMatch"]
            DEV.update(screen="login", user="", pw="", caps=caps, sid=f"s{len([c for c in CALLS if c[1] == '/wd/hub/session'])}")
            return self._send(200, {"value": {"sessionId": DEV["sid"], "capabilities": caps}})
        m = re.match(r"^/wd/hub/session/([^/]+)(/.*)?$", path)
        if not m:
            return self._send(404, {"value": {"error": "unknown command", "message": path}})
        sub = m.group(2) or ""
        if method == "DELETE" and not sub:
            DEV["quit"] = True
            return self._send(200, {"value": None})
        if sub == "/element":
            k = _match(body["using"], body["value"])
            if not k:
                return self._send(404, {"value": {"error": "no such element", "message": "not found"}})
            return self._send(200, {"value": {"element-6066-11e4-a52e-4f735466cecf": k}})
        if sub == "/elements":
            k = _match(body["using"], body["value"])
            return self._send(200, {"value": [{"element-6066-11e4-a52e-4f735466cecf": k}] if k else []})
        em = re.match(r"^/element/([^/]+)/(click|value|clear|text|displayed)$", sub)
        if em:
            el, op = em.groups()
            if op == "value":
                DEV["user" if el == "user" else "pw"] = body.get("text", "")
            elif op == "click" and el == "login":
                DEV["screen"] = "home" if DEV.get("user") == "tess" and DEV.get("pw") == "s3cret-pass" else "login"
            elif op == "text":
                return self._send(200, {"value": "Welcome, tess" if el == "welcome" else ""})
            elif op == "displayed":
                return self._send(200, {"value": True})
            return self._send(200, {"value": None})
        if sub == "/screenshot":
            return self._send(200, {"value": base64.b64encode(PNG).decode()})
        if sub == "/source":
            return self._send(200, {"value": SCREEN_HOME if DEV.get("screen") == "home" else SCREEN_LOGIN})
        if sub == "/window/rect":
            return self._send(200, {"value": {"x": 0, "y": 0, "width": 1080, "height": 2400}})
        if sub in ("/actions", "/back", "/appium/device/hide_keyboard", "/appium/device/activate_app"):
            if sub == "/actions" and method == "POST":
                DEV["taps"] = DEV.get("taps", 0) + 1
            return self._send(200, {"value": None})
        if sub == "/execute/sync":
            DEV.setdefault("executed", []).append(body.get("script", ""))
            return self._send(200, {"value": None})
        return self._send(404, {"value": {"error": "unknown command", "message": sub}})


@pytest.fixture
def hub(monkeypatch):
    from testpilot import integrations, mobileproviders as mp

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    for k in ("browserstack", "browserstack_api", "lambdatest", "saucelabs", "saucelabs_api"):
        monkeypatch.setitem(mp.TEST_HUB_OVERRIDE, k, base + ("/wd/hub" if "_api" not in k else ""))
    monkeypatch.setattr(integrations, "ALLOW_PRIVATE_FOR_TESTS", True)
    CALLS.clear()
    DEV.clear()
    yield base
    srv.shutdown()


def secret(pid: int, name: str, value: str) -> None:
    from testpilot.db import session_scope
    from testpilot.models import Project, ProjectSecret
    from testpilot.security import encrypt_secret

    with session_scope() as db:
        p = db.get(Project, pid)
        db.add(ProjectSecret(tenant_id=p.tenant_id, project_id=pid, name=name, value_encrypted=encrypt_secret(value)))


def run_all() -> list[int]:
    from testpilot import mobilerun

    done = []
    while True:
        rid = mobilerun.claim("test-worker", True)
        if not rid:
            return done
        mobilerun.execute(rid)
        done.append(rid)


def bs_provider(c, pid) -> dict:
    r = c.post(f"/api/projects/{pid}/mobile/providers", json={"kind": "browserstack", "name": "BrowserStack",
                                                             "config": {"username": "qa-bot"}, "secret": "bs-key-1234567890"})
    assert r.status_code == 201, r.text
    return r.json()


def upload_apk(c, pid) -> dict:
    r = c.post(f"/api/projects/{pid}/mobile/apps", files={"file": ("acme.apk", b"PK\x03\x04" + b"x" * 4000, "application/octet-stream")},
               data={"name": "Acme", "version": "2.1.0", "app_id": "com.acme"})
    assert r.status_code == 201, r.text
    return r.json()


LOGIN_STEPS = [{"action": "type", "target": "id=com.acme:id/user", "text": "tess"},
               {"action": "type", "target": "id=com.acme:id/pass", "text": "{{secret:APP_PW}}"},
               {"action": "tap", "target": "~login"},
               {"action": "expect_text", "target": "id=com.acme:id/welcome", "text": "Welcome, tess"}]


def test_native_run_on_two_devices_with_cached_upload_report_and_marking(tester, hub):
    pid = project(tester, "https://acme.example.com")
    secret(pid, "APP_PW", "s3cret-pass")
    prov = bs_provider(tester, pid)
    assert prov["has_secret"] and "bs-key" not in json.dumps(prov) and prov["secret_hint"] == "…7890"
    t = tester.post(f"/api/mobile/providers/{prov['id']}/test").json()
    assert t["ok"] and "parallel sessions allowed: 5" in t["message"]
    devs = tester.get(f"/api/mobile/providers/{prov['id']}/devices").json()["devices"]
    assert {"name": "Google Pixel 8", "os_version": "14.0", "platform": "android"} in devs
    app = upload_apk(tester, pid)
    assert app["platform"] == "android" and app["size"] == 4004 and len(app["sha256"]) == 64
    case = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "Login", "kind": "native", "platform": "android",
                                                                 "app_id": app["id"], "steps": LOGIN_STEPS})
    assert case.status_code == 201, case.text
    g = tester.post(f"/api/projects/{pid}/mobile/run-groups", json={
        "provider_id": prov["id"], "case_ids": [case.json()["id"]],
        "devices": [{"name": "Google Pixel 8", "os_version": "14.0", "platform": "android"},
                    {"name": "iPhone 15", "os_version": "17", "platform": "ios"}]})
    assert g.status_code == 201, g.text
    g = g.json()
    assert g["total"] == 1 and g["skipped"][0]["reason"] == "the case is for android"
    tester.post(f"/api/projects/{pid}/mobile/run-groups", json={
        "provider_id": prov["id"], "case_ids": [case.json()["id"]], "devices": [{"name": "Samsung Galaxy S24", "platform": "android"}]})
    assert len(run_all()) == 2
    detail = tester.get(f"/api/mobile/run-groups/{g['id']}").json()
    assert detail["status"] == "passed", detail
    run = tester.get(f"/api/mobile/runs/{detail['runs'][0]['id']}").json()
    assert [s["status"] for s in run["steps"]] == ["passed"] * 4
    assert run["session_url"].startswith("https://example.test/s/")
    # the build was uploaded once and the cloud reference reused by the second run
    assert len([c for c in CALLS if c[1] == "/app-automate/upload"]) == 1
    caps = DEV["caps"]
    assert caps["appium:app"] == "bs://abc123" and caps["bstack:options"]["accessKey"] == "bs-key-1234567890"
    assert caps["platformName"] == "Android" and caps["appium:automationName"] == "UiAutomator2"
    assert any('"status": "passed"' in s for s in DEV["executed"])  # session marked passed on BrowserStack
    # the secret was typed but never stored in results
    assert "s3cret-pass" not in json.dumps(run)
    shot = tester.get(f"/api/mobile/runs/{run['id']}/files/{run['steps'][0]['shot']}")
    assert shot.status_code == 200 and shot.headers["content-type"] in ("image/png", "image/jpeg")
    assert tester.get(f"/api/mobile/runs/{run['id']}/files/..%2F..%2Fsecret").status_code == 404
    rep = tester.get(f"/api/mobile/run-groups/{g['id']}/report.html")
    assert rep.status_code == 200 and "Cross-device matrix" in rep.text and "Google Pixel 8" in rep.text


def test_failure_skips_remaining_and_marks_failed(tester, hub):
    pid = project(tester, "https://acme.example.com")
    secret(pid, "APP_PW", "wrong")
    prov = bs_provider(tester, pid)
    app = upload_apk(tester, pid)
    cid = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "Login", "platform": "android", "app_id": app["id"],
                                                                "steps": LOGIN_STEPS + [{"action": "back"}]}).json()["id"]
    g = tester.post(f"/api/projects/{pid}/mobile/run-groups", json={"provider_id": prov["id"], "case_ids": [cid],
                                                                    "devices": [{"name": "Pixel", "platform": "android"}]}).json()
    run_all()
    run = tester.get(f"/api/mobile/runs/{g['runs'][0]['id']}").json()
    assert run["status"] == "failed" and "Step 4 failed" in run["error"]
    assert [s["status"] for s in run["steps"]] == ["passed", "passed", "passed", "failed", "skipped"]
    assert any('"status": "failed"' in s for s in DEV["executed"])
    assert DEV.get("quit")


def test_own_appium_gets_signed_download_link(tester, hub, monkeypatch):
    pid = project(tester, "https://acme.example.com")
    secret(pid, "APP_PW", "s3cret-pass")
    r = tester.post(f"/api/projects/{pid}/mobile/providers", json={"kind": "appium", "name": "Lab", "config": {"hub_url": hub + "/wd/hub"}})
    assert r.status_code == 201, r.text
    assert tester.post(f"/api/mobile/providers/{r.json()['id']}/test").json() == {"ok": True, "message": "Appium server ready (version 2.11.0)"}
    app = upload_apk(tester, pid)
    cid = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "Login", "platform": "android", "app_id": app["id"],
                                                                "steps": LOGIN_STEPS}).json()["id"]
    tester.post(f"/api/projects/{pid}/mobile/run-groups", json={"provider_id": r.json()["id"], "case_ids": [cid],
                                                                "devices": [{"name": "emulator-5554", "platform": "android"}]})
    run_all()
    link = DEV["caps"]["appium:app"]
    m = re.match(r"^https://testingagent\.unical\.in(/api/public/mobile-apps/\d+/\d+/[0-9a-f]{40}/acme\.apk)$", link)
    assert m, link
    anon = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(tester.app, base_url="https://testingagent.unical.in")
    d = anon.get(m.group(1))
    assert d.status_code == 200 and d.content.startswith(b"PK")
    assert anon.get(m.group(1).replace("/acme.apk", "").rsplit("/", 1)[0] + "/" + "0" * 40 + "/acme.apk").status_code == 404


def _host(stop):
    from testpilot import mobilerun

    def loop():
        while not stop.is_set():
            sid = mobilerun.claim_session("test-host", True)
            if sid:
                threading.Thread(target=mobilerun.live, args=(sid, stop), daemon=True).start()
            stop.wait(0.2)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def _until(c, sid, pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/api/mobile/sessions/{sid}").json()
        if pred(s):
            return s
        time.sleep(0.2)
    raise AssertionError(f"session never reached the state: {s}")


def test_recorder_records_actions_and_saves_case(tester, hub):
    pid = project(tester, "https://acme.example.com")
    secret(pid, "APP_PW", "s3cret-pass")
    prov = bs_provider(tester, pid)
    app = upload_apk(tester, pid)
    stop = threading.Event()
    _host(stop)
    try:
        s = tester.post(f"/api/projects/{pid}/mobile/sessions", json={"purpose": "record", "provider_id": prov["id"], "kind": "native",
                                                                     "app_id": app["id"], "device": {"name": "Pixel 8", "platform": "android"}})
        assert s.status_code == 201, s.text
        sid = s.json()["id"]
        s = _until(tester, sid, lambda s: s["status"] == "ready" and s["screen"]["has_image"])
        els = s["screen"]["elements"]
        by_target = {e["target"]: e for e in els}
        assert {"id=com.acme:id/user", "id=com.acme:id/pass", "~login", "text=Acme Mobile"} <= set(by_target)
        # duplicate labels fall back to a positional selector
        assert "xpath=(//android.widget.Button)[2]" in by_target and "xpath=(//android.widget.Button)[3]" in by_target
        assert tester.get(f"/api/mobile/sessions/{sid}/screen").status_code == 200
        hit = tester.post(f"/api/mobile/sessions/{sid}/inspect", json={"x": 0.5, "y": 0.4}).json()["element"]
        assert hit["target"] == "~login"
        for st in LOGIN_STEPS[:3]:
            r = tester.post(f"/api/mobile/sessions/{sid}/actions", json={"step": st})
            assert r.status_code == 200 and r.json()["screen"]["ok"], r.text
        r = tester.post(f"/api/mobile/sessions/{sid}/actions", json={"step": {"action": "expect_text", "target": "id=com.acme:id/welcome", "text": "nope"}})
        assert not r.json()["screen"]["ok"] and len(r.json()["steps"]) == 3  # failed actions are not recorded
        r = tester.post(f"/api/mobile/sessions/{sid}/actions", json={"step": LOGIN_STEPS[3]}).json()
        assert len(r["steps"]) == 4 and "Welcome, tess" in json.dumps(r["screen"]["elements"])
        bad = tester.post(f"/api/mobile/sessions/{sid}/actions", json={"step": {"action": "execute_script", "script": "x"}})
        assert bad.status_code == 422
        saved = tester.post(f"/api/mobile/sessions/{sid}/save", json={"name": "Recorded login"})
        assert saved.status_code == 201 and saved.json()["source"] == "recorded" and saved.json()["steps"][1]["text"] == "{{secret:APP_PW}}"
        assert tester.post(f"/api/mobile/sessions/{sid}/stop").json()["status"] in ("ready", "closed")
        _until(tester, sid, lambda s: s["status"] == "closed")
        assert DEV.get("quit")
    finally:
        stop.set()


def test_ai_agent_builds_steps(tester, hub, monkeypatch):
    from types import SimpleNamespace

    from testpilot import ai

    plan = [{"thought": "enter user", "action": "type", "ref": None, "target": "id=com.acme:id/user", "text": "tess"},
            {"thought": "bad ref", "action": "tap", "ref": 999},
            {"thought": "password", "action": "type", "target": "id=com.acme:id/pass", "text": "{{secret:APP_PW}}"},
            {"thought": "sign in", "action": "tap", "target": "~login"},
            {"thought": "verify", "action": "expect_text", "target": "id=com.acme:id/welcome", "text": "Welcome"},
            {"thought": "ok", "action": "done", "summary": "Signed in and verified"}]
    seen = []

    def fake_call(**kw):
        seen.append(kw)
        a = plan[len(seen) - 1]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="next_action", input={k: v for k, v in a.items() if v is not None})])

    monkeypatch.setattr(ai, "_call", fake_call)
    monkeypatch.setattr(ai, "status", lambda: {"configured": True, "available": True})
    pid = project(tester, "https://acme.example.com")
    secret(pid, "APP_PW", "s3cret-pass")
    prov = bs_provider(tester, pid)
    app = upload_apk(tester, pid)
    stop = threading.Event()
    _host(stop)
    try:
        r = tester.post(f"/api/projects/{pid}/mobile/sessions", json={"purpose": "ai", "provider_id": prov["id"], "kind": "native",
                                                                     "app_id": app["id"], "device": {"name": "Pixel 8", "platform": "android"},
                                                                     "goal": "Sign in as tess and check the welcome message"})
        assert r.status_code == 201, r.text
        s = _until(tester, r.json()["id"], lambda s: s["phase"] in ("done", "failed"), 60)
        assert s["phase"] == "done", s
        assert [x["action"] for x in s["steps"]] == ["type", "type", "tap", "expect_text"]
        # Claude saw the screen and the secret names, never the secret value
        msg = seen[0]["messages"][0]["content"]
        assert msg[0]["type"] == "image" and "APP_PW" in msg[1]["text"] and "s3cret" not in json.dumps(seen)
        assert "invalid action" in json.dumps(s["log"])
    finally:
        stop.set()


def test_emulator_mobile_web_run(tester, sample_server):
    allow("127.0.0.1")
    pid = project(tester, sample_server)
    secret(pid, "PW", "demo-pass-123")
    r = tester.post(f"/api/projects/{pid}/mobile/providers", json={"kind": "emulator", "name": "Phones"})
    assert r.status_code == 201 and r.json()["status"] == "ok"
    steps = [{"action": "type", "target": "id=username", "text": "demo"},
             {"action": "type", "target": "id=password", "text": "{{secret:PW}}"},
             {"action": "tap", "target": "text=Sign in"},
             {"action": "expect_text", "target": "css=[data-testid=welcome]", "text": "Welcome, demo"},
             {"action": "swipe", "direction": "up"}]
    c = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "Web login", "kind": "web", "platform": "any",
                                                              "start_url": sample_server + "/index.html", "steps": steps})
    assert c.status_code == 201, c.text
    nat = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "App", "platform": "android", "steps": steps[:1]}).json()
    g = tester.post(f"/api/projects/{pid}/mobile/run-groups", json={"provider_id": r.json()["id"], "case_ids": [c.json()["id"], nat["id"]],
                                                                    "devices": [{"name": "iPhone 15 Pro", "platform": "ios"},
                                                                                {"name": "Pixel 7", "platform": "android"}]})
    assert g.status_code == 201, g.text
    assert {s["reason"] for s in g.json()["skipped"]} == {"app tests need a real device provider"}
    run_all()
    d = tester.get(f"/api/mobile/run-groups/{g.json()['id']}").json()
    assert [r["status"] for r in d["runs"]] == ["passed", "passed"], [tester.get(f"/api/mobile/runs/{x['id']}").json() for x in d["runs"]]
    # recording is not offered on the emulator; app tests are refused
    rec = tester.post(f"/api/projects/{pid}/mobile/sessions", json={"purpose": "record", "provider_id": r.json()["id"], "kind": "web",
                                                                   "start_url": sample_server, "device": {"name": "Pixel 7", "platform": "android"}})
    assert rec.status_code == 422


def test_emulator_refuses_unauthorised_hosts(tester, sample_server):
    allow("127.0.0.1")
    pid = project(tester, sample_server)
    prov = tester.post(f"/api/projects/{pid}/mobile/providers", json={"kind": "emulator", "name": "Phones"}).json()
    steps = [{"action": "open_url", "url": "http://169.254.169.254/latest/meta-data/"},
             {"action": "expect_visible", "target": "css=body", "timeout_ms": 1000}]
    cid = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "Escape", "kind": "web", "start_url": sample_server,
                                                                "steps": steps}).json()["id"]
    g = tester.post(f"/api/projects/{pid}/mobile/run-groups", json={"provider_id": prov["id"], "case_ids": [cid],
                                                                    "devices": [{"name": "Pixel 7", "platform": "android"}]}).json()
    run_all()
    run = tester.get(f"/api/mobile/runs/{g['runs'][0]['id']}").json()
    assert run["status"] == "failed" and run["steps"][0]["status"] == "failed"


def test_validation_permissions_and_isolation(app, tester, viewer, hub):
    pid = project(tester, "https://acme.example.com")
    bad = [({"kind": "browserstack", "name": "x", "config": {}, "secret": "k" * 12}, "username"),
           ({"kind": "browserstack", "name": "x", "config": {"username": "u"}}, "access key"),
           ({"kind": "saucelabs", "name": "x", "config": {"username": "u", "region": "mars"}, "secret": "k" * 12}, "region"),
           ({"kind": "unical_lab", "name": "x"}, "not configured")]
    for body, msg in bad:
        r = tester.post(f"/api/projects/{pid}/mobile/providers", json=body)
        assert r.status_code == 422 and msg in r.text, (body, r.text)
    assert viewer.post(f"/api/projects/{pid}/mobile/providers", json={"kind": "emulator", "name": "x"}).status_code == 403
    prov = bs_provider(tester, pid)
    assert viewer.get(f"/api/projects/{pid}/mobile/providers").status_code == 200
    assert "bs-key" not in viewer.get(f"/api/projects/{pid}/mobile/providers").text
    r = tester.post(f"/api/projects/{pid}/mobile/apps", files={"file": ("x.exe", b"x" * 2000, "application/octet-stream")})
    assert r.status_code == 422
    r = tester.post(f"/api/projects/{pid}/mobile/apps", files={"file": ("x.ipa", b"x" * 2000, "application/octet-stream")}, data={"platform": "android"})
    assert r.status_code == 422
    for steps in ([{"action": "tap", "target": "Sign in"}], [{"action": "tap", "target": "~x", "script": "1"}],
                  [{"action": "tap", "target": "text={{secret:PW}}"}], [{"action": "shell", "cmd": "rm"}]):
        r = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "x", "platform": "android", "steps": steps})
        assert r.status_code == 422, steps
    cid = tester.post(f"/api/projects/{pid}/mobile/cases", json={"name": "x", "platform": "android", "steps": [{"action": "back"}]}).json()["id"]
    assert viewer.post(f"/api/projects/{pid}/mobile/run-groups", json={"provider_id": prov["id"], "case_ids": [cid],
                                                                      "devices": [{"name": "P", "platform": "android"}]}).status_code == 403
    r = tester.post(f"/api/projects/{pid}/mobile/run-groups", json={"provider_id": prov["id"], "case_ids": [cid],
                                                                    "devices": [{"name": "P", "platform": "android"}]})
    assert r.status_code == 422 and "no app build" in r.text
    # another organization sees nothing
    make_tenant("other")
    other = login(app, "olga", "tenant_admin", tenant="other")
    assert other.get(f"/api/t/other/mobile/providers/{prov['id']}/devices").status_code == 404
    assert other.get(f"/api/t/other/mobile/cases/{cid}").status_code == 404
    assert other.get(f"/api/t/other/projects/{pid}/mobile/cases").status_code == 404
