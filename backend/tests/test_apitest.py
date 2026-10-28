"""API testing (release 1.6): generation from OpenAPI / endpoint lists, runs through the egress proxy,
credentials handling, write protection, reports, permissions and tenant isolation."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import allow, login, make_tenant

KEY = "k-123-super-secret"
SPEC = """
openapi: 3.0.3
info: {title: Pets, version: '1.0'}
security: [{key: []}]
components:
  securitySchemes: {key: {type: apiKey, in: header, name: X-API-Key}}
  schemas:
    Pet:
      type: object
      required: [id, name]
      properties: {id: {type: integer}, name: {type: string}, status: {type: string, enum: [available, sold]}}
    NewPet: {type: object, required: [name], properties: {name: {type: string}}}
paths:
  /pets:
    get:
      operationId: listPets
      parameters: [{name: limit, in: query, required: true, schema: {type: integer, default: 10}}]
      responses:
        '200': {description: ok, content: {application/json: {schema: {type: array, items: {$ref: '#/components/schemas/Pet'}}}}}
    post:
      operationId: createPet
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/NewPet'}}}}
      responses:
        '201': {description: created, content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}}
  /pets/{petId}:
    get:
      operationId: getPet
      parameters: [{name: petId, in: path, required: true, schema: {type: integer}}]
      responses:
        '200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}}
"""
STATE = {"posts": 0, "down": False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        if self.headers.get("X-API-Key") != KEY:
            self._j(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.path == "/openapi.yaml":
            b = SPEC.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if self.path.startswith("/health"):
            return self._j(503, {"status": "down"}) if STATE["down"] else self._j(200, {"status": "ok"})
        if not self._auth():
            return
        if self.path.split("?")[0] == "/pets":
            if "limit=" not in self.path:
                return self._j(400, {"error": "limit required"})
            return self._j(200, [{"id": 7, "name": "Rex", "status": "available"}])
        if self.path.startswith("/pets/"):
            pid = self.path.split("/")[2]
            if not pid.isdigit():
                return self._j(400, {"error": "bad id"})
            return self._j(200, {"id": 7, "name": "Rex"}) if pid == "7" else self._j(404, {"error": "nf"})
        self._j(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if "name" not in body:
            return self._j(422, {"error": "name required"})
        STATE["posts"] += 1
        self._j(201, {"id": 8, **body})


@pytest.fixture(scope="module")
def fake_api():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.update(posts=0, down=False)


def project(c, base, prefix="/api"):
    r = c.post(f"{prefix}/projects", json={"name": "Pets", "base_url": base})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def suite(c, pid, base, prefix="/api", **kw):
    body = {"name": "Pets API", "base_url": base, "spec_source": "upload", "spec_text": SPEC,
            "auth": {"type": "api_key", "config": {"name": "X-API-Key", "location": "header"}, "secrets": {"api_key": KEY}},
            "run": True, **kw}
    r = c.post(f"{prefix}/projects/{pid}/api-suites", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def work():
    from testpilot import apitesting

    n = 0
    while apitesting.tick("test-worker"):
        n += 1
        assert n < 10
    return n


def test_upload_spec_generates_runs_and_reports(tester, fake_api):
    allow("127.0.0.1")
    pid = project(tester, fake_api)
    s = suite(tester, pid, fake_api)
    assert KEY not in json.dumps(s)
    assert s["generation"]["generated"] >= 8 and s["generation"]["run"]
    assert s["spec_title"] == "Pets 1.0" and s["has_credentials"] and s["auth_hint"].startswith("API key")
    cats = {c["category"] for c in tester.get(f"/api/api-suites/{s['id']}/cases").json()}
    assert {"positive", "auth", "negative"} <= cats
    work()
    run = tester.get(f"/api/api-runs/{s['generation']['run']}").json()
    assert run["status"] == "passed", [r for r in run["results"] if r["status"] in ("failed", "error")]
    by = {r["name"]: r for r in run["results"]}
    get_pet = next(r for n, r in by.items() if n.startswith("GET /pets/{petId} — returns 200"))
    assert get_pet["url"].endswith("/pets/7")  # id carried over from GET /pets
    writes = [r for r in run["results"] if r["method"] == "POST"]
    assert writes and all(r["status"] == "skipped" for r in writes)
    assert STATE["posts"] == 0  # read-only run: nothing was written
    dumped = json.dumps(run)
    assert KEY not in dumped and "[secret]" in dumped
    html = tester.get(f"/api/api-runs/{run['id']}/report?format=html")
    assert html.status_code == 200 and "API test report" in html.text and KEY not in html.text
    assert "attachment" in html.headers["content-disposition"] and "default-src 'none'" in html.headers["content-security-policy"]
    rep = tester.get(f"/api/api-runs/{run['id']}/report?format=json").json()
    assert rep["fingerprint"]["value"] == run["report_sha256"] and rep["summary"]["passed"] == run["summary"]["passed"]


def test_write_tests_only_when_allowed_and_requested(tester, fake_api):
    allow("127.0.0.1")
    pid = project(tester, fake_api)
    s = suite(tester, pid, fake_api, run=False)
    r = tester.post(f"/api/api-suites/{s['id']}/runs", json={"include_writes": True})
    assert r.status_code == 422  # writes not allowed for the suite
    upd = {k: s[k] for k in ("name", "base_url", "spec_source", "spec_url", "endpoints_text", "requires_auth", "verify_tls",
                             "timeout_ms", "max_response_ms", "default_headers")}
    assert tester.put(f"/api/api-suites/{s['id']}", json={**upd, "allow_writes": True}).status_code == 200
    rid = tester.post(f"/api/api-suites/{s['id']}/runs", json={"include_writes": True}).json()["id"]
    work()
    run = tester.get(f"/api/api-runs/{rid}").json()
    post = [r for r in run["results"] if r["method"] == "POST" and r["category"] == "positive"][0]
    assert post["status"] == "passed" and post["http_status"] == 201 and STATE["posts"] >= 1


def test_unauthorised_host_is_not_contacted(tester, fake_api):
    pid = project(tester, fake_api)  # 127.0.0.1 NOT on the allowlist
    s = suite(tester, pid, fake_api)
    assert s["generation"]["run"] is None and "allowlist" in s["generation"]["run_error"]
    assert s["policy_problems"]
    assert tester.post(f"/api/api-suites/{s['id']}/runs", json={}).status_code == 409


def test_openapi_url_is_imported_by_the_worker_then_run(tester, fake_api):
    allow("127.0.0.1")
    pid = project(tester, fake_api)
    r = tester.post(f"/api/projects/{pid}/api-suites", json={
        "name": "By URL", "spec_source": "url", "spec_url": f"{fake_api}/openapi.yaml", "base_url": fake_api,
        "auth": {"type": "api_key", "config": {"name": "X-API-Key"}, "secrets": {"api_key": KEY}}, "run": True})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["import_state"] == "pending" and s["case_count"] == 0
    work()
    s = tester.get(f"/api/api-suites/{s['id']}").json()
    assert s["import_state"] == "done" and s["case_count"] >= 8 and s["last_run"]["status"] == "passed"


def test_endpoint_list_and_failed_expectation(tester, fake_api):
    allow("127.0.0.1")
    pid = project(tester, fake_api)
    r = tester.post(f"/api/projects/{pid}/api-suites", json={
        "name": "Manual", "spec_source": "manual", "base_url": fake_api,
        "endpoints_text": "GET /pets?limit=5\nGET /pets/{id}\nGET /pets/{id} -> 418",
        "auth": {"type": "api_key", "config": {"name": "X-API-Key"}, "secrets": {"api_key": KEY}}, "run": True})
    assert r.status_code == 201, r.text
    work()
    run = tester.get(f"/api/api-runs/{r.json()['generation']['run']}").json()
    assert run["status"] == "failed"
    bad = [x for x in run["results"] if x["status"] == "failed"]
    assert len(bad) == 1 and "418" in bad[0]["assertions"][0]["name"]
    assert any(x["category"] == "auth" and x["status"] == "passed" for x in run["results"])


def test_bad_inputs_are_rejected(tester, fake_api):
    pid = project(tester, fake_api)
    base = {"name": "x", "base_url": fake_api}
    assert tester.post(f"/api/projects/{pid}/api-suites", json={**base, "spec_source": "upload", "spec_text": "nope"}).status_code == 422
    assert tester.post(f"/api/projects/{pid}/api-suites", json={**base, "spec_source": "manual", "endpoints_text": "FETCH x"}).status_code == 422
    assert tester.post(f"/api/projects/{pid}/api-suites", json={**base, "base_url": "file:///etc/passwd", "spec_source": "manual",
                                                                  "endpoints_text": "GET /"}).status_code == 422
    r = tester.post(f"/api/projects/{pid}/api-suites", json={**base, "spec_source": "manual", "endpoints_text": "GET /",
                                                              "default_headers": {"Authorization": "Bearer x"}})
    assert r.status_code == 422
    s = suite(tester, pid, fake_api, run=False)
    case = {"name": "abs", "definition": {"method": "GET", "path": "https://evil.example/x", "expect": {"status": [200]}}}
    assert tester.post(f"/api/api-suites/{s['id']}/cases", json=case).status_code == 422


def test_permissions_and_tenant_isolation(app, tester, viewer, fake_api):
    pid = project(tester, fake_api)
    s = suite(tester, pid, fake_api, run=False)
    assert viewer.get(f"/api/api-suites/{s['id']}").status_code == 200
    assert viewer.post(f"/api/projects/{pid}/api-suites", json={"name": "v", "base_url": fake_api, "spec_source": "manual",
                                                                 "endpoints_text": "GET /"}).status_code == 403
    assert viewer.post(f"/api/api-suites/{s['id']}/runs", json={}).status_code == 403
    assert viewer.put(f"/api/api-suites/{s['id']}/credentials", json={"type": "bearer", "secrets": {"token": "x"}}).status_code == 403
    make_tenant("acme")
    other = login(app, "eve", "tenant_admin", tenant="acme")
    assert other.get(f"/api/t/acme/api-suites/{s['id']}").status_code == 404
    assert other.get(f"/api/t/acme/api-cases/1").status_code in (404, 405)


def test_spec_generator_units():
    from testpilot import apiexec, apispec

    doc = apispec.parse_spec_text(json.dumps({
        "swagger": "2.0", "info": {"title": "S2", "version": "2"}, "host": "api.example.com", "basePath": "/v2", "schemes": ["https"],
        "paths": {"/items/{id}": {"get": {"parameters": [{"name": "id", "in": "path", "required": True, "type": "integer"}],
                                          "responses": {"200": {"description": "ok", "schema": {"type": "object", "required": ["id"],
                                                                                                "properties": {"id": {"type": "integer"}}}}}}}}}))
    assert apispec.spec_base_url(doc) == "https://api.example.com/v2"
    cases = apispec.generate_from_spec(doc, has_auth=False, max_ms=500)
    assert any(c["category"] == "negative" and c["definition"]["path_params"].get("id") == "invalid-id-x" for c in cases)
    assert apispec.validate({"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}, {"id": "x"})
    assert not apispec.validate({"type": "array", "items": {"type": "integer", "nullable": True}}, [1, None])
    assert apispec.status_matches(["2xx"], 204) and not apispec.status_matches([200], 201)
    with pytest.raises(apispec.SpecError):
        apispec.parse_endpoints("GET users")
    red = apiexec.Redactor(["s3cr3t"], query_keys=("api_key",))
    assert red.url("https://h/x?api_key=s3cr3t&a=1") == "https://h/x?api_key=%5Bsecret%5D&a=1"
    assert red.headers({"Authorization": "Bearer s3cr3t", "X-Other": "s3cr3t"}) == {"Authorization": "[secret]", "X-Other": "[secret]"}
