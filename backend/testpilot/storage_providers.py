"""Clients for the organization's own storage: SharePoint (Microsoft Graph) and Google Drive.

Both use credentials the customer creates and controls; Unical has no global credential that can
reach customer resources:

  SharePoint   the customer's Microsoft Entra app registration (client credentials). Least privilege:
               Graph application permission Sites.Selected, granted write access to the one site.
  Google Drive the customer's Google Cloud service account key. The customer adds the service account
               to a Shared Drive (Content manager). Service accounts have no storage quota of their
               own, so files must live in a Shared Drive, not in someone's My Drive.

Every upload is verified (size + provider hash; if the hash format cannot be checked, the file is
downloaded back and compared with SHA-256). HTTP goes through `_http()` so tests can substitute a
mock transport.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx
import jwt

GRAPH = "https://graph.microsoft.com/v1.0"
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SMALL_UPLOAD = 4 * 1024 * 1024          # Graph simple-upload limit
CHUNK = 10 * 320 * 1024                 # Graph upload-session chunks must be multiples of 320 KiB
GCHUNK = 32 * 256 * 1024                # Drive resumable chunks must be multiples of 256 KiB


class StorageError(RuntimeError):
    """A storage operation failed. `transient` errors (network, 429, 5xx) are worth retrying."""

    def __init__(self, msg: str, transient: bool = False):
        super().__init__(msg)
        self.transient = transient


def _http() -> httpx.Client:  # replaced in tests
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


def clean_name(name: str, fallback: str = "item") -> str:
    """A file/folder name valid in SharePoint and Google Drive."""
    n = re.sub(r'[\x00-\x1f"*:<>?/\\|#%]+', "-", name or "").strip().strip(".").strip()
    n = re.sub(r"\s{2,}", " ", n)[:100].strip()
    return n or fallback


def quickxor(path: Path) -> str:
    """Microsoft's QuickXorHash (base64), the hash SharePoint/OneDrive report for uploaded files.

    Byte i is XORed into a 160-bit circular register at bit offset (i * 11) mod 160; the file length
    (little-endian, 64-bit) is then XORed into the last 8 bytes."""
    width, mask = 160, (1 << 160) - 1
    lanes = bytearray(160)  # lanes[r] = XOR of all bytes at positions ≡ r (mod 160)
    length = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            pad = length % 160
            buf = bytes(pad) + chunk
            buf += bytes((-len(buf)) % 160)
            acc = 0
            for off in range(0, len(buf), 160):
                acc ^= int.from_bytes(buf[off:off + 160], "little")
            for r, b in enumerate(acc.to_bytes(160, "little")):
                lanes[r] ^= b
            length += len(chunk)
    reg = 0
    for r, b in enumerate(lanes):
        if b:
            s = (r * 11) % width
            reg ^= ((b << s) | (b >> (width - s))) & mask if s else b
    out = bytearray(reg.to_bytes(20, "little"))
    for i, lb in enumerate(length.to_bytes(8, "little")):
        out[12 + i] ^= lb
    return base64.b64encode(bytes(out)).decode()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Uploaded:
    ref: dict[str, Any]   # provider identifiers needed to fetch/delete the file later
    path: str             # human-readable location ("TestAgent-AI/Project (3)/run-12/evidence/x.jpg")
    web_url: str = ""


@dataclass
class Provider:
    """Base class. `dest` is the configured destination (site/drive/folder ids)."""

    name: str = ""
    dest: dict[str, Any] = field(default_factory=dict)
    _folders: dict[tuple, str] = field(default_factory=dict)

    # -- interface
    def check(self) -> str: ...
    def ensure_folder(self, parts: list[str]) -> str: ...
    def upload(self, folder_id: str, path: Path, name: str, mime: str, sha256: str) -> Uploaded: ...
    def download(self, ref: dict[str, Any], max_bytes: int) -> bytes: ...
    def delete(self, ref: dict[str, Any]) -> None: ...

    def base_path(self) -> str:
        return self.dest.get("folder_path", "")


# ============================================================================ SharePoint
class SharePoint(Provider):
    def __init__(self, entra_tenant_id: str, client_id: str, client_secret: str, dest: dict[str, Any],
                 login_host: str = "login.microsoftonline.com"):
        super().__init__(name="sharepoint", dest=dest or {})
        self.tid, self.cid, self.secret, self.login_host = entra_tenant_id, client_id, client_secret, login_host
        self._token: tuple[str, float] | None = None
        self.c = _http()

    # -- auth
    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        if not (self.tid and self.cid and self.secret):
            raise StorageError("SharePoint credentials are incomplete")
        try:
            r = self.c.post(f"https://{self.login_host}/{quote(self.tid)}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.secret,
                "scope": "https://graph.microsoft.com/.default"})
        except httpx.HTTPError as e:
            raise StorageError(f"Cannot reach Microsoft sign-in: {e}", transient=True)
        if r.status_code != 200:
            j = _json(r)
            raise StorageError("Microsoft rejected the app credentials: "
                               + (j.get("error_description") or j.get("error") or f"HTTP {r.status_code}").split("\r\n")[0][:300])
        j = r.json()
        self._token = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        return self._token[0]

    def _req(self, method: str, url: str, ok=(200, 201, 204), **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token()}", **kw.pop("headers", {})}
        try:
            r = self.c.request(method, url if url.startswith("http") else GRAPH + url, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise StorageError(f"SharePoint request failed: {e}", transient=True)
        if r.status_code not in ok:
            raise _graph_error(r)
        return r

    # -- discovery (used by the configuration UI)
    def search_sites(self, q: str) -> list[dict]:
        q = (q or "").strip()
        if q.startswith("https://"):
            u = urlsplit(q)
            path = re.sub(r"/(Shared%20Documents|Shared Documents|SitePages|Forms).*$", "", u.path).rstrip("/")
            r = self._req("GET", f"/sites/{u.hostname}:{quote(path) or '/'}" if path else f"/sites/{u.hostname}")
            s = r.json()
            return [{"id": s["id"], "name": s.get("displayName") or s.get("name"), "url": s.get("webUrl", "")}]
        r = self._req("GET", "/sites", params={"search": q or "*", "$top": "50"})
        return [{"id": s["id"], "name": s.get("displayName") or s.get("name"), "url": s.get("webUrl", "")}
                for s in r.json().get("value", [])]

    def libraries(self, site_id: str) -> list[dict]:
        r = self._req("GET", f"/sites/{quote(site_id, safe=',')}/drives", params={"$select": "id,name,webUrl,driveType"})
        return [{"id": d["id"], "name": d["name"], "url": d.get("webUrl", "")} for d in r.json().get("value", [])]

    def folders(self, drive_id: str, parent_id: str = "root") -> list[dict]:
        out, url = [], f"/drives/{quote(drive_id)}/items/{quote(parent_id)}/children"
        params: dict | None = {"$select": "id,name,folder,webUrl", "$top": "200"}
        while url and len(out) < 1000:
            j = self._req("GET", url, params=params).json()
            out += [{"id": i["id"], "name": i["name"]} for i in j.get("value", []) if "folder" in i]
            url, params = j.get("@odata.nextLink"), None
        return sorted(out, key=lambda x: x["name"].lower())

    def create_folder(self, drive_id: str, parent_id: str, name: str) -> dict:
        name = clean_name(name, "folder")
        r = self._req("POST", f"/drives/{quote(drive_id)}/items/{quote(parent_id)}/children", ok=(200, 201, 409),
                      json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
        if r.status_code == 409:  # already exists: use it
            r = self._req("GET", f"/drives/{quote(drive_id)}/items/{quote(parent_id)}:/{quote(name)}")
        j = r.json()
        return {"id": j["id"], "name": j["name"]}

    # -- runtime
    def _drive(self) -> str:
        d = self.dest.get("drive_id")
        if not d or not self.dest.get("folder_id"):
            raise StorageError("No SharePoint destination folder has been selected")
        return d

    def check(self) -> str:
        d = self._drive()
        j = self._req("GET", f"/drives/{quote(d)}/items/{quote(self.dest['folder_id'])}").json()
        if "folder" not in j:
            raise StorageError("The selected SharePoint destination is not a folder")
        probe = self._probe()
        return f"Connected to {self.dest.get('site_name') or 'SharePoint'} / {self.dest.get('folder_path') or j.get('name')}. {probe}"

    def _probe(self) -> str:
        import tempfile

        with tempfile.NamedTemporaryFile(prefix="tp-probe-", suffix=".txt") as f:
            f.write(b"TestAgent storage check. This file is deleted automatically.\n")
            f.flush()
            p = Path(f.name)
            up = self.upload(self.dest["folder_id"], p, ".testpilot-check.txt", "text/plain", sha256_file(p))
        try:
            self.delete(up.ref)
            return "Test file written, verified and deleted."
        except StorageError as e:
            return f"Test file written and verified, but could not be deleted ({e}); remove .testpilot-check.txt manually."

    def ensure_folder(self, parts: list[str]) -> str:
        d = self._drive()
        cur = self.dest["folder_id"]
        for i, part in enumerate(parts):
            key = tuple(parts[: i + 1])
            if key in self._folders:
                cur = self._folders[key]
                continue
            cur = self.create_folder(d, cur, part)["id"]
            self._folders[key] = cur
        return cur

    def upload(self, folder_id: str, path: Path, name: str, mime: str, sha256: str) -> Uploaded:
        d, name = self._drive(), clean_name(name, "file")
        size = path.stat().st_size
        base = f"/drives/{quote(d)}/items/{quote(folder_id)}:/{quote(name)}:"
        if size <= SMALL_UPLOAD:
            item = self._req("PUT", f"{base}/content", content=path.read_bytes(),
                             headers={"Content-Type": mime or "application/octet-stream"}).json()
        else:
            sess = self._req("POST", f"{base}/createUploadSession",
                             json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
            item = self._session_upload(sess["uploadUrl"], path, size)
        self._verify(item, path, size, sha256)
        return Uploaded(ref={"drive_id": d, "item_id": item["id"]}, path="", web_url=item.get("webUrl", ""))

    def _session_upload(self, url: str, path: Path, size: int) -> dict:
        with path.open("rb") as f:
            start = 0
            while start < size:
                data = f.read(CHUNK)
                end = start + len(data) - 1
                try:  # the upload URL is pre-authorised: no Authorization header
                    r = self.c.put(url, content=data, headers={"Content-Length": str(len(data)),
                                                               "Content-Range": f"bytes {start}-{end}/{size}"})
                except httpx.HTTPError as e:
                    raise StorageError(f"SharePoint upload interrupted: {e}", transient=True)
                if r.status_code not in (200, 201, 202):
                    raise _graph_error(r)
                start = end + 1
        return r.json()

    def _verify(self, item: dict, path: Path, size: int, sha256: str) -> None:
        if int(item.get("size", -1)) != size:
            raise StorageError(f"Upload verification failed: SharePoint reports {item.get('size')} bytes, expected {size}")
        qx = ((item.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        if qx and qx == quickxor(path):
            return
        # No hash, or one we could not match: compare the stored bytes themselves.
        data = self.download({"drive_id": self._drive(), "item_id": item["id"]}, size + 1)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError("Upload verification failed: stored file differs from the original")

    def download(self, ref: dict[str, Any], max_bytes: int) -> bytes:
        r = self._req("GET", f"/drives/{quote(ref['drive_id'])}/items/{quote(ref['item_id'])}/content")
        if len(r.content) > max_bytes:
            raise StorageError("File is larger than the download limit")
        return r.content

    def delete(self, ref: dict[str, Any]) -> None:
        self._req("DELETE", f"/drives/{quote(ref['drive_id'])}/items/{quote(ref['item_id'])}", ok=(204, 200, 404))


def _graph_error(r: httpx.Response) -> StorageError:
    j = _json(r)
    msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error_description")
    hint = ""
    if r.status_code in (401, 403):
        hint = (" — the app registration needs Microsoft Graph application permission Sites.Selected "
                "(granted write access to this site) or Sites.ReadWrite.All, with admin consent")
    return StorageError(f"SharePoint error {r.status_code}: {(msg or r.reason_phrase)[:300]}{hint}",
                        transient=r.status_code == 429 or r.status_code >= 500)


# ============================================================================ Google Drive
class GoogleDrive(Provider):
    def __init__(self, service_account: dict[str, Any], dest: dict[str, Any]):
        super().__init__(name="gdrive", dest=dest or {})
        self.sa = service_account
        self._token: tuple[str, float] | None = None
        self.c = _http()

    @staticmethod
    def parse_key(text: str) -> dict[str, Any]:
        try:
            sa = json.loads(text)
        except ValueError:
            raise StorageError("The service-account key is not valid JSON")
        if sa.get("type") != "service_account" or not sa.get("client_email") or not sa.get("private_key"):
            raise StorageError("This is not a Google service-account key (expected type service_account "
                               "with client_email and private_key)")
        return {k: sa[k] for k in ("type", "client_email", "private_key", "private_key_id", "token_uri", "project_id") if k in sa}

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        uri = self.sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        if urlsplit(uri).hostname != "oauth2.googleapis.com":
            raise StorageError("Unexpected token_uri in the service-account key")
        now = int(time.time())
        try:
            assertion = jwt.encode({"iss": self.sa["client_email"], "scope": "https://www.googleapis.com/auth/drive",
                                    "aud": uri, "iat": now, "exp": now + 3600}, self.sa["private_key"],
                                   algorithm="RS256", headers={"kid": self.sa.get("private_key_id", "")})
        except Exception:
            raise StorageError("The service-account private key could not be used")
        try:
            r = self.c.post(uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        except httpx.HTTPError as e:
            raise StorageError(f"Cannot reach Google sign-in: {e}", transient=True)
        if r.status_code != 200:
            j = _json(r)
            raise StorageError("Google rejected the service-account key: "
                               + str(j.get("error_description") or j.get("error") or r.status_code)[:300])
        j = r.json()
        self._token = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        return self._token[0]

    def _req(self, method: str, url: str, ok=(200, 201, 204), **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token()}", **kw.pop("headers", {})}
        params = {"supportsAllDrives": "true", **(kw.pop("params", None) or {})}
        try:
            r = self.c.request(method, url if url.startswith("http") else DRIVE + url, headers=headers, params=params, **kw)
        except httpx.HTTPError as e:
            raise StorageError(f"Google Drive request failed: {e}", transient=True)
        if r.status_code not in ok:
            raise _drive_error(r)
        return r

    def shared_drives(self) -> list[dict]:
        r = self._req("GET", "/drives", params={"pageSize": "100"})
        return [{"id": d["id"], "name": d["name"]} for d in r.json().get("drives", [])]

    def folders(self, drive_id: str, parent_id: str = "") -> list[dict]:
        parent = parent_id or drive_id
        q = f"'{_q(parent)}' in parents and mimeType='{FOLDER_MIME}' and trashed=false"
        out, token = [], None
        while len(out) < 1000:
            params = {"q": q, "fields": "nextPageToken,files(id,name)", "pageSize": "200", "corpora": "drive",
                      "driveId": drive_id, "includeItemsFromAllDrives": "true", "orderBy": "name"}
            if token:
                params["pageToken"] = token
            j = self._req("GET", "/files", params=params).json()
            out += [{"id": f["id"], "name": f["name"]} for f in j.get("files", [])]
            token = j.get("nextPageToken")
            if not token:
                break
        return out

    def create_folder(self, drive_id: str, parent_id: str, name: str) -> dict:
        name = clean_name(name, "folder")
        parent = parent_id or drive_id
        q = f"'{_q(parent)}' in parents and name='{_q(name)}' and mimeType='{FOLDER_MIME}' and trashed=false"
        j = self._req("GET", "/files", params={"q": q, "fields": "files(id,name)", "corpora": "drive", "driveId": drive_id,
                                                "includeItemsFromAllDrives": "true"}).json()
        if j.get("files"):
            f = j["files"][0]
            return {"id": f["id"], "name": f["name"]}
        f = self._req("POST", "/files", params={"fields": "id,name"},
                      json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).json()
        return {"id": f["id"], "name": f["name"]}

    def _dest(self) -> tuple[str, str]:
        d, f = self.dest.get("drive_id"), self.dest.get("folder_id")
        if not d or not f:
            raise StorageError("No Google Drive destination folder has been selected")
        return d, f

    def check(self) -> str:
        d, f = self._dest()
        j = self._req("GET", f"/files/{quote(f)}", params={"fields": "id,name,mimeType,driveId,capabilities(canAddChildren)"}).json()
        if f != d and j.get("mimeType") != FOLDER_MIME:
            raise StorageError("The selected Google Drive destination is not a folder")
        if not j.get("driveId"):
            raise StorageError("The folder is in a My Drive. Service accounts cannot store files there; "
                               "choose a folder in a Shared Drive")
        if not (j.get("capabilities") or {}).get("canAddChildren", True):
            raise StorageError("The service account cannot add files to this folder; make it a Content manager of the Shared Drive")
        probe = self._probe()
        return f"Connected to {self.dest.get('drive_name') or 'Shared Drive'} / {self.dest.get('folder_path') or j.get('name')}. {probe}"

    def _probe(self) -> str:
        import tempfile

        _, f = self._dest()
        with tempfile.NamedTemporaryFile(prefix="tp-probe-", suffix=".txt") as t:
            t.write(b"TestAgent storage check. This file is deleted automatically.\n")
            t.flush()
            p = Path(t.name)
            up = self.upload(f, p, ".testpilot-check.txt", "text/plain", sha256_file(p))
        try:
            self.delete(up.ref)
            return "Test file written, verified and removed."
        except StorageError as e:
            return f"Test file written and verified, but could not be removed ({e})."

    def ensure_folder(self, parts: list[str]) -> str:
        d, cur = self._dest()
        for i, part in enumerate(parts):
            key = tuple(parts[: i + 1])
            if key in self._folders:
                cur = self._folders[key]
                continue
            cur = self.create_folder(d, cur, part)["id"]
            self._folders[key] = cur
        return cur

    def upload(self, folder_id: str, path: Path, name: str, mime: str, sha256: str) -> Uploaded:
        name, size = clean_name(name, "file"), path.stat().st_size
        fields = "id,name,size,md5Checksum,sha256Checksum,webViewLink"
        meta = {"name": name, "parents": [folder_id]}
        mime = mime or "application/octet-stream"
        r = self._req("POST", f"{DRIVE_UPLOAD}/files", params={"uploadType": "resumable", "fields": fields},
                      json=meta, headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        loc = r.headers.get("location")
        if not loc:
            raise StorageError("Google Drive did not start the upload")
        item = None
        with path.open("rb") as f:
            start = 0
            while True:
                data = f.read(GCHUNK)
                end = start + len(data) - 1
                rng = f"bytes {start}-{end}/{size}" if data else f"bytes */{size}"
                try:
                    pr = self.c.put(loc, content=data, headers={"Authorization": f"Bearer {self.token()}",
                                                                "Content-Range": rng, "Content-Type": mime})
                except httpx.HTTPError as e:
                    raise StorageError(f"Google Drive upload interrupted: {e}", transient=True)
                if pr.status_code in (200, 201):
                    item = pr.json()
                    break
                if pr.status_code != 308:
                    raise _drive_error(pr)
                start = end + 1
                if not data:
                    raise StorageError("Google Drive upload did not complete")
        if int(item.get("size", -1)) != size:
            raise StorageError(f"Upload verification failed: Google Drive reports {item.get('size')} bytes, expected {size}")
        if item.get("sha256Checksum"):
            ok = item["sha256Checksum"] == sha256
        elif item.get("md5Checksum"):
            h = hashlib.md5(usedforsecurity=False)
            with path.open("rb") as fh:
                for c in iter(lambda: fh.read(1 << 20), b""):
                    h.update(c)
            ok = item["md5Checksum"] == h.hexdigest()
        else:
            ok = hashlib.sha256(self.download({"file_id": item["id"]}, size + 1)).hexdigest() == sha256
        if not ok:
            raise StorageError("Upload verification failed: stored file differs from the original")
        return Uploaded(ref={"file_id": item["id"], "drive_id": self.dest.get("drive_id")}, path="",
                        web_url=item.get("webViewLink", ""))

    def download(self, ref: dict[str, Any], max_bytes: int) -> bytes:
        r = self._req("GET", f"/files/{quote(ref['file_id'])}", params={"alt": "media"})
        if len(r.content) > max_bytes:
            raise StorageError("File is larger than the download limit")
        return r.content

    def delete(self, ref: dict[str, Any]) -> None:
        # Shared Drive content managers may trash but not permanently delete: trash (recoverable for 30 days).
        self._req("PATCH", f"/files/{quote(ref['file_id'])}", ok=(200, 404), params={"fields": "id"},
                  json={"trashed": True})


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _drive_error(r: httpx.Response) -> StorageError:
    j = _json(r)
    e = j.get("error")
    msg = e.get("message") if isinstance(e, dict) else (j.get("error_description") or e)
    hint = ""
    if r.status_code in (403, 404):
        hint = " — add the service account's e-mail as a Content manager of the Shared Drive"
    return StorageError(f"Google Drive error {r.status_code}: {str(msg or r.reason_phrase)[:300]}{hint}",
                        transient=r.status_code == 429 or r.status_code >= 500)


def _json(r: httpx.Response) -> dict:
    try:
        j = r.json()
        return j if isinstance(j, dict) else {}
    except ValueError:
        return {}


# Registry used by storage_policy.provider_for(); tests may register fakes.
FACTORIES: dict[str, Callable[..., Provider]] = {"sharepoint": SharePoint, "gdrive": GoogleDrive}
