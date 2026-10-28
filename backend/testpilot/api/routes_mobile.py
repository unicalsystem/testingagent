"""Mobile app testing endpoints (release 1.8): device providers, app builds, mobile cases, cross-device runs,
live device sessions (recorder and AI agent) and reports."""
from __future__ import annotations

import hashlib
import os
import re
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import access, mobilelive as live, mobileproviders as mp, mobilereport, mobilerun, storage
from ..db import get_db
from ..mobiledrivers import element_at
from ..mobilesteps import ACTIONS, describe, json_schema, validate_steps
from ..models import (MobileApp, MobileCase, MobileProvider, MobileRun, MobileRunGroup, MobileSession, Project,
                      TestCase, User, utcnow)
from ..security import SecretsUnavailable
from ..urlguard import TargetRejected, parse_target
from .deps import audit, member

router = APIRouter(tags=["mobile"])
public = APIRouter(tags=["mobile"])
MAX_APP_MB = int(os.environ.get("MOBILE_MAX_APP_MB", "400") or 400)
APP_EXT = {".apk": "android", ".aab": "android", ".ipa": "ios", ".zip": "ios"}
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,254}$")
ACTIVITY_RE = re.compile(r"^[A-Za-z0-9_.$]{0,255}$")
FILE_RE = re.compile(r"^(step-\d{3}\.(jpg|png)|video\.webm)$")
ACTIVE = ("starting", "ready")


# --------------------------------------------------------------------------- schemas
class ProviderIn(BaseModel):
    kind: str = Field(pattern="^(browserstack|lambdatest|saucelabs|appium|unical_lab|emulator)$")
    name: str = Field(min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)
    secret: str = Field(default="", max_length=500)  # empty keeps the stored key


class AppUrlIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field(pattern="^(android|ios)$")
    version: str = Field(default="", max_length=60)
    source_url: str = Field(default="", max_length=2048)
    app_id: str = Field(default="", max_length=255)
    app_activity: str = Field(default="", max_length=255)


class CaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    kind: str = Field(default="native", pattern="^(native|web)$")
    platform: str = Field(default="android", pattern="^(android|ios|any)$")
    app_id: int | None = None
    start_url: str = Field(default="", max_length=2048)
    steps: list = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source: str = Field(default="manual", pattern="^(manual|ai|recorded)$")
    test_case_id: int | None = None


class Device(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    os_version: str = Field(default="", max_length=30)
    platform: str = Field(pattern="^(android|ios)$")


class GroupIn(BaseModel):
    name: str = Field(default="", max_length=200)
    provider_id: int
    devices: list[Device] = Field(min_length=1, max_length=20)
    case_ids: list[int] = Field(min_length=1, max_length=100)


class SessionIn(BaseModel):
    purpose: str = Field(pattern="^(record|ai)$")
    provider_id: int
    device: Device
    kind: str = Field(default="native", pattern="^(native|web)$")
    app_id: int | None = None
    app_package: str = Field(default="", max_length=255)
    start_url: str = Field(default="", max_length=2048)
    goal: str = Field(default="", max_length=8000)
    test_case_id: int | None = None


class ActionIn(BaseModel):
    step: dict
    record: bool = True


class PointIn(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class StepsIn(BaseModel):
    steps: list


class SaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    case_id: int | None = None  # overwrite the steps of an existing mobile case


# --------------------------------------------------------------------------- helpers
def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    return access.require(db, me, db.get(Project, pid), perm)


def _owned(db: Session, model, oid: int, me: User, perm: str, label: str):
    o = db.get(model, oid)
    if not o or o.tenant_id != me.tenant_id or getattr(o, "archived", False):
        raise HTTPException(404, f"{label} not found")
    try:
        access.require(db, me, db.get(Project, o.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, f"{label} not found")
        raise
    return o


def provider_out(p: MobileProvider) -> dict:
    return {"id": p.id, "project_id": p.project_id, "kind": p.kind, "label": mp.LABEL.get(p.kind, p.kind), "name": p.name,
            "config": p.config or {}, "secret_hint": p.secret_hint, "has_secret": bool(p.secret_enc), "status": p.status,
            "last_error": p.last_error, "native": p.kind != "emulator", "created_at": p.created_at, "updated_at": p.updated_at}


def app_out(a: MobileApp) -> dict:
    return {"id": a.id, "name": a.name, "platform": a.platform, "version": a.version, "filename": a.filename,
            "has_file": bool(a.rel_path), "source_url": a.source_url, "size": a.size, "sha256": a.sha256, "app_id": a.app_id,
            "app_activity": a.app_activity, "created_at": a.created_at}


def case_out(c: MobileCase, full: bool = True) -> dict:
    d = {"id": c.id, "project_id": c.project_id, "name": c.name, "description": c.description, "kind": c.kind,
         "platform": c.platform, "app_id": c.app_id, "start_url": c.start_url, "step_count": len(c.steps or []),
         "source": c.source, "test_case_id": c.test_case_id, "tags": c.tags or [], "created_at": c.created_at,
         "updated_at": c.updated_at}
    if full:
        d["steps"] = c.steps or []
    return d


def run_out(r: MobileRun, full: bool = False) -> dict:
    res = r.step_results or []
    d = {"id": r.id, "group_id": r.group_id, "case_id": r.case_id, "case_name": (r.case_snapshot or {}).get("name", ""),
         "device": r.device or {}, "status": r.status, "error": r.error, "session_url": r.session_url,
         "passed_steps": sum(1 for x in res if x.get("status") == "passed"), "total_steps": len((r.case_snapshot or {}).get("steps") or []),
         "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at}
    if full:
        d["steps"] = [{**x, "shot": os.path.basename(x["shot"]) if x.get("shot") else ""} for x in res]
        d["case"] = {k: (r.case_snapshot or {}).get(k) for k in ("name", "kind", "platform", "start_url")}
        d["has_video"] = (mobilerun.run_dir(r) / "video.webm").exists()
    return d


def group_out(db: Session, g: MobileRunGroup, with_runs: bool = False) -> dict:
    runs = db.scalars(select(MobileRun).where(MobileRun.group_id == g.id).order_by(MobileRun.id)).all()
    counts: dict[str, int] = {}
    for r in runs:
        counts[r.status] = counts.get(r.status, 0) + 1
    prov = db.get(MobileProvider, g.provider_id) if g.provider_id else None
    d = {"id": g.id, "project_id": g.project_id, "name": g.name, "provider": prov.name if prov else "(removed)",
         "provider_kind": prov.kind if prov else "", "devices": g.devices or [], "counts": counts, "total": len(runs),
         "status": _group_status(counts, len(runs)), "created_at": g.created_at}
    if with_runs:
        d["runs"] = [run_out(r) for r in runs]
    return d


def _group_status(counts: dict, total: int) -> str:
    if counts.get("queued", 0) + counts.get("running", 0):
        return "running" if counts.get("running") or total > counts.get("queued", 0) else "queued"
    if counts.get("error"):
        return "error"
    if counts.get("failed"):
        return "failed"
    if counts.get("cancelled") == total:
        return "cancelled"
    return "passed"


def _check_url(url: str, label: str) -> str:
    url = url.strip()
    if not url:
        return ""
    try:
        parse_target(url)
    except TargetRejected as e:
        raise HTTPException(422, f"{label}: {e}")
    return url


def _validate_case(db: Session, body: CaseIn, me: User, pid: int) -> list[dict]:
    try:
        steps = validate_steps(body.steps)
    except Exception as e:
        raise HTTPException(422, f"Steps: {str(e)[:1500]}")
    if body.kind == "native" and body.platform == "any":
        raise HTTPException(422, "Choose Android or iOS for an app test")
    if body.app_id is not None:
        a = db.get(MobileApp, body.app_id)
        if not a or a.tenant_id != me.tenant_id or a.project_id != pid or a.archived:
            raise HTTPException(422, "Unknown app build")
        if body.kind == "native" and a.platform != body.platform:
            raise HTTPException(422, f"The build is for {a.platform}")
    if body.kind == "web":
        _check_url(body.start_url, "Start URL")
    if body.test_case_id is not None:
        tc = db.get(TestCase, body.test_case_id)
        if not tc or tc.tenant_id != me.tenant_id or tc.project_id != pid:
            raise HTTPException(422, "Unknown manual test case")
    return steps


def _apply_case(c: MobileCase, body: CaseIn, steps: list[dict]) -> None:
    c.name, c.description, c.kind = body.name.strip(), body.description, body.kind
    c.platform = "any" if body.kind == "web" else body.platform
    c.app_id = body.app_id if body.kind == "native" else None
    c.start_url = body.start_url.strip() if body.kind == "web" else ""
    c.steps, c.tags, c.test_case_id = steps, [t.strip()[:40] for t in body.tags if t.strip()], body.test_case_id


def _provider_for(db: Session, pid_: int, me: User, project_id: int) -> MobileProvider:
    p = db.get(MobileProvider, pid_)
    if not p or p.tenant_id != me.tenant_id or p.project_id != project_id:
        raise HTTPException(422, "Unknown device provider")
    if p.kind == "unical_lab" and not mp.lab_available():
        raise HTTPException(422, "The Unical device lab is not configured on this server yet")
    if p.kind not in ("emulator", "unical_lab", "appium") and not p.secret_enc:
        raise HTTPException(422, f"Add the {mp.LABEL[p.kind]} access key first")
    return p


# --------------------------------------------------------------------------- overview & providers
@router.get("/projects/{pid}/mobile")
def overview(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    count = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(m.project_id == pid, m.tenant_id == me.tenant_id, *w)) or 0
    return {"providers": count(MobileProvider), "apps": count(MobileApp, MobileApp.archived.is_(False)),
            "cases": count(MobileCase, MobileCase.archived.is_(False)), "run_groups": count(MobileRunGroup),
            "kinds": [{"kind": k, "label": mp.LABEL[k], "available": k != "unical_lab" or mp.lab_available()} for k in mp.KINDS],
            "sauce_regions": list(mp.SAUCE_REGIONS), "emulated_devices": mp.Emulator(None).devices(),
            "actions": ACTIONS, "ai_available": live.ai_configured(), "max_app_mb": MAX_APP_MB,
            "my_permissions": sorted(access.perms_for(db, me, pid))}


@router.get("/mobile/step-schema")
def step_schema(me: User = Depends(member)):
    return {"actions": ACTIONS, "schema": json_schema()}


@router.get("/projects/{pid}/mobile/providers")
def list_providers(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    return [provider_out(p) for p in db.scalars(select(MobileProvider).where(
        MobileProvider.project_id == pid, MobileProvider.tenant_id == me.tenant_id).order_by(MobileProvider.id)).all()]


@router.post("/projects/{pid}/mobile/providers", status_code=201)
def create_provider(pid: int, body: ProviderIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me, "secret.write")
    n = db.scalar(select(func.count()).select_from(MobileProvider).where(MobileProvider.project_id == pid)) or 0
    if n >= 20:
        raise HTTPException(422, "At most 20 device providers per project")
    p = MobileProvider(tenant_id=me.tenant_id, project_id=pid, kind=body.kind, name=body.name.strip(), created_by=me.id)
    try:
        p.config = mp.clean_config(body.kind, body.config)
        mp.set_secret(p, body.secret, keep=False)
    except mp.ProviderError as e:
        raise HTTPException(422, str(e))
    except SecretsUnavailable as e:
        raise HTTPException(503, str(e))
    p.status = "ok" if body.kind == "emulator" else "unverified"
    db.add(p)
    db.flush()
    audit(db, request, me, "mobile_provider_created", f"mobile provider {p.id}", kind=p.kind, project_id=pid)
    db.commit()
    return provider_out(p)


@router.put("/mobile/providers/{prid}")
def update_provider(prid: int, body: ProviderIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _owned(db, MobileProvider, prid, me, "secret.write", "Device provider")
    if body.kind != p.kind:
        raise HTTPException(422, "The provider type cannot be changed; add a new provider instead")
    try:
        p.config = mp.clean_config(p.kind, body.config)
        mp.set_secret(p, body.secret, keep=True)
    except mp.ProviderError as e:
        raise HTTPException(422, str(e))
    except SecretsUnavailable as e:
        raise HTTPException(503, str(e))
    p.name = body.name.strip()
    if p.kind != "emulator":
        p.status, p.last_error = "unverified", ""
    audit(db, request, me, "mobile_provider_updated", f"mobile provider {p.id}", secret_changed=bool(body.secret))
    db.commit()
    return provider_out(p)


@router.delete("/mobile/providers/{prid}", status_code=204)
def delete_provider(prid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _owned(db, MobileProvider, prid, me, "secret.write", "Device provider")
    busy = db.scalar(select(func.count()).select_from(MobileRun).where(MobileRun.provider_id == p.id,
                                                                       MobileRun.status.in_(("queued", "running")))) or 0
    if busy:
        raise HTTPException(409, "Runs on this provider are still queued or running")
    audit(db, request, me, "mobile_provider_deleted", f"mobile provider {p.id}", kind=p.kind)
    db.commit()
    db.delete(p)
    return Response(status_code=204)


@router.post("/mobile/providers/{prid}/test")
def test_provider(prid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _owned(db, MobileProvider, prid, me, "project.update", "Device provider")
    try:
        msg = mp.provider(p).test()
        p.status, p.last_error = "ok", ""
        out = {"ok": True, "message": msg}
    except mp.ProviderError as e:
        p.status, p.last_error = "error", str(e)[:1000]
        out = {"ok": False, "message": str(e)}
    except SecretsUnavailable as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        p.status, p.last_error = "error", f"{type(e).__name__}"
        out = {"ok": False, "message": f"Could not reach the provider ({type(e).__name__})"}
    db.commit()
    return out


@router.get("/mobile/providers/{prid}/devices")
def provider_devices(prid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _owned(db, MobileProvider, prid, me, "project.read", "Device provider")
    try:
        return {"devices": mp.provider(p).devices(), "free_text": p.kind in ("lambdatest", "saucelabs", "appium", "unical_lab")}
    except (mp.ProviderError, SecretsUnavailable) as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not load the device list ({type(e).__name__})")


# --------------------------------------------------------------------------- apps
@router.get("/projects/{pid}/mobile/apps")
def list_apps(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    return [app_out(a) for a in db.scalars(select(MobileApp).where(
        MobileApp.project_id == pid, MobileApp.tenant_id == me.tenant_id, MobileApp.archived.is_(False)).order_by(MobileApp.id.desc())).all()]


def _app_fields(name: str, platform: str, app_id: str, app_activity: str) -> None:
    if platform not in ("android", "ios"):
        raise HTTPException(422, "Platform must be android or ios")
    if app_id and not PKG_RE.match(app_id):
        raise HTTPException(422, "Invalid package name / bundle id")
    if app_activity and not ACTIVITY_RE.match(app_activity):
        raise HTTPException(422, "Invalid launch activity")


@router.post("/projects/{pid}/mobile/apps", status_code=201)
def upload_app(pid: int, request: Request, file: UploadFile = File(...), name: str = Form(""), platform: str = Form(""),
               version: str = Form(""), app_id: str = Form(""), app_activity: str = Form(""),
               db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me, "case.write")
    fname = os.path.basename(file.filename or "app").strip()[:200] or "app"
    ext = os.path.splitext(fname)[1].lower()
    if ext not in APP_EXT:
        raise HTTPException(422, "Upload an .apk, .aab, .ipa or a zipped .app")
    platform = platform or APP_EXT[ext]
    _app_fields(name, platform, app_id.strip(), app_activity.strip())
    if APP_EXT[ext] != platform:
        raise HTTPException(422, f"A {ext} file is a {APP_EXT[ext]} build")
    a = MobileApp(tenant_id=me.tenant_id, project_id=pid, name=(name or fname).strip()[:160], platform=platform,
                  version=version.strip()[:60], filename=re.sub(r"[^A-Za-z0-9._-]", "_", fname), app_id=app_id.strip(),
                  app_activity=app_activity.strip(), created_by=me.id)
    db.add(a)
    db.flush()
    d = mobilerun.app_dir(a)
    dest = d / a.filename
    h, size, limit = hashlib.sha256(), 0, MAX_APP_MB * 1024 * 1024
    try:
        with dest.open("wb") as out:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"The build is larger than {MAX_APP_MB} MB")
                h.update(chunk)
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    if size < 1000:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(422, "The file is empty or too small to be an app build")
    os.chmod(dest, 0o640)
    a.rel_path, a.size, a.sha256 = mobilerun.rel(dest), size, h.hexdigest()
    audit(db, request, me, "mobile_app_uploaded", f"mobile app {a.id}", size=size, platform=platform)
    db.commit()
    return app_out(a)


@router.post("/projects/{pid}/mobile/apps/url", status_code=201)
def add_app_url(pid: int, body: AppUrlIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me, "case.write")
    _app_fields(body.name, body.platform, body.app_id.strip(), body.app_activity.strip())
    url = body.source_url.strip()
    if url:
        if not url.startswith("https://"):
            raise HTTPException(422, "The download URL must use https")
        _check_url(url, "Download URL")
    elif not body.app_id.strip():
        raise HTTPException(422, "Give a download URL, or the package name / bundle id of an app already on the devices")
    a = MobileApp(tenant_id=me.tenant_id, project_id=pid, name=body.name.strip(), platform=body.platform,
                  version=body.version.strip(), source_url=url, app_id=body.app_id.strip(),
                  app_activity=body.app_activity.strip(), created_by=me.id)
    db.add(a)
    db.flush()
    audit(db, request, me, "mobile_app_added", f"mobile app {a.id}", platform=a.platform)
    db.commit()
    return app_out(a)


@router.delete("/mobile/apps/{aid}", status_code=204)
def delete_app(aid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    a = _owned(db, MobileApp, aid, me, "case.write", "App build")
    if a.rel_path:
        shutil.rmtree(storage.resolve_evidence(a.rel_path).parent, ignore_errors=True)
    a.archived, a.rel_path, a.provider_refs = True, "", {}
    audit(db, request, me, "mobile_app_deleted", f"mobile app {a.id}")
    db.commit()
    return Response(status_code=204)


@public.get("/public/mobile-apps/{aid}/{exp}/{sig}/{filename}")
def download_app(aid: int, exp: int, sig: str, filename: str, db: Session = Depends(get_db)):
    """Signed, expiring link given to Appium servers (own server / Unical lab) so they can install the build."""
    if not re.fullmatch(r"[0-9a-f]{40}", sig) or not mp.check_download(aid, exp, sig):
        raise HTTPException(404, "Not found")
    a = db.get(MobileApp, aid)
    if not a or a.archived or not a.rel_path:
        raise HTTPException(404, "Not found")
    p = storage.resolve_evidence(a.rel_path)
    if not p.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="application/octet-stream", filename=a.filename,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------------------------- cases
@router.get("/projects/{pid}/mobile/cases")
def list_cases(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    return [case_out(c, False) for c in db.scalars(select(MobileCase).where(
        MobileCase.project_id == pid, MobileCase.tenant_id == me.tenant_id, MobileCase.archived.is_(False)).order_by(MobileCase.id)).all()]


@router.post("/projects/{pid}/mobile/cases", status_code=201)
def create_case(pid: int, body: CaseIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me, "case.write")
    steps = _validate_case(db, body, me, pid)
    c = MobileCase(tenant_id=me.tenant_id, project_id=pid, source=body.source, created_by=me.id)
    _apply_case(c, body, steps)
    db.add(c)
    db.flush()
    audit(db, request, me, "mobile_case_created", f"mobile case {c.id}", source=c.source)
    db.commit()
    return case_out(c)


@router.get("/mobile/cases/{cid}")
def get_case(cid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    return case_out(_owned(db, MobileCase, cid, me, "project.read", "Mobile case"))


@router.put("/mobile/cases/{cid}")
def update_case(cid: int, body: CaseIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    c = _owned(db, MobileCase, cid, me, "case.write", "Mobile case")
    steps = _validate_case(db, body, me, c.project_id)
    _apply_case(c, body, steps)
    audit(db, request, me, "mobile_case_updated", f"mobile case {c.id}")
    db.commit()
    return case_out(c)


@router.delete("/mobile/cases/{cid}", status_code=204)
def delete_case(cid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    c = _owned(db, MobileCase, cid, me, "case.write", "Mobile case")
    c.archived = True
    audit(db, request, me, "mobile_case_deleted", f"mobile case {c.id}")
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- runs
def _compatible(c: MobileCase, kind: str, dev: Device) -> str | None:
    if kind == "emulator" and c.kind != "web":
        return "app tests need a real device provider"
    if c.kind == "native" and c.platform != dev.platform:
        return f"the case is for {c.platform}"
    if c.kind == "native" and not c.app_id:
        return "the case has no app build"
    return None


@router.post("/projects/{pid}/mobile/run-groups", status_code=201)
def create_group(pid: int, body: GroupIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    project = _project(db, pid, me, "run.execute")
    prov = _provider_for(db, body.provider_id, me, pid)
    cases = db.scalars(select(MobileCase).where(MobileCase.id.in_(body.case_ids), MobileCase.tenant_id == me.tenant_id,
                                                MobileCase.project_id == pid, MobileCase.archived.is_(False))).all()
    if len(cases) != len(set(body.case_ids)):
        raise HTTPException(422, "Unknown mobile case")
    pairs, skipped = [], []
    for c in sorted(cases, key=lambda c: body.case_ids.index(c.id)):
        if not c.steps:
            skipped.append({"case": c.name, "device": "", "reason": "the case has no steps"})
            continue
        for dv in body.devices:
            why = _compatible(c, prov.kind, dv)
            if why:
                skipped.append({"case": c.name, "device": dv.name, "reason": why})
            else:
                pairs.append((c, dv))
    if not pairs:
        raise HTTPException(422, "Nothing to run: " + "; ".join(f"{s['case']} on {s['device'] or 'any device'}: {s['reason']}" for s in skipped[:5]))
    if len(pairs) > 200:
        raise HTTPException(422, "At most 200 case × device combinations per run")
    from .. import billing
    from ..models import Tenant
    billing.check_quota(db, db.get(Tenant, me.tenant_id), "mobile_runs_per_month", len(pairs), request)
    if any(c.kind == "web" for c, _ in pairs):
        pol = mobilerun.targets.build(db, project)
        if pol.problems:
            raise HTTPException(422, "Target policy: " + "; ".join(pol.problems))
    devices = [d.model_dump() for d in body.devices]
    g = MobileRunGroup(tenant_id=me.tenant_id, project_id=pid, provider_id=prov.id, devices=devices, requested_by=me.id,
                       name=body.name.strip() or f"{len(cases)} case(s) on {len(body.devices)} device(s)")
    db.add(g)
    db.flush()
    for c, dv in pairs:
        db.add(MobileRun(tenant_id=me.tenant_id, project_id=pid, group_id=g.id, case_id=c.id, provider_id=prov.id,
                         device={**dv.model_dump(), "provider_kind": prov.kind},
                         case_snapshot={"name": c.name, "kind": c.kind, "platform": c.platform, "app_id": c.app_id,
                                        "start_url": c.start_url, "steps": c.steps},
                         requested_by=me.id))
    db.flush()
    audit(db, request, me, "mobile_run_started", f"mobile run group {g.id}", runs=len(pairs), provider=prov.kind)
    db.commit()
    return {**group_out(db, g, True), "skipped": skipped}


@router.get("/projects/{pid}/mobile/run-groups")
def list_groups(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    gs = db.scalars(select(MobileRunGroup).where(MobileRunGroup.project_id == pid, MobileRunGroup.tenant_id == me.tenant_id)
                    .order_by(MobileRunGroup.id.desc()).limit(50)).all()
    return [group_out(db, g) for g in gs]


@router.get("/mobile/run-groups/{gid}")
def get_group(gid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    g = _owned(db, MobileRunGroup, gid, me, "project.read", "Mobile run")
    return group_out(db, g, True)


@router.post("/mobile/run-groups/{gid}/cancel")
def cancel_group(gid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    g = _owned(db, MobileRunGroup, gid, me, "run.cancel", "Mobile run")
    n = 0
    for r in db.scalars(select(MobileRun).where(MobileRun.group_id == g.id, MobileRun.status.in_(("queued", "running")))).all():
        r.cancel_requested = True
        if r.status == "queued":
            r.status, r.finished_at, r.error = "cancelled", utcnow(), "Cancelled before it started"
        n += 1
    audit(db, request, me, "mobile_run_cancelled", f"mobile run group {g.id}", runs=n)
    db.commit()
    return {"cancelled": n}


@router.post("/mobile/run-groups/{gid}/rerun", status_code=201)
def rerun_group(gid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    g = _owned(db, MobileRunGroup, gid, me, "run.execute", "Mobile run")
    case_ids = list(dict.fromkeys(r.case_id for r in db.scalars(select(MobileRun).where(MobileRun.group_id == g.id)).all() if r.case_id))
    if not g.provider_id or not case_ids:
        raise HTTPException(422, "The provider or the cases of this run no longer exist")
    body = GroupIn(name=g.name, provider_id=g.provider_id, devices=[Device(**{k: d.get(k, "") for k in ("name", "os_version", "platform")}) for d in g.devices],
                   case_ids=case_ids)
    return create_group(g.project_id, body, request, db, me)


@router.get("/mobile/runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    return run_out(_owned(db, MobileRun, rid, me, "project.read", "Mobile run"), True)


@router.get("/mobile/runs/{rid}/files/{name}")
def run_file(rid: int, name: str, db: Session = Depends(get_db), me: User = Depends(member)):
    r = _owned(db, MobileRun, rid, me, "evidence.read", "Mobile run")
    if not FILE_RE.match(name):
        raise HTTPException(404, "File not found")
    p = mobilerun.run_dir(r) / name
    if not p.exists():
        raise HTTPException(404, "File not found")
    mime = "video/webm" if name.endswith(".webm") else ("image/jpeg" if name.endswith(".jpg") else "image/png")
    return FileResponse(p, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/mobile/run-groups/{gid}/report.html")
def group_report(gid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    g = _owned(db, MobileRunGroup, gid, me, "report.read", "Mobile run")
    project = db.get(Project, g.project_id)
    html = mobilereport.html(db, g, project)
    return HTMLResponse(html, headers={"Content-Disposition": f'attachment; filename="mobile-run-{g.id}.html"',
                                       "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'"})


# --------------------------------------------------------------------------- live sessions
def session_out(s: MobileSession) -> dict:
    res = s.res or {}
    obs = res.get("obs") or {}
    return {"id": s.id, "project_id": s.project_id, "purpose": s.purpose, "status": s.status, "phase": s.phase,
            "device": s.device or {}, "target": s.target or {}, "goal": s.goal, "test_case_id": s.test_case_id,
            "session_url": s.session_url, "error": s.error, "steps": s.steps or [],
            "step_text": [describe(x) for x in (s.steps or [])], "log": (s.log or [])[-60:],
            "screen": {"seq": res.get("seq", -1), "ok": res.get("ok", True), "error": res.get("error", ""),
                       "note": res.get("note", ""), "has_image": bool(obs.get("shot")), "width": obs.get("width"),
                       "height": obs.get("height"), "url": obs.get("url", ""), "elements": obs.get("elements") or []},
            "busy": int(res.get("seq", -1)) < s.cmd_seq, "created_at": s.created_at, "closed_at": s.closed_at}


def _session(db: Session, sid: int, me: User, perm: str = "project.read") -> MobileSession:
    return _owned(db, MobileSession, sid, me, perm, "Device session")


@router.get("/projects/{pid}/mobile/sessions")
def list_sessions(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    ss = db.scalars(select(MobileSession).where(MobileSession.project_id == pid, MobileSession.tenant_id == me.tenant_id)
                    .order_by(MobileSession.id.desc()).limit(30)).all()
    return [{k: v for k, v in session_out(s).items() if k not in ("log", "screen", "steps")} for s in ss]


@router.post("/projects/{pid}/mobile/sessions", status_code=201)
def create_session(pid: int, body: SessionIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    project = _project(db, pid, me, "run.execute")
    if body.purpose == "ai":
        access.require(db, me, project, "ai.use")
        from .. import billing
        from ..models import Tenant
        tn = db.get(Tenant, me.tenant_id)
        billing.require_feature(db, tn, "ai", request)
        billing.consume_ai(db, tn, 1, request)
        if not live.ai_configured():
            raise HTTPException(503, "Claude is not configured or temporarily unavailable on this server")
    prov = _provider_for(db, body.provider_id, me, pid)
    if prov.kind == "emulator":
        if body.kind != "web":
            raise HTTPException(422, "The built-in phone emulation is for mobile web; use a device cloud or Appium for apps")
        if body.purpose == "record":
            raise HTTPException(422, "Recording needs a real device provider; with the built-in emulation, use the AI agent or the step editor")
    active = db.scalar(select(func.count()).select_from(MobileSession).where(MobileSession.tenant_id == me.tenant_id,
                                                                            MobileSession.status.in_(ACTIVE))) or 0
    if active >= 2:
        raise HTTPException(429, "Your organization already has 2 live device sessions; close one first")
    target = {"kind": body.kind, "platform": body.device.platform}
    if body.kind == "web":
        if not body.start_url.strip():
            raise HTTPException(422, "Enter the start URL")
        target["start_url"] = _check_url(body.start_url, "Start URL")
        pol = mobilerun.targets.build(db, project)
        if pol.problems:
            raise HTTPException(422, "Target policy: " + "; ".join(pol.problems))
    else:
        if body.app_id is not None:
            a = db.get(MobileApp, body.app_id)
            if not a or a.tenant_id != me.tenant_id or a.project_id != pid or a.archived:
                raise HTTPException(422, "Unknown app build")
            if a.platform != body.device.platform:
                raise HTTPException(422, f"The build is for {a.platform}, the device is {body.device.platform}")
            target["app_id"] = a.id
        elif body.app_package.strip():
            if not PKG_RE.match(body.app_package.strip()):
                raise HTTPException(422, "Invalid package name / bundle id")
            target["app_package"] = body.app_package.strip()
        else:
            raise HTTPException(422, "Choose an app build")
    goal = body.goal.strip()
    if body.test_case_id is not None:
        tc = db.get(TestCase, body.test_case_id)
        if not tc or tc.tenant_id != me.tenant_id or tc.project_id != pid:
            raise HTTPException(422, "Unknown manual test case")
        goal = (live.goal_from_case(tc) + ("\n\nAdditional instructions:\n" + goal if goal else ""))
    if body.purpose == "ai" and len(goal) < 10:
        raise HTTPException(422, "Describe what the test should do (or pick a manual test case)")
    s = MobileSession(tenant_id=me.tenant_id, project_id=pid, purpose=body.purpose, provider_id=prov.id,
                      device={**body.device.model_dump(), "provider_kind": prov.kind}, target=target, goal=goal[:8000],
                      test_case_id=body.test_case_id, phase="running" if body.purpose == "ai" else "", created_by=me.id)
    db.add(s)
    db.flush()
    audit(db, request, me, "mobile_session_started", f"mobile session {s.id}", purpose=s.purpose, provider=prov.kind)
    db.commit()
    out = session_out(s)
    if body.purpose == "ai":
        db.commit()
        live.start_agent(s.id)
    return out


@router.get("/mobile/sessions/{sid}")
def get_session(sid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    return session_out(_session(db, sid, me))


@router.get("/mobile/sessions/{sid}/screen")
def session_screen(sid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _session(db, sid, me, "evidence.read")
    img = live.screen_bytes(s.res or {})
    if not img:
        raise HTTPException(404, "No screen yet")
    return Response(img[0], media_type=img[1], headers={"Cache-Control": "no-store"})


def _sync(db: Session, sid: int, op: str, step: dict | None = None) -> dict:
    db.commit()  # release locks before waiting for the device
    try:
        seq = live.send(sid, op, step)
        return live.wait(sid, seq, 120)
    except live.Busy as e:
        raise HTTPException(409, str(e))
    except live.Gone as e:
        raise HTTPException(410, str(e))


@router.post("/mobile/sessions/{sid}/actions")
def session_action(sid: int, body: ActionIn, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _session(db, sid, me, "run.execute")
    if s.purpose == "ai" and s.phase == "running":
        raise HTTPException(409, "The AI agent is driving this device")
    try:
        step = validate_steps([body.step])[0]
    except Exception as e:
        raise HTTPException(422, f"Step: {str(e)[:800]}")
    res = _sync(db, sid, "perform", step)
    if res.get("ok") and body.record:
        try:
            live.append_step(sid, res.get("step") or step)
        except ValueError as e:
            raise HTTPException(422, str(e))
    db.expire_all()
    return session_out(_session(db, sid, me))


@router.post("/mobile/sessions/{sid}/refresh")
def session_refresh(sid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _session(db, sid, me, "run.execute")
    _sync(db, sid, "observe")
    db.expire_all()
    return session_out(_session(db, sid, me))


@router.post("/mobile/sessions/{sid}/inspect")
def session_inspect(sid: int, body: PointIn, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _session(db, sid, me)
    els = ((s.res or {}).get("obs") or {}).get("elements") or []
    return {"element": element_at(els, body.x, body.y), "x": body.x, "y": body.y}


@router.put("/mobile/sessions/{sid}/steps")
def session_steps(sid: int, body: StepsIn, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _session(db, sid, me, "case.write")
    try:
        s.steps = validate_steps(body.steps)
    except Exception as e:
        raise HTTPException(422, f"Steps: {str(e)[:800]}")
    db.commit()
    return session_out(s)


@router.post("/mobile/sessions/{sid}/stop")
def session_stop(sid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _session(db, sid, me, "run.execute")
    if s.phase == "running":
        s.phase = "stopped"
    db.commit()
    live.close(sid)
    audit(db, request, me, "mobile_session_closed", f"mobile session {sid}")
    db.commit()
    db.expire_all()
    return session_out(_session(db, sid, me))


@router.post("/mobile/sessions/{sid}/save", status_code=201)
def session_save(sid: int, body: SaveIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _session(db, sid, me, "case.write")
    if not s.steps:
        raise HTTPException(422, "There are no steps to save yet")
    t = s.target or {}
    src = "ai" if s.purpose == "ai" else "recorded"
    if body.case_id:
        c = _owned(db, MobileCase, body.case_id, me, "case.write", "Mobile case")
        if c.project_id != s.project_id:
            raise HTTPException(422, "The case belongs to another project")
        c.steps = validate_steps(s.steps)
    else:
        c = MobileCase(tenant_id=me.tenant_id, project_id=s.project_id, name=body.name.strip(), kind=t.get("kind", "native"),
                       platform="any" if t.get("kind") == "web" else t.get("platform", "android"), app_id=t.get("app_id"),
                       start_url=t.get("start_url", ""), steps=validate_steps(s.steps), source=src,
                       description=(s.goal or "")[:10_000], test_case_id=s.test_case_id, created_by=me.id)
        db.add(c)
    db.flush()
    audit(db, request, me, "mobile_case_saved_from_session", f"mobile case {c.id}", session=sid, source=src)
    db.commit()
    return case_out(c)
