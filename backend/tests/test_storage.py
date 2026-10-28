"""Release 1.3: storage policy, organization-owned storage (SharePoint / Google Drive), external-only artifacts."""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, update

from testpilot import artifacts, storage, storage_providers, worker
from testpilot.db import session_scope
from testpilot.models import AuditLog, CaseVersion, Evidence, Run, TenantStorage, utcnow
from testpilot.storage_providers import GoogleDrive, Provider, SharePoint, StorageError, Uploaded, quickxor

from .conftest import H, allow, login, make_tenant


# ============================================================ in-memory organization store
class Fake:
    files: dict[str, bytes] = {}
    paths: dict[str, str] = {}
    folders: set[str] = set()
    down = False
    fail_uploads_after: int | None = None
    deleted: list[str] = []
    ids = itertools.count(1)

    @classmethod
    def reset(cls):
        cls.files, cls.paths, cls.folders, cls.deleted = {}, {}, set(), []
        cls.down, cls.fail_uploads_after = False, None


class FakeStore(Provider):
    def __init__(self, tid="", cid="", secret="", dest=None, login_host=None):
        super().__init__(name="sharepoint", dest=dest or {})
        self.secret = secret

    def _up(self):
        if Fake.down:
            raise StorageError("SharePoint error 503: service unavailable", transient=True)

    def token(self):
        if self.secret == "wrong":
            raise StorageError("Microsoft rejected the app credentials: invalid_client")
        self._up()
        return "tok"

    def search_sites(self, q):
        self._up()
        return [{"id": "site-1", "name": "QA Site", "url": "https://contoso.sharepoint.com/sites/qa"}]

    def libraries(self, site_id):
        return [{"id": "drive-1", "name": "Documents", "url": ""}]

    def folders(self, drive_id, parent_id="root"):
        return [{"id": "folder-evidence", "name": "Evidence"}]

    def create_folder(self, drive_id, parent_id, name):
        return {"id": f"{parent_id}/{name}", "name": name}

    def check(self):
        self._up()
        if not self.dest.get("folder_id"):
            raise StorageError("No SharePoint destination folder has been selected")
        return "Connected. Test file written, verified and deleted."

    def ensure_folder(self, parts):
        self._up()
        p = "/".join(parts)
        Fake.folders.add(p)
        return p

    def upload(self, folder_id, path, name, mime, sha256):
        self._up()
        if Fake.fail_uploads_after is not None:
            if Fake.fail_uploads_after <= 0:
                raise StorageError("SharePoint error 503: busy", transient=True)
            Fake.fail_uploads_after -= 1
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256
        i = f"item-{next(Fake.ids)}"
        Fake.files[i], Fake.paths[i] = data, f"{folder_id}/{name}"
        return Uploaded(ref={"drive_id": "drive-1", "item_id": i}, path="")

    def download(self, ref, max_bytes):
        self._up()
        return Fake.files[ref["item_id"]]

    def delete(self, ref):
        Fake.deleted.append(ref["item_id"])
        Fake.files.pop(ref["item_id"], None)


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    Fake.reset()
    monkeypatch.setitem(storage_providers.FACTORIES, "sharepoint", FakeStore)
    yield


def _setup_external(c, base="/api/admin/storage"):
    r = c.put(f"{base}/pending", json={"provider": "sharepoint", "entra_tenant_id": "t", "client_id": "c",
                                         "client_secret": "s3cret-value"})
    assert r.status_code == 200, r.text
    assert "s3cret" not in r.text and r.json()["pending"]["provider"] == "sharepoint"
    assert c.post(f"{base}/pending/browse", json={"what": "sites", "q": "qa"}).json()["items"][0]["id"] == "site-1"
    assert c.post(f"{base}/pending/browse", json={"what": "libraries", "site_id": "site-1"}).json()["items"][0]["id"] == "drive-1"
    r = c.put(f"{base}/pending/destination", json={"site_id": "site-1", "site_name": "QA Site", "drive_id": "drive-1",
                                                   "drive_name": "Documents", "folder_id": "root/QA", "folder_path": "Documents/QA"})
    assert r.status_code == 200, r.text
    assert c.post(f"{base}/pending/test").json()["ok"] is True
    r = c.post(f"{base}/activate")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "external" and r.json()["provider"] == "sharepoint"
    return r.json()


# ============================================================ policy & choice
def test_new_organization_must_choose_storage_before_running(app):
    make_tenant("acme", storage="")
    owner = login(app, "own", "tenant_owner", tenant="acme")
    st = owner.get("/api/t/acme/admin/storage").json()
    assert st["mode"] == "" and "Choose where" in st["run_block_reason"]
    p = owner.post("/api/t/acme/projects", json={"name": "P", "base_url": "https://example.com"}).json()
    c = owner.post(f"/api/t/acme/projects/{p['id']}/cases", json={"name": "c", "steps": [{"action": "goto", "url": "/"}]}).json()
    r = owner.post(f"/api/t/acme/cases/{c['id']}/runs")
    assert r.status_code in (409, 422)  # storage not chosen (or target not authorised)
    assert owner.post("/api/t/acme/admin/storage/unical", json={"agree": False}).status_code == 422
    r = owner.post("/api/t/acme/admin/storage/unical", json={"agree": True})
    assert r.status_code == 200 and r.json()["mode"] == "unical" and r.json()["unical_consent_source"] == "admin"
    assert r.json()["run_block_reason"] is None
    assert owner.get("/api/t/acme/system/status").json()["storage"]["mode"] == "unical"


def test_storage_settings_need_storage_permission(app, tester):
    assert tester.get("/api/admin/storage").status_code == 403
    assert tester.post("/api/admin/storage/unical", json={"agree": True}).status_code == 403
    assert tester.get("/api/platform/storage").status_code == 403


def test_platform_policy_change_requires_acknowledgement_and_never_switches_silently(admin):
    make_tenant("acme")  # Unical-managed storage
    s = admin.get("/api/platform/storage").json()["settings"]
    body = {k: s[k] for k in ("local_enabled", "sharepoint_enabled", "gdrive_enabled", "default_policy",
                              "default_retention_days", "max_artifact_mb", "spool_max_hours", "base_folder")}
    body["default_policy"] = "external_only"
    r = admin.put("/api/platform/storage", json=body).json()
    assert r["needs_acknowledgement"] and {a["slug"] for a in r["affected"]} == {"unical", "acme"}
    assert admin.get("/api/platform/storage").json()["settings"]["default_policy"] == "choice"  # nothing saved
    r = admin.put("/api/platform/storage", json={**body, "acknowledge": [a["tenant_id"] for a in r["affected"]]})
    assert r.status_code == 200 and "must choose" in r.json()["message"]
    with session_scope() as db:
        rows = db.scalars(select(TenantStorage)).all()
        assert all(x.needs_reselection and x.mode == "unical" for x in rows)  # choice untouched, but flagged
    st = admin.get("/api/admin/storage").json()
    assert "choose the storage again" in st["run_block_reason"]
    assert [o["provider"] for o in st["options"]] == ["sharepoint", "gdrive"]
    assert admin.post("/api/admin/storage/unical", json={"agree": True}).status_code == 403
    _setup_external(admin)
    assert admin.get("/api/admin/storage").json()["run_block_reason"] is None


def test_per_organization_override(admin):
    tid = make_tenant("acme")
    r = admin.put(f"/api/platform/storage/tenants/{tid}", json={"policy_override": "external_only"}).json()
    assert r["needs_acknowledgement"] and r["affected"][0]["slug"] == "acme"
    r = admin.put(f"/api/platform/storage/tenants/{tid}", json={"policy_override": "external_only", "acknowledge": [tid]})
    assert r.status_code == 200
    row = next(t for t in r.json()["tenants"] if t["slug"] == "acme")
    assert row["policy"] == "external_only" and row["needs_reselection"]
    unical = next(t for t in r.json()["tenants"] if t["slug"] == "unical")
    assert not unical["needs_reselection"]


def test_external_setup_validates_and_keeps_secret(admin):
    r = admin.put("/api/admin/storage/pending", json={"provider": "sharepoint", "entra_tenant_id": "t",
                                                      "client_id": "c", "client_secret": "wrong"})
    assert r.status_code == 422 and "invalid_client" in r.json()["detail"]
    assert admin.put("/api/admin/storage/pending", json={"provider": "gdrive", "service_account_json": "{}"}).status_code == 422
    assert admin.post("/api/admin/storage/activate").status_code in (409, 422)
    _setup_external(admin)
    with session_scope() as db:
        row = db.get(TenantStorage, 1)
        assert "s3cret" not in json.dumps(row.config) and "s3cret" not in row.credentials_enc
    assert "s3cret" not in admin.get("/api/admin/storage").text
    with session_scope() as db:
        assert "storage_external_activated" in [a.action for a in db.scalars(select(AuditLog)).all()]


def test_activation_fails_when_store_unreachable(admin):
    admin.put("/api/admin/storage/pending", json={"provider": "sharepoint", "entra_tenant_id": "t", "client_id": "c",
                                                  "client_secret": "x"})
    admin.put("/api/admin/storage/pending/destination", json={"site_id": "s", "drive_id": "d", "folder_id": "f"})
    Fake.down = True
    r = admin.post("/api/admin/storage/activate")
    assert r.status_code == 502
    assert admin.get("/api/admin/storage").json()["mode"] == "unical"  # unchanged


def test_ai_switch(admin):
    p = admin.post("/api/projects", json={"name": "P", "base_url": "https://example.com"}).json()
    admin.put("/api/admin/storage/settings", json={"ai_enabled": False})
    r = admin.post(f"/api/projects/{p['id']}/ai/generate", json={"requirement": "Test the login page works"})
    assert r.status_code == 403 and "data-residency" in r.json()["detail"]


def test_folder_settings_validated(admin):
    assert admin.put("/api/admin/storage/settings", json={"folders": {"evidence_folder": "a/b"}}).status_code == 422
    assert admin.put("/api/admin/storage/settings", json={"folders": {"project_folder": "x"}}).status_code == 422
    r = admin.put("/api/admin/storage/settings", json={"folders": {"evidence_folder": "Evidence"}, "retention_days": 90})
    assert r.status_code == 200 and r.json()["folders"]["evidence_folder"] == "Evidence"


# ============================================================ external-only execution
@pytest.fixture
def ext_project(admin, sample_server):
    allow("127.0.0.1")
    _setup_external(admin)
    p = admin.post("/api/projects", json={"name": "Shop", "base_url": sample_server}).json()
    return p


def _run(c, pid, steps, name="case"):
    case = c.post(f"/api/projects/{pid}/cases", json={"name": name, "steps": steps})
    assert case.status_code == 201, case.text
    r = c.post(f"/api/cases/{case.json()['id']}/runs")
    assert r.status_code == 201, r.text
    assert worker.run_once("pytest") == r.json()["id"]
    return c.get(f"/api/runs/{r.json()['id']}").json(), case.json()


FAILING = [{"action": "goto", "url": "/"},
           {"action": "expect_text", "selector": "h1", "text": "Nonexistent heading", "timeout_ms": 600}]


def test_external_run_stores_everything_in_organization_storage(admin, ext_project):
    run, case = _run(admin, ext_project["id"], FAILING)
    assert run["status"] == "failed" and run["storage_mode"] == "external" and run["upload_state"] == "done"
    assert run["details_external"] is True
    # PostgreSQL keeps only operational metadata
    assert all(s["error"] == "" for s in run["steps"]) and "Nonexistent" not in run["error"]
    with session_scope() as db:
        evs = db.scalars(select(Evidence).where(Evidence.run_id == run["id"])).all()
        assert evs and all(e.storage == "sharepoint" and e.upload_status == "uploaded" and e.rel_path == "" for e in evs)
        kinds = {e.kind for e in evs}
    assert {"screenshot", "log", "report_html", "report_json", "test_case"} <= kinds
    # folder layout: TestAgent-AI/<project>/run-<id>/{evidence,reports,test-cases}
    base = f"TestAgent-AI/Shop ({ext_project['id']})/run-{run['id']}"
    assert {f"{base}/evidence", f"{base}/reports", f"{base}/test-cases"} <= Fake.folders
    # nothing left on Unical storage
    assert not any(storage.spool_root().rglob("run.log"))
    assert not (storage.root() / "tenants" / "1" / "projects" / str(ext_project["id"]) / "runs" / str(run["id"])).exists()
    # details come back from the organization's report
    d = admin.get(f"/api/runs/{run['id']}/details").json()
    assert d["source"] == "organization storage" and "Nonexistent heading" in d["steps"][1]["error"]
    rep = admin.get(f"/api/runs/{run['id']}/report?format=json")
    assert rep.status_code == 200 and rep.json()["steps"][1]["status"] == "failed"
    shot = next(e for s in run["steps"] for e in s["evidence"] if e["kind"] == "screenshot")
    img = admin.get(shot["url"].replace("/api/t/unical", "/api"))
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    # versioned definition queued on save and uploaded by the worker loop
    artifacts.upload_case_versions()
    with session_scope() as db:
        cv = db.scalars(select(CaseVersion).where(CaseVersion.test_case_id == case["id"])).one()
        assert cv.version == 1 and cv.upload_status == "uploaded" and cv.spool_path == ""
    assert any(p.endswith(f"test-cases/case ({case['id']})/v1.json") for p in Fake.paths.values())


def test_store_down_at_start_fails_safely_without_storing(admin, ext_project):
    Fake.down = True
    run, _ = _run(admin, ext_project["id"], FAILING)
    assert run["status"] == "error" and "storage is unavailable" in run["error"] and "nothing was stored" in run["error"]
    assert run["evidence"] == [] and all(not s["evidence"] for s in run["steps"])
    assert not (storage.spool_root() / "tenants" / "1" / "runs" / str(run["id"])).exists()
    assert admin.get("/api/admin/storage").json()["status"] == "error"


def test_upload_failure_waits_in_spool_then_completes(admin, ext_project):
    Fake.fail_uploads_after = 1
    run, _ = _run(admin, ext_project["id"], FAILING)
    assert run["upload_state"] == "pending" and not run["details_external"]
    spool = storage.spool_root() / "tenants" / "1" / "runs" / str(run["id"])
    assert spool.exists() and any(spool.iterdir())
    with session_scope() as db:  # never copied into the backed-up evidence area
        assert not db.scalars(select(Evidence).where(Evidence.run_id == run["id"], Evidence.storage == "local")).first()
    Fake.fail_uploads_after = None
    artifacts.retry_pending()  # the organization's store just failed: backed off, not retried yet
    assert admin.get(f"/api/runs/{run['id']}").json()["upload_state"] == "pending"
    with session_scope() as db:
        db.execute(update(TenantStorage).values(last_check_at=utcnow() - timedelta(minutes=10)))
    artifacts.retry_pending()
    run = admin.get(f"/api/runs/{run['id']}").json()
    assert run["upload_state"] == "done" and run["details_external"] and not spool.exists()


def test_spooled_files_expire_and_are_discarded(admin, ext_project):
    Fake.fail_uploads_after = 0
    run, _ = _run(admin, ext_project["id"], FAILING)
    with session_scope() as db:
        db.execute(update(Run).where(Run.id == run["id"]).values(finished_at=utcnow() - timedelta(hours=30)))
    artifacts.retry_pending()
    run = admin.get(f"/api/runs/{run['id']}").json()
    assert run["upload_state"] == "failed" and run["details_external"]
    assert not (storage.spool_root() / "tenants" / "1" / "runs" / str(run["id"])).exists()
    lost = [e for s in run["steps"] for e in s["evidence"]] + run["evidence"]
    assert lost and all(e["upload_status"] == "lost" and not e["available"] for e in lost)
    assert admin.get(lost[0]["url"].replace("/api/t/unical", "/api")).status_code == 410


def test_retention_deletes_only_with_permission(admin, ext_project):
    run, _ = _run(admin, ext_project["id"], FAILING)
    with session_scope() as db:
        db.execute(update(Evidence).where(Evidence.run_id == run["id"]).values(created_at=utcnow() - timedelta(days=40)))
    admin.put("/api/admin/storage/settings", json={"retention_days": 30, "allow_delete": False})
    assert artifacts.retention_purge() == 0 and not Fake.deleted
    admin.put("/api/admin/storage/settings", json={"retention_days": 30, "allow_delete": True})
    assert artifacts.retention_purge() > 0 and Fake.deleted


def test_delete_local_evidence(admin, sample_server):
    allow("127.0.0.1")
    p = admin.post("/api/projects", json={"name": "Local", "base_url": sample_server}).json()
    run, _ = _run(admin, p["id"], FAILING)
    with session_scope() as db:
        files = [storage.resolve_evidence(e.rel_path) for e in db.scalars(select(Evidence).where(Evidence.run_id == run["id"]))]
    assert files and all(f.exists() for f in files)
    assert admin.post("/api/admin/storage/delete-local", json={"confirm": "nope"}).status_code == 422
    r = admin.post("/api/admin/storage/delete-local", json={"confirm": "unical"})
    assert r.status_code == 200 and r.json()["deleted"] == len(files)
    assert not any(f.exists() for f in files)
    run = admin.get(f"/api/runs/{run['id']}").json()
    assert all(s["error"] == "" for s in run["steps"])
    assert admin.get(run["evidence"][0]["url"].replace("/api/t/unical", "/api")).status_code == 410


def test_other_organization_cannot_see_storage(app, admin):
    _setup_external(admin)
    make_tenant("acme")
    other = login(app, "o", "tenant_admin", tenant="acme")
    st = other.get("/api/t/acme/admin/storage").json()
    assert st["mode"] == "unical" and st["destination"] == {}


# ============================================================ real clients against mocked HTTP
def _client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_sharepoint_upload_and_verify(monkeypatch, tmp_path):
    small, big = tmp_path / "a.jpg", tmp_path / "b.webm"
    small.write_bytes(b"x" * 1000)
    big.write_bytes(bytes(range(256)) * 20000)  # 5 MB: upload session
    stored: dict[str, bytes] = {}
    session_parts: list[str] = []

    def handler(req: httpx.Request):
        u = str(req.url)
        if "oauth2/v2.0/token" in u:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        assert req.headers.get("authorization") == "Bearer T" or "upload-session" in u
        if u.endswith(":/a.jpg:/content") and req.method == "PUT":
            stored["a"] = req.content
            return httpx.Response(201, json={"id": "A", "size": len(req.content), "webUrl": "w",
                                             "file": {"hashes": {"quickXorHash": quickxor(small)}}})
        if u.endswith("createUploadSession"):
            stored["b"] = b""
            return httpx.Response(200, json={"uploadUrl": "https://upload-session.example/u1"})
        if "upload-session" in u:
            assert "authorization" not in req.headers  # pre-authorised URL
            session_parts.append(req.headers["content-range"])
            stored["b"] = stored.get("b", b"") + req.content
            if len(stored["b"]) < big.stat().st_size:
                return httpx.Response(202, json={})
            return httpx.Response(201, json={"id": "B", "size": len(stored["b"]),
                                             "file": {"hashes": {"quickXorHash": "not-matching"}}})
        if u.endswith("/items/B/content"):  # verification download when the hash cannot be matched
            return httpx.Response(200, content=stored["b"])
        return httpx.Response(404, json={"error": {"message": "nope " + u}})

    monkeypatch.setattr(storage_providers, "_http", _client(handler))
    sp = SharePoint("tid", "cid", "sec", {"drive_id": "D", "folder_id": "F"})
    up = sp.upload("F", small, "a.jpg", "image/jpeg", hashlib.sha256(small.read_bytes()).hexdigest())
    assert up.ref == {"drive_id": "D", "item_id": "A"}
    up = sp.upload("F", big, "b.webm", "video/webm", hashlib.sha256(big.read_bytes()).hexdigest())
    assert up.ref["item_id"] == "B" and stored["b"] == big.read_bytes() and len(session_parts) == 2
    with pytest.raises(StorageError, match="differs"):
        sp.upload("F", big, "b.webm", "video/webm", "0" * 64)


def test_sharepoint_permission_error_explains_least_privilege(monkeypatch):
    def handler(req):
        if "token" in str(req.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(403, json={"error": {"message": "Access denied"}})
    monkeypatch.setattr(storage_providers, "_http", _client(handler))
    with pytest.raises(StorageError, match="Sites.Selected"):
        SharePoint("t", "c", "s", {}).libraries("site")


def _sa_key() -> str:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return json.dumps({"type": "service_account", "client_email": "tp@proj.iam.gserviceaccount.com", "private_key": pem,
                       "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token", "client_secret": "dropped"})


def test_google_drive_resumable_upload_and_shared_drive_check(monkeypatch, tmp_path):
    f = tmp_path / "r.html"
    f.write_bytes(b"<html>" + b"y" * 9_000_000)
    got = bytearray()

    def handler(req: httpx.Request):
        u = str(req.url)
        if u.startswith("https://oauth2.googleapis.com/token"):
            assert b"jwt-bearer" in req.content
            return httpx.Response(200, json={"access_token": "G", "expires_in": 3600})
        if "uploadType=resumable" in u:
            assert "supportsAllDrives=true" in u
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/1"})
        if u.startswith("https://www.googleapis.com/upload/session/1"):
            got.extend(req.content)
            if len(got) < f.stat().st_size:
                return httpx.Response(308)
            return httpx.Response(200, json={"id": "G1", "size": str(len(got)),
                                             "sha256Checksum": hashlib.sha256(bytes(got)).hexdigest()})
        if "/files/MYDRIVE" in u:
            return httpx.Response(200, json={"id": "MYDRIVE", "mimeType": "application/vnd.google-apps.folder"})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    monkeypatch.setattr(storage_providers, "_http", _client(handler))
    sa = GoogleDrive.parse_key(_sa_key())
    assert "client_secret" not in sa
    g = GoogleDrive(sa, {"drive_id": "SD", "folder_id": "F"})
    up = g.upload("F", f, "r.html", "text/html", hashlib.sha256(f.read_bytes()).hexdigest())
    assert up.ref["file_id"] == "G1" and bytes(got) == f.read_bytes()
    g2 = GoogleDrive(sa, {"drive_id": "SD", "folder_id": "MYDRIVE"})
    with pytest.raises(StorageError, match="Shared Drive"):
        g2.check()


def test_google_key_validation():
    for bad in ("not json", "{}", json.dumps({"type": "authorized_user"})):
        with pytest.raises(StorageError):
            GoogleDrive.parse_key(bad)


# ============================================================ review regressions
def _plat_body(admin):
    s = admin.get("/api/platform/storage").json()["settings"]
    return {k: s[k] for k in ("local_enabled", "sharepoint_enabled", "gdrive_enabled", "default_policy",
                              "default_retention_days", "max_artifact_mb", "spool_max_hours", "base_folder")}


def test_platform_cannot_leave_organizations_without_options(admin):
    b = {**_plat_body(admin), "default_policy": "external_only", "sharepoint_enabled": False, "gdrive_enabled": False}
    assert admin.put("/api/platform/storage", json=b).status_code == 422
    tid = make_tenant("acme")
    admin.put("/api/platform/storage", json={**_plat_body(admin), "sharepoint_enabled": False, "gdrive_enabled": False})
    assert admin.put(f"/api/platform/storage/tenants/{tid}", json={"policy_override": "external_only"}).status_code == 422


def test_queued_run_follows_storage_and_delete_local_waits_for_runs(admin, ext_project):
    case = admin.post(f"/api/projects/{ext_project['id']}/cases", json={"name": "q", "steps": FAILING}).json()
    run = admin.post(f"/api/cases/{case['id']}/runs").json()
    assert run["storage_mode"] == "external" and run["storage_provider"] == "sharepoint"
    assert admin.post("/api/admin/storage/delete-local", json={"confirm": "unical"}).status_code == 409
    admin.post(f"/api/runs/{run['id']}/cancel")
    assert admin.post("/api/admin/storage/delete-local", json={"confirm": "unical"}).status_code == 200


def test_pending_uploads_never_go_to_a_different_provider(admin, ext_project):
    Fake.fail_uploads_after = 0
    run, _ = _run(admin, ext_project["id"], FAILING)
    with session_scope() as db:
        db.execute(update(TenantStorage).where(TenantStorage.tenant_id == 1).values(provider="gdrive"))
    Fake.fail_uploads_after = None
    assert artifacts.upload_run(run["id"]) is False
    assert admin.get(f"/api/runs/{run['id']}").json()["upload_state"] == "pending" and not Fake.files


def test_one_uploader_per_run(admin, ext_project):
    Fake.fail_uploads_after = 0
    run, _ = _run(admin, ext_project["id"], FAILING)
    Fake.fail_uploads_after = None
    with artifacts._run_lock(run["id"]) as mine:
        assert mine
        assert artifacts.upload_run(run["id"]) is False  # another worker holds it
    assert artifacts.upload_run(run["id"]) is True


def test_reaped_and_stray_spool_runs_do_not_persist(admin, ext_project):
    run, _ = _run(admin, ext_project["id"], FAILING)
    stray = storage.spool_root() / "tenants" / "1" / "runs" / "999999"
    stray.mkdir(parents=True)
    (stray / "x.jpg").write_bytes(b"x")
    assert artifacts.sweep_spool(24) >= 1 and not stray.exists()
    with session_scope() as db:  # a run that crashed while running
        db.execute(update(Run).where(Run.id == run["id"]).values(status="running", upload_state="",
                                                                heartbeat_at=utcnow() - timedelta(hours=2)))
    worker.reap_stale()
    assert admin.get(f"/api/runs/{run['id']}").json()["upload_state"] == "pending"


def test_retention_survives_provider_change(admin, ext_project):
    run, _ = _run(admin, ext_project["id"], FAILING)
    with session_scope() as db:
        db.execute(update(Evidence).where(Evidence.run_id == run["id"]).values(created_at=utcnow() - timedelta(days=40)))
        db.execute(update(TenantStorage).where(TenantStorage.tenant_id == 1).values(provider="gdrive"))
    admin.put("/api/admin/storage/settings", json={"retention_days": 30, "allow_delete": True})
    assert artifacts.retention_purge() == 0  # SharePoint files are not touched through the Drive client
