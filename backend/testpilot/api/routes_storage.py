"""Storage & data residency.

Organization (tenant.storage — owners and administrators), under /admin/storage:
  GET    ''                         current choice, policy, health, pending setup
  POST   /unical                    choose Unical-managed storage (explicit agreement required)
  PUT    /pending                   start/replace a SharePoint or Google Drive setup (credentials)
  POST   /pending/browse            list sites / libraries / shared drives / folders with those credentials
  POST   /pending/folder            create a folder at the destination
  PUT    /pending/destination       pick the destination folder
  POST   /pending/test              live test of the setup (probe file written, verified, deleted)
  POST   /activate                  switch the organization to the tested setup
  DELETE /pending                   abandon the setup
  PUT    /settings                  folder names, retention, deletion permission, AI switch
  POST   /test                      live test of the active external store
  POST   /delete-local              erase everything this organization has on Unical servers
Unical Super Admin, under /platform/storage: providers, default policy, per-organization policy,
limits, health and the storage audit trail.
"""
from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import artifacts, report_store, settings_store, storage, storage_policy
from ..db import get_db
from ..models import STORAGE_POLICIES, AuditLog, CaseVersion, Evidence, Run, Tenant, TenantStorage, User, utcnow
from ..storage_providers import StorageError, clean_name
from .deps import audit, current_tenant, require_perm, require_platform_admin

router = APIRouter(prefix="/admin/storage", tags=["storage"])
platform = APIRouter(prefix="/platform/storage", tags=["storage"])
perm = require_perm("tenant.storage")


class AgreeIn(BaseModel):
    agree: bool = False


class PendingIn(BaseModel):
    provider: str
    source: str = "own"                     # SharePoint: own | sso (reuse the Identity & SSO app registration)
    entra_tenant_id: str = Field(default="", max_length=64)
    client_id: str = Field(default="", max_length=64)
    client_secret: str = Field(default="", max_length=512)
    service_account_json: str = Field(default="", max_length=20000)


class BrowseIn(BaseModel):
    what: str                                # sites | libraries | drives | folders
    q: str = Field(default="", max_length=500)
    site_id: str = Field(default="", max_length=300)
    drive_id: str = Field(default="", max_length=300)
    parent_id: str = Field(default="", max_length=300)


class FolderIn(BaseModel):
    drive_id: str = Field(max_length=300)
    parent_id: str = Field(default="", max_length=300)
    name: str = Field(min_length=1, max_length=100)


class DestinationIn(BaseModel):
    site_id: str = Field(default="", max_length=300)
    site_name: str = Field(default="", max_length=200)
    site_url: str = Field(default="", max_length=500)
    drive_id: str = Field(max_length=300)
    drive_name: str = Field(default="", max_length=200)
    folder_id: str = Field(max_length=300)
    folder_path: str = Field(default="", max_length=1000)


class SettingsIn(BaseModel):
    folders: dict[str, str] = {}
    allow_delete: bool = False
    retention_days: int = Field(default=0, ge=0, le=3650)
    ai_enabled: bool = True


class ConfirmIn(BaseModel):
    confirm: str = ""


def _out(db: Session, r: TenantStorage) -> dict:
    v = storage_policy.view(db, r)
    v["pending_uploads"] = db.scalar(select(func.count()).select_from(Evidence).where(
        Evidence.tenant_id == r.tenant_id, Evidence.upload_status == "pending")) or 0
    v["lost_uploads"] = db.scalar(select(func.count()).select_from(Evidence).where(
        Evidence.tenant_id == r.tenant_id, Evidence.upload_status == "lost")) or 0
    v["pending_case_versions"] = db.scalar(select(func.count()).select_from(CaseVersion).where(
        CaseVersion.tenant_id == r.tenant_id, CaseVersion.upload_status == "pending")) or 0
    v["local_artifacts"] = db.scalar(select(func.count()).select_from(Evidence).where(
        Evidence.tenant_id == r.tenant_id, Evidence.storage == "local", Evidence.deleted_at.is_(None))) or 0
    return v


def _row(db: Session, me: User) -> TenantStorage:
    return storage_policy.row(db, me.tenant_id)


def _pending(r: TenantStorage) -> dict[str, Any]:
    p = (r.config or {}).get("pending")
    if not p:
        raise HTTPException(409, "Start the SharePoint or Google Drive setup first")
    return p


def _save_pending(r: TenantStorage, p: dict | None) -> None:
    cfg = dict(r.config or {})
    if p is None:
        cfg.pop("pending", None)
    else:
        cfg["pending"] = p
    r.config = cfg  # reassign so the JSON change is persisted


def _storage_error(e: StorageError) -> HTTPException:
    return HTTPException(502 if e.transient else 422, str(e))


@router.get("")
def get_storage(db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    db.commit()
    return _out(db, r)


@router.post("/unical")
def choose_unical(body: AgreeIn, request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    if ("unical", "local") not in storage_policy.allowed(db, r):
        raise HTTPException(403, "Unical-managed storage is not permitted for this organization")
    if not body.agree:
        raise HTTPException(422, "Agree to store test artifacts on Unical-managed infrastructure first")
    was = (r.mode, r.provider)
    r.mode, r.provider = "unical", "local"
    r.unical_consent_at, r.unical_consent_by, r.unical_consent_source = utcnow(), me.id, "admin"
    r.credentials_enc, r.credentials_hint = "", ""
    cfg = dict(r.config or {})
    cfg.pop("destination", None)
    r.config = cfg
    r.status, r.last_error, r.needs_reselection, r.reselection_reason, r.updated_by = "ok", "", False, "", me.id
    audit(db, request, me, "storage_unical_selected", "Unical-managed storage", previous=list(was))
    db.commit()
    return _out(db, r)


@router.put("/pending")
def set_pending(body: PendingIn, request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    if ("external", body.provider) not in storage_policy.allowed(db, r):
        raise HTTPException(403, f"{storage_policy.PROVIDER_LABELS.get(body.provider, body.provider)} is not permitted for this organization")
    try:
        enc, hint = storage_policy.seal_credentials(body.provider, body.model_dump())
    except StorageError as e:
        raise HTTPException(422, str(e))
    _save_pending(r, {"provider": body.provider, "credentials_enc": enc, "credentials_hint": hint, "destination": {},
                      "started_by": me.id, "started_at": utcnow().isoformat()})
    try:  # authenticate now, so a wrong secret is reported immediately
        prov = storage_policy.pending_provider(db, r)
        prov.token()
    except StorageError as e:
        audit(db, request, me, "storage_setup_credentials", body.provider, ok=False)
        db.commit()
        raise _storage_error(e)
    audit(db, request, me, "storage_setup_credentials", body.provider, ok=True)
    db.commit()
    return _out(db, r)


@router.post("/pending/browse")
def browse(body: BrowseIn, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    p = _pending(r)
    try:
        prov = storage_policy.pending_provider(db, r)
        if body.what == "sites" and p["provider"] == "sharepoint":
            return {"items": prov.search_sites(body.q)}
        if body.what == "libraries" and p["provider"] == "sharepoint":
            return {"items": prov.libraries(body.site_id)}
        if body.what == "drives" and p["provider"] == "gdrive":
            return {"items": prov.shared_drives()}
        if body.what == "folders":
            parent = body.parent_id or ("root" if p["provider"] == "sharepoint" else "")
            return {"items": prov.folders(body.drive_id, parent)}
    except StorageError as e:
        raise _storage_error(e)
    raise HTTPException(422, "Unknown browse request")


@router.post("/pending/folder", status_code=201)
def create_folder(body: FolderIn, request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    p = _pending(r)
    try:
        prov = storage_policy.pending_provider(db, r)
        parent = body.parent_id or ("root" if p["provider"] == "sharepoint" else "")
        f = prov.create_folder(body.drive_id, parent, body.name)
    except StorageError as e:
        raise _storage_error(e)
    audit(db, request, me, "storage_folder_created", f["name"], provider=p["provider"])
    db.commit()
    return f


@router.put("/pending/destination")
def set_destination(body: DestinationIn, request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    p = dict(_pending(r))
    if p["provider"] == "sharepoint" and not body.site_id:
        raise HTTPException(422, "Select a SharePoint site")
    p["destination"] = body.model_dump()
    _save_pending(r, p)
    audit(db, request, me, "storage_destination_selected", body.folder_path or body.folder_id, provider=p["provider"])
    db.commit()
    return _out(db, r)


@router.post("/pending/test")
def test_pending(request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    p = dict(_pending(r))
    try:
        msg = storage_policy.pending_provider(db, r).check()
        p.update(tested_ok=True, tested_at=utcnow().isoformat(), test_message=msg)
    except StorageError as e:
        p.update(tested_ok=False, tested_at=utcnow().isoformat(), test_message=str(e))
    _save_pending(r, p)
    audit(db, request, me, "storage_setup_tested", p["provider"], ok=p["tested_ok"])
    db.commit()
    return {**_out(db, r), "ok": p["tested_ok"], "message": p["test_message"]}


@router.post("/activate")
def activate(request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    p = dict(_pending(r))
    if ("external", p["provider"]) not in storage_policy.allowed(db, r):
        raise HTTPException(403, "This storage provider is not permitted for this organization")
    if not (p.get("destination") or {}).get("folder_id"):
        raise HTTPException(422, "Select the destination folder first")
    try:  # always re-tested at the moment of switching
        msg = storage_policy.pending_provider(db, r).check()
    except StorageError as e:
        raise _storage_error(e)
    was = (r.mode, r.provider)
    cfg = dict(r.config or {})
    cfg["destination"] = p["destination"]
    cfg.pop("pending", None)
    r.config = cfg
    r.mode, r.provider = "external", p["provider"]
    r.credentials_enc, r.credentials_hint = p["credentials_enc"], p.get("credentials_hint", "")
    r.status, r.last_ok_at, r.last_check_at, r.last_error = "ok", utcnow(), utcnow(), ""
    r.needs_reselection, r.reselection_reason, r.updated_by = False, "", me.id
    audit(db, request, me, "storage_external_activated", storage_policy.PROVIDER_LABELS[r.provider],
          previous=list(was), folder=p["destination"].get("folder_path", ""))
    db.commit()
    return {**_out(db, r), "message": msg}


@router.delete("/pending")
def cancel_pending(request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    _save_pending(r, None)
    audit(db, request, me, "storage_setup_cancelled", "")
    db.commit()
    return _out(db, r)


@router.put("/settings")
def put_settings(body: SettingsIn, request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    f = {}
    for k, v in body.folders.items():
        if k not in storage_policy.FOLDER_DEFAULTS:
            continue
        v = (v or "").strip()
        if k == "project_folder":
            if v not in ("name_id", "name", "id"):
                raise HTTPException(422, "Project folder naming must be name_id, name or id")
        elif k == "run_prefix":
            if len(v) > 20 or clean_name(v + "1", "") != v + "1":
                raise HTTPException(422, "Execution folder prefix: up to 20 characters, no special characters")
        elif v and clean_name(v, "") != v:
            raise HTTPException(422, f"Folder name '{v}' contains characters SharePoint/Drive do not allow")
        if v:
            f[k] = v
    cfg = dict(r.config or {})
    cfg["folders"] = f
    r.config = cfg
    r.allow_delete, r.retention_days, r.ai_enabled, r.updated_by = body.allow_delete, body.retention_days, body.ai_enabled, me.id
    audit(db, request, me, "storage_settings_updated", "", retention_days=body.retention_days,
          allow_delete=body.allow_delete, ai_enabled=body.ai_enabled, folders=f)
    db.commit()
    return _out(db, r)


@router.post("/test")
def test_active(request: Request, db: Session = Depends(get_db), me: User = Depends(perm)):
    r = _row(db, me)
    if r.mode != "external":
        raise HTTPException(409, "This organization uses Unical-managed storage")
    ok, msg = storage_policy.check(db, r)
    audit(db, request, me, "storage_tested", storage_policy.PROVIDER_LABELS[r.provider], ok=ok)
    db.commit()
    return {**_out(db, r), "ok": ok, "message": msg}


@router.post("/delete-local")
def delete_local(body: ConfirmIn, request: Request, db: Session = Depends(get_db), me: User = Depends(perm),
                 tenant: Tenant = Depends(current_tenant)):
    if body.confirm != tenant.slug:
        raise HTTPException(422, f"Type the organization address ({tenant.slug}) to confirm")
    busy = db.scalar(select(func.count()).select_from(Run).where(Run.tenant_id == me.tenant_id,
                                                                 Run.status.in_(("queued", "running")))) or 0
    if busy:
        raise HTTPException(409, f"{busy} run(s) are queued or running. Wait for them to finish (or cancel them) first.")
    try:
        n = artifacts.delete_local_evidence(db, me.tenant_id)
    except report_store.ReportStoreError as e:
        db.rollback()
        raise HTTPException(502, f"Could not remove the report copies from MySQL ({e}); nothing was deleted. Try again later.")
    audit(db, request, me, "storage_local_data_deleted", f"{n} artifact(s)")
    db.commit()
    return {**_out(db, _row(db, me)), "deleted": n, "message": f"Deleted {n} file(s) from Unical-managed storage."}


# ================================================================== Unical Super Admin
class PlatformIn(BaseModel):
    local_enabled: bool
    sharepoint_enabled: bool
    gdrive_enabled: bool
    default_policy: str
    default_retention_days: int = Field(ge=0, le=3650)
    max_artifact_mb: int = Field(ge=1, le=2048)
    spool_max_hours: int = Field(ge=1, le=720)
    base_folder: str = Field(min_length=1, max_length=100)
    acknowledge: list[int] = []


class TenantPolicyIn(BaseModel):
    policy_override: str | None = None
    acknowledge: list[int] = []


def _platform_out(db: Session) -> dict:
    plat = storage_policy.platform(db)
    rows = []
    for t in db.scalars(select(Tenant).order_by(Tenant.name)).all():
        r = storage_policy.row(db, t.id)
        pol = storage_policy.policy_of(db, r, plat)
        rows.append({"tenant_id": t.id, "slug": t.slug, "name": t.name, "status": t.status,
                     "policy_override": r.policy_override, "policy": pol, "policy_label": storage_policy.POLICY_LABELS[pol],
                     "mode": r.mode, "provider": r.provider,
                     "provider_label": storage_policy.PROVIDER_LABELS.get(r.provider, "not chosen") if r.mode else "not chosen",
                     "storage_status": r.status, "last_ok_at": r.last_ok_at, "last_error": r.last_error,
                     "needs_reselection": r.needs_reselection,
                     "pending_uploads": db.scalar(select(func.count()).select_from(Evidence).where(
                         Evidence.tenant_id == t.id, Evidence.upload_status == "pending")) or 0})
    root = storage.root()
    du = shutil.disk_usage(root)
    events = db.scalars(select(AuditLog).where(AuditLog.action.like("%storage%")).order_by(AuditLog.id.desc()).limit(50)).all()
    users = {u.id: u.username for u in db.scalars(select(User).where(User.id.in_({e.user_id for e in events if e.user_id}))).all()}
    tenants = {t["tenant_id"]: t["slug"] for t in rows}
    db.commit()
    return {"settings": plat, "policies": [{"value": p, "label": storage_policy.POLICY_LABELS[p]} for p in STORAGE_POLICIES],
            "tenants": rows,
            "local": {"total_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
                      "used_pct": round(100 * (du.total - du.free) / du.total, 1) if du.total else None},
            "audit": [{"id": e.id, "at": e.created_at, "user": users.get(e.user_id), "organization": tenants.get(e.tenant_id),
                       "action": e.action, "target": e.target} for e in events]}


@platform.get("")
def platform_get(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    return _platform_out(db)


@platform.put("")
def platform_put(body: PlatformIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    if body.default_policy not in STORAGE_POLICIES:
        raise HTTPException(422, "Unknown policy")
    if not (body.local_enabled or body.sharepoint_enabled or body.gdrive_enabled):
        raise HTTPException(422, "Enable at least one storage provider")
    base = clean_name(body.base_folder, "")
    if base != body.base_folder.strip():
        raise HTTPException(422, "The base folder name contains characters SharePoint/Drive do not allow")
    new = {**storage_policy.platform(db), **body.model_dump(exclude={"acknowledge"}), "base_folder": base}
    in_use = {body.default_policy} | {p for p in db.scalars(select(TenantStorage.policy_override)).all() if p}
    bad = storage_policy.unusable_policies(new, in_use)
    if bad:
        raise HTTPException(422, "With these providers, organizations under '" + "', '".join(bad) +
                            "' would have no storage to choose. Enable a provider or change those policies first.")
    hit = storage_policy.affected(db, new)
    if {a["tenant_id"] for a in hit} - set(body.acknowledge):
        # Nothing is saved: the Super Admin must explicitly acknowledge every affected organization.
        return {**_platform_out(db), "needs_acknowledgement": True, "affected": hit}
    settings_store.put(db, "storage", body.model_dump(exclude={"acknowledge"}) | {"base_folder": base}, me.id)
    storage_policy.flag_reselection(db, [a["tenant_id"] for a in hit],
                                    "Unical changed the platform storage policy on " + utcnow().strftime("%d %b %Y"))
    audit(db, request, me, "platform_storage_settings", "storage", tenant_id=None, scope="platform",
          settings=body.model_dump(exclude={"acknowledge"}), affected=[a["slug"] for a in hit])
    for a in hit:
        audit(db, request, me, "storage_reselection_required", a["name"], tenant_id=a["tenant_id"])
    db.commit()
    return {**_platform_out(db), "message": "Saved" + (f"; {len(hit)} organization(s) must choose their storage again" if hit else "")}


@platform.put("/tenants/{tid}")
def platform_tenant_policy(tid: int, body: TenantPolicyIn, request: Request, db: Session = Depends(get_db),
                           me: User = Depends(require_platform_admin)):
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "Organization not found")
    if body.policy_override is not None and body.policy_override not in STORAGE_POLICIES:
        raise HTTPException(422, "Unknown policy")
    plat = storage_policy.platform(db)
    if storage_policy.unusable_policies(plat, {body.policy_override or plat["default_policy"]}):
        raise HTTPException(422, "No enabled storage provider satisfies this policy")
    hit = [a for a in storage_policy.affected(db, plat, {tid: body.policy_override}) if a["tenant_id"] == tid]
    if hit and tid not in body.acknowledge:
        return {**_platform_out(db), "needs_acknowledgement": True, "affected": hit}
    r = storage_policy.row(db, tid)
    r.policy_override = body.policy_override
    if hit:
        storage_policy.flag_reselection(db, [tid], "Unical changed this organization's storage policy on "
                                        + utcnow().strftime("%d %b %Y"))
    audit(db, request, me, "platform_storage_tenant_policy", t.name, tenant_id=t.id, scope="platform",
          policy=body.policy_override or "default")
    if hit:
        audit(db, request, me, "storage_reselection_required", t.name, tenant_id=t.id)
    db.commit()
    return {**_platform_out(db), "message": f"Policy for {t.name} saved"}


@platform.post("/test")
def platform_test(request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    """Unical-managed storage health: write, read back and delete a probe file on the evidence volume."""
    import hashlib
    import os

    plat = dict(storage_policy.platform(db))
    plat["local_last_check_at"] = utcnow().isoformat()
    try:
        p = storage.root() / f".probe-{os.getpid()}"
        data = os.urandom(4096)
        p.write_bytes(data)
        ok = hashlib.sha256(p.read_bytes()).digest() == hashlib.sha256(data).digest()
        p.unlink()
        if not ok:
            raise OSError("read-back mismatch")
        plat["local_last_ok_at"], plat["local_last_error"] = plat["local_last_check_at"], ""
        msg, good = "Unical-managed storage is writable and verified.", True
    except OSError as e:
        plat["local_last_error"] = str(e)[:500]
        msg, good = f"Unical-managed storage check failed: {e}", False
    settings_store.put(db, "storage", plat, me.id)
    audit(db, request, me, "platform_storage_tested", "local", tenant_id=None, scope="platform", ok=good)
    db.commit()
    return {**_platform_out(db), "ok": good, "message": msg}
