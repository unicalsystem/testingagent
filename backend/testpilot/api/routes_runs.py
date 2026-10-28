from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import access, ai, artifacts, report, storage, storage_policy, targets
from ..db import get_db
from ..models import TERMINAL, Evidence, Project, ProjectSecret, Run, RunStep, Tenant, TestCase, User, utcnow
from ..steps import describe, secret_names
from ..storage_providers import StorageError
from .deps import audit, member

router = APIRouter(tags=["runs"])
read = execute = member


class RunIn(BaseModel):
    retries: int | None = None  # default retries for steps that don't set their own


def _ev(e: Evidence, prefix: str) -> dict:
    name = (e.rel_path or e.external_path).rsplit("/", 1)[-1]
    return {"id": e.id, "kind": e.kind, "step_id": e.step_id, "mime": e.mime, "size": e.size, "sha256": e.sha256,
            "file": name, "url": f"{prefix}/evidence/{e.id}", "storage": e.storage, "location": e.external_path,
            "upload_status": e.upload_status, "upload_error": e.upload_error,
            "available": e.deleted_at is None and e.upload_status != "lost"}


def _run_out(db: Session, r: Run, full: bool = False) -> dict:
    out = {"id": r.id, "project_id": r.project_id, "test_case_id": r.test_case_id, "case_name": r.case_snapshot.get("name"),
           "status": r.status, "error": r.error, "summary": r.summary, "cancel_requested": r.cancel_requested,
           "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at,
           "storage_mode": r.storage_mode, "storage_provider": r.storage_provider, "upload_state": r.upload_state,
           "details_external": r.details_external}
    if full:
        req = db.get(User, r.requested_by) if r.requested_by else None
        evidence = db.scalars(select(Evidence).where(Evidence.tenant_id == r.tenant_id, Evidence.run_id == r.id)
                              .order_by(Evidence.id)).all()
        t = db.get(Tenant, r.tenant_id)
        prefix = f"/api/t/{t.slug}"
        out.update(
            requested_by=req.username if req else None,
            worker=r.worker_id,
            ai_analysis=r.ai_analysis,
            ai_analysis_at=r.ai_analysis_at,
            report_synced_at=r.report_synced_at,
            report_sync_error=r.report_sync_error,
            steps=[{"id": s.id, "idx": s.idx, "action": s.action, "description": s.description, "status": s.status,
                    "attempts": s.attempts, "duration_ms": s.duration_ms, "error": s.error,
                    "evidence": [_ev(e, prefix) for e in evidence if e.step_id == s.id]} for s in r.steps],
            evidence=[_ev(e, prefix) for e in evidence if e.step_id is None],
        )
    return out


def _get_run(db: Session, rid: int, me: User, perm: str = "project.read") -> Run:
    """A run of the caller's tenant in a project the caller may access now; otherwise not found."""
    r = db.get(Run, rid)
    if not r or r.tenant_id != me.tenant_id:
        raise HTTPException(404, "Run not found")
    try:
        access.require(db, me, db.get(Project, r.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Run not found")
        raise
    return r


def queue_case_run(db: Session, c: TestCase, me: User, retries: int | None = None) -> Run:
    """Validates and queues one automated execution of a case (no commit). Checks tenant, project
    permission (run.execute) and the organization's storage policy, so every caller is covered."""
    if not c or c.archived or c.tenant_id != me.tenant_id:
        raise HTTPException(404, "Test case not found")
    try:
        p = access.require(db, me, db.get(Project, c.project_id), "run.execute")
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Test case not found")
        raise
    if not c.steps:
        raise HTTPException(422, "Test case has no automated steps")
    block = storage_policy.run_block_reason(db, me.tenant_id)
    if block:
        raise HTTPException(409, block)
    from ..automation import full_steps

    all_steps = full_steps(p, c.steps, c.use_setup)
    pol = targets.build(db, p)
    if pol.problems:
        raise HTTPException(422, "Cannot run: " + "; ".join(pol.problems) + ". Ask an admin to authorise the target.")
    needed = secret_names(all_steps)
    have = {s.name for s in db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == p.id)).all()}
    if needed - have:
        raise HTTPException(422, "Missing project secrets: " + ", ".join(sorted(needed - have)))
    steps = all_steps
    if retries is not None:
        for s in steps:
            s.setdefault("retries", max(0, min(retries, 5)))
    srow = storage_policy.row(db, me.tenant_id)
    # Provisional (the worker decides again when the run starts): a run cancelled while queued must
    # still follow the organization's storage choice (e.g. never be copied to the MySQL report store).
    r = Run(tenant_id=me.tenant_id, project_id=p.id, test_case_id=c.id, requested_by=me.id, status="queued",
            case_snapshot={"name": c.name, "description": c.description, "steps": steps},
            options={"retries": retries},
            storage_mode=srow.mode or "unical", storage_provider=srow.provider if srow.mode == "external" else "local")
    db.add(r)
    db.flush()
    for i, s in enumerate(steps):
        db.add(RunStep(tenant_id=me.tenant_id, run_id=r.id, idx=i, action=s["action"], description=describe(s)[:500]))
    return r


@router.post("/cases/{cid}/runs", status_code=201)
def start_run(cid: int, request: Request, body: RunIn | None = None, db: Session = Depends(get_db),
              me: User = Depends(execute)):
    c = db.get(TestCase, cid)
    r = queue_case_run(db, c, me, body.retries if body else None)
    audit(db, request, me, "run_queued", c.name, run_id=r.id, case_id=c.id)
    db.commit()
    db.refresh(r)
    return _run_out(db, r, full=True)


@router.get("/projects/{pid}/runs")
def list_runs(pid: int, limit: int = 100, db: Session = Depends(get_db), me: User = Depends(read)):
    access.require(db, me, db.get(Project, pid), "project.read")
    rows = db.scalars(select(Run).where(Run.tenant_id == me.tenant_id, Run.project_id == pid)
                      .order_by(Run.id.desc()).limit(max(1, min(limit, 500)))).all()
    return [_run_out(db, r) for r in rows]


@router.get("/runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db), me: User = Depends(read)):
    return _run_out(db, _get_run(db, rid, me), full=True)


@router.post("/runs/{rid}/cancel")
def cancel_run(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(execute)):
    r = _get_run(db, rid, me, "run.cancel")
    if r.status in TERMINAL:
        raise HTTPException(409, f"Run already {r.status}")
    # Atomic: only a run that is still queued is cancelled here; a running one is flagged and
    # the worker stops it at the next step boundary.
    res = db.execute(update(Run).where(Run.id == rid, Run.status == "queued")
                     .values(status="cancelled", finished_at=utcnow(), cancel_requested=True))
    if res.rowcount:
        db.execute(update(RunStep).where(RunStep.run_id == rid, RunStep.status == "pending").values(status="skipped"))
    else:
        db.execute(update(Run).where(Run.id == rid, Run.status == "running").values(cancel_requested=True))
    audit(db, request, me, "run_cancel", str(rid), run_id=rid)
    db.commit()
    db.refresh(r)
    return _run_out(db, r)


@router.post("/runs/{rid}/rerun", status_code=201)
def rerun(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(execute)):
    r = _get_run(db, rid, me, "run.execute")
    if not r.test_case_id:
        raise HTTPException(409, "The original test case no longer exists")
    return start_run(r.test_case_id, request, RunIn(retries=r.options.get("retries")), db, me)


@router.get("/runs/{rid}/report")
def download_report(rid: int, format: str = "html", db: Session = Depends(get_db),
                    me: User = Depends(member)):
    r = _get_run(db, rid, me, "report.read")
    if r.status not in TERMINAL:
        raise HTTPException(409, "The report is available when the run has finished")
    if format not in ("html", "json"):
        raise HTTPException(422, "format must be html or json")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", r.case_snapshot.get("name") or "run").strip("-")[:60]
    fname = f"testpilot-run-{r.id}-{name}.{ 'json' if format == 'json' else 'html'}"
    mime = "application/json" if format == "json" else "text/html; charset=utf-8"
    if r.storage_mode == "external" or r.details_external:
        # The report lives in the organization's own storage: serve that copy (never rebuilt here).
        ev = db.scalars(select(Evidence).where(Evidence.run_id == r.id, Evidence.kind == f"report_{format}")
                        .order_by(Evidence.id.desc())).first()
        if not ev:
            raise HTTPException(404, "This run has no stored report")
        body = _fetch(db, ev)
    else:
        data = report.build_data(db, r)
        body = report.to_json(data) if format == "json" else report.to_html(db, data)
    return Response(body, media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        # The report is a static document: no scripts, no external loads.
        "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
    })


@router.get("/evidence/{eid}")
def download_evidence(eid: int, download: bool = False, db: Session = Depends(get_db),
                      me: User = Depends(member)):
    e = db.get(Evidence, eid)
    if not e or e.tenant_id != me.tenant_id:
        raise HTTPException(404, "Evidence not found")
    try:
        access.require(db, me, db.get(Project, e.project_id), "evidence.read")
    except HTTPException as x:
        if x.status_code == 404:
            raise HTTPException(404, "Evidence not found")
        raise
    inline = e.kind == "screenshot" and not download
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"}
    media = e.mime if e.kind != "report_html" or download else "text/plain"
    if e.deleted_at:
        raise HTTPException(410, "This file was deleted (retention or a deletion request)")
    if e.storage == "local":
        try:
            path = storage.resolve_evidence(e.rel_path)
        except PermissionError:
            raise HTTPException(404, "Evidence not found")
        if not path.is_file():
            raise HTTPException(410, "Evidence file is missing from storage")
        return FileResponse(path, media_type=media, filename=path.name,
                            content_disposition_type="inline" if inline else "attachment", headers=headers)
    name = re.sub(r'[^A-Za-z0-9._-]+', "-", (e.rel_path or e.external_path).rsplit("/", 1)[-1] or f"evidence-{e.id}")
    headers["Content-Disposition"] = f'{"inline" if inline else "attachment"}; filename="{name}"'
    return Response(_fetch(db, e), media_type=media, headers=headers)


def _fetch(db: Session, e: Evidence) -> bytes:
    if e.upload_status == "lost":
        raise HTTPException(410, "This file was not stored: " + (e.upload_error or "storage unavailable"))
    try:
        return artifacts.fetch(db, e)
    except FileNotFoundError:
        raise HTTPException(410, "This file is no longer available")
    except PermissionError:
        raise HTTPException(404, "Evidence not found")
    except StorageError as x:
        raise HTTPException(502, f"Could not read the file from your organization's storage: {x}")


def _external_report(db: Session, r: Run) -> dict:
    ev = db.scalars(select(Evidence).where(Evidence.run_id == r.id, Evidence.kind == "report_json")
                    .order_by(Evidence.id.desc())).first()
    if not ev:
        raise HTTPException(404, "This run has no stored report")
    try:
        return json.loads(_fetch(db, ev))
    except ValueError:
        raise HTTPException(502, "The stored report could not be read")


@router.get("/runs/{rid}/details")
def run_details(rid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    """Step errors and AI analysis of a run whose details live in the organization's own storage."""
    r = _get_run(db, rid, me, "report.read")
    if not r.details_external:
        return {"error": r.error, "steps": [{"idx": s.idx, "error": s.error} for s in r.steps],
                "ai_analysis": r.ai_analysis, "source": "testpilot"}
    data = _external_report(db, r)
    ai_text = ""
    ai_ev = db.scalars(select(Evidence).where(Evidence.run_id == r.id, Evidence.kind == "ai_analysis",
                                              Evidence.deleted_at.is_(None)).order_by(Evidence.id.desc())).first()
    if ai_ev and ai_ev.upload_status != "lost":
        try:
            ai_text = artifacts.fetch(db, ai_ev).decode("utf-8", "replace")
        except (StorageError, FileNotFoundError, PermissionError):
            ai_text = ""
    return {"error": (data.get("run") or {}).get("error") or "", "ai_analysis": ai_text or (data.get("run") or {}).get("ai_analysis") or "",
            "steps": [{"idx": s["index"] - 1, "error": s.get("error") or ""} for s in data.get("steps", [])],
            "source": "organization storage"}


@router.post("/runs/{rid}/ai/analyze")
def ai_analyze(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    r = _get_run(db, rid, me, "ai.use")
    if r.status not in ("failed", "error"):
        raise HTTPException(409, "Analysis is available for failed runs")
    if not storage_policy.row(db, r.tenant_id).ai_enabled:
        raise HTTPException(403, "AI features are turned off in your organization's data-residency settings")
    errors = {s.idx: s.error for s in r.steps}
    run_error = r.error
    if r.details_external:
        data = _external_report(db, r)
        errors = {s["index"] - 1: s.get("error") or "" for s in data.get("steps", [])}
        run_error = (data.get("run") or {}).get("error") or ""
    rows = [{"idx": s.idx, "status": s.status, "action": s.action, "description": s.description,
             "error": errors.get(s.idx, "")} for s in r.steps]
    shot, mime = None, "image/jpeg"
    failed = next((s for s in r.steps if s.status == "failed"), None)
    if failed:
        ev = db.scalars(select(Evidence).where(Evidence.step_id == failed.id, Evidence.kind == "screenshot",
                                               Evidence.deleted_at.is_(None)).order_by(Evidence.id.desc())).first()
        if ev:
            try:
                shot, mime = artifacts.fetch(db, ev), ev.mime
            except (OSError, PermissionError, StorageError):
                pass
    try:
        text = ai.analyze_run(case_name=r.case_snapshot.get("name", ""), step_rows=rows, run_error=run_error,
                              screenshot=shot, screenshot_mime=mime)
    except ai.AIUnavailable as e:
        raise HTTPException(503, str(e))
    saved = True
    if r.storage_mode == "external":
        saved = _save_external_analysis(db, r, text)  # kept only in the organization's storage
    else:
        r.ai_analysis = text
        r.report_synced_at = None  # re-push the report (now with analysis) to the MySQL store
    r.ai_analysis_at = utcnow()
    audit(db, request, me, "ai_analyze", str(rid), run_id=rid)
    db.commit()
    return {"ai_analysis": text, "ai_analysis_at": r.ai_analysis_at, "saved": saved}


def _save_external_analysis(db: Session, r: Run, text: str) -> bool:
    srow = storage_policy.row(db, r.tenant_id)
    if srow.mode != "external" or srow.provider != r.storage_provider:
        return False
    try:
        prov = storage_policy.provider_for(db, srow)
        f = storage_policy.folders(db, srow)
        parts = storage_policy.run_parts(f, db.get(Project, r.project_id), r.id)
        sub = storage_policy.sub_for_kind(f, "ai_analysis")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / f"ai-analysis-{utcnow():%Y%m%d-%H%M%S}.txt"
            p.write_text(text, encoding="utf-8")
            sha = storage.sha256_file(p)
            up = prov.upload(prov.ensure_folder([*parts, sub]), p, p.name, "text/plain", sha)
            db.add(Evidence(tenant_id=r.tenant_id, project_id=r.project_id, test_case_id=r.test_case_id, run_id=r.id,
                            kind="ai_analysis", rel_path="", mime="text/plain", size=p.stat().st_size, sha256=sha,
                            storage=prov.name, external_ref=up.ref, upload_status="uploaded",
                            external_path="/".join([prov.base_path(), *parts, sub, p.name]).strip("/")[:1024]))
        return True
    except StorageError:
        return False
