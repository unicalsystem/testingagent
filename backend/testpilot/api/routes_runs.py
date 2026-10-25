from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import ai, report, storage, targets
from ..db import get_db
from ..models import TERMINAL, Evidence, Project, ProjectSecret, Run, RunStep, TestCase, User, utcnow
from ..steps import describe, secret_names
from .deps import audit, require

router = APIRouter(tags=["runs"])
viewer, tester = require("viewer"), require("tester")


class RunIn(BaseModel):
    retries: int | None = None  # default retries for steps that don't set their own


def _ev(e: Evidence) -> dict:
    return {"id": e.id, "kind": e.kind, "step_id": e.step_id, "mime": e.mime, "size": e.size, "sha256": e.sha256,
            "file": e.rel_path.rsplit("/", 1)[-1], "url": f"/api/evidence/{e.id}"}


def _run_out(db: Session, r: Run, full: bool = False) -> dict:
    out = {"id": r.id, "project_id": r.project_id, "test_case_id": r.test_case_id, "case_name": r.case_snapshot.get("name"),
           "status": r.status, "error": r.error, "summary": r.summary, "cancel_requested": r.cancel_requested,
           "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at}
    if full:
        req = db.get(User, r.requested_by) if r.requested_by else None
        evidence = db.scalars(select(Evidence).where(Evidence.run_id == r.id).order_by(Evidence.id)).all()
        out.update(
            requested_by=req.username if req else None,
            worker=r.worker_id,
            ai_analysis=r.ai_analysis,
            ai_analysis_at=r.ai_analysis_at,
            report_synced_at=r.report_synced_at,
            report_sync_error=r.report_sync_error,
            steps=[{"id": s.id, "idx": s.idx, "action": s.action, "description": s.description, "status": s.status,
                    "attempts": s.attempts, "duration_ms": s.duration_ms, "error": s.error,
                    "evidence": [_ev(e) for e in evidence if e.step_id == s.id]} for s in r.steps],
            evidence=[_ev(e) for e in evidence if e.step_id is None],
        )
    return out


def _get_run(db: Session, rid: int) -> Run:
    r = db.get(Run, rid)
    if not r:
        raise HTTPException(404, "Run not found")
    return r


@router.post("/cases/{cid}/runs", status_code=201)
def start_run(cid: int, request: Request, body: RunIn | None = None, db: Session = Depends(get_db),
              me: User = Depends(tester)):
    c = db.get(TestCase, cid)
    if not c or c.archived:
        raise HTTPException(404, "Test case not found")
    p = db.get(Project, c.project_id)
    if not c.steps:
        raise HTTPException(422, "Test case has no steps")
    pol = targets.build(db, p)
    if pol.problems:
        raise HTTPException(422, "Cannot run: " + "; ".join(pol.problems) + ". Ask an admin to authorise the target.")
    needed = secret_names(c.steps)
    have = {s.name for s in db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == p.id)).all()}
    if needed - have:
        raise HTTPException(422, "Missing project secrets: " + ", ".join(sorted(needed - have)))
    steps = [dict(s) for s in c.steps]
    if body and body.retries is not None:
        for s in steps:
            s.setdefault("retries", max(0, min(body.retries, 5)))
    r = Run(project_id=p.id, test_case_id=c.id, requested_by=me.id, status="queued",
            case_snapshot={"name": c.name, "description": c.description, "steps": steps},
            options={"retries": body.retries if body else None})
    db.add(r)
    db.flush()
    for i, s in enumerate(steps):
        db.add(RunStep(run_id=r.id, idx=i, action=s["action"], description=describe(s)[:500]))
    audit(db, request, me, "run_queued", c.name, run_id=r.id, case_id=c.id)
    db.commit()
    db.refresh(r)
    return _run_out(db, r, full=True)


@router.get("/projects/{pid}/runs")
def list_runs(pid: int, limit: int = 100, db: Session = Depends(get_db), _: User = Depends(viewer)):
    rows = db.scalars(select(Run).where(Run.project_id == pid).order_by(Run.id.desc()).limit(min(limit, 500))).all()
    return [_run_out(db, r) for r in rows]


@router.get("/runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db), _: User = Depends(viewer)):
    return _run_out(db, _get_run(db, rid), full=True)


@router.post("/runs/{rid}/cancel")
def cancel_run(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    r = _get_run(db, rid)
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
def rerun(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    r = _get_run(db, rid)
    if not r.test_case_id:
        raise HTTPException(409, "The original test case no longer exists")
    return start_run(r.test_case_id, request, RunIn(retries=r.options.get("retries")), db, me)


@router.get("/runs/{rid}/report")
def download_report(rid: int, format: str = "html", db: Session = Depends(get_db), me: User = Depends(viewer)):
    r = _get_run(db, rid)
    if r.status not in TERMINAL:
        raise HTTPException(409, "The report is available when the run has finished")
    data = report.build_data(db, r)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", r.case_snapshot.get("name") or "run").strip("-")[:60]
    fname = f"testpilot-run-{r.id}-{name}.{ 'json' if format == 'json' else 'html'}"
    if format == "json":
        body, mime = report.to_json(data), "application/json"
    elif format == "html":
        body, mime = report.to_html(db, data), "text/html; charset=utf-8"
    else:
        raise HTTPException(422, "format must be html or json")
    return Response(body, media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        # The report is a static document: no scripts, no external loads.
        "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
    })


@router.get("/evidence/{eid}")
def download_evidence(eid: int, download: bool = False, db: Session = Depends(get_db), _: User = Depends(viewer)):
    e = db.get(Evidence, eid)
    if not e:
        raise HTTPException(404, "Evidence not found")
    try:
        path = storage.resolve_evidence(e.rel_path)
    except PermissionError:
        raise HTTPException(404, "Evidence not found")
    if not path.is_file():
        raise HTTPException(410, "Evidence file is missing from storage")
    inline = e.kind == "screenshot" and not download
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"}
    return FileResponse(path, media_type=e.mime if e.kind != "report_html" or download else "text/plain",
                        filename=path.name, content_disposition_type="inline" if inline else "attachment",
                        headers=headers)


@router.post("/runs/{rid}/ai/analyze")
def ai_analyze(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    r = _get_run(db, rid)
    if r.status not in ("failed", "error"):
        raise HTTPException(409, "Analysis is available for failed runs")
    rows = [{"idx": s.idx, "status": s.status, "action": s.action, "description": s.description, "error": s.error}
            for s in r.steps]
    shot, mime = None, "image/jpeg"
    failed = next((s for s in r.steps if s.status == "failed"), None)
    if failed:
        ev = db.scalars(select(Evidence).where(Evidence.step_id == failed.id, Evidence.kind == "screenshot")
                        .order_by(Evidence.id.desc())).first()
        if ev:
            try:
                shot, mime = storage.resolve_evidence(ev.rel_path).read_bytes(), ev.mime
            except (OSError, PermissionError):
                pass
    try:
        text = ai.analyze_run(case_name=r.case_snapshot.get("name", ""), step_rows=rows, run_error=r.error,
                              screenshot=shot, screenshot_mime=mime)
    except ai.AIUnavailable as e:
        raise HTTPException(503, str(e))
    r.ai_analysis, r.ai_analysis_at = text, utcnow()
    r.report_synced_at = None  # re-push the report (now with analysis) to the MySQL store
    audit(db, request, me, "ai_analyze", str(rid), run_id=rid)
    db.commit()
    return {"ai_analysis": text, "ai_analysis_at": r.ai_analysis_at}
