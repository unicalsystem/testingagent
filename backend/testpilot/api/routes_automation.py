"""AI automation of manual test cases (jobs are executed by the worker; see automation.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from .. import access, ai, artifacts, storage_policy
from ..config import get_settings
from ..db import get_db
from ..models import AutomationItem, AutomationJob, Project, Run, TestCase, TestRun, TestRunItem, User, utcnow
from ..steps import validate_steps
from .deps import audit, member

router = APIRouter(tags=["automation"])
ACTIVE_ITEM = ("queued", "repair", "drafting", "repairing", "trial")
MAX_JOB_CASES = 1000

# Project permissions used here (access.PROJECT_MATRIX):
#   project.read   -> view settings, jobs and drafts
#   project.update -> edit the project's automation settings (setup steps, notes)
#   ai.use + case.write -> start / cancel / retry jobs; case.write -> approve or reject drafts
# plus the organization's AI switch (data-residency settings) for anything that calls Claude.


def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    return access.require(db, me, db.get(Project, pid), perm)


def _ai_allowed(db: Session, me: User, pid: int) -> None:
    if not access.has(db, me, pid, "ai.use") or not access.has(db, me, pid, "case.write"):
        raise HTTPException(403, "You do not have permission for this action on this project")
    if not storage_policy.row(db, me.tenant_id).ai_enabled:
        raise HTTPException(403, "AI features are turned off in your organization's data-residency settings")


def _steps(raw: list[dict]) -> list[dict]:
    try:
        return validate_steps(raw, get_settings().max_steps_per_case)
    except ValidationError as e:
        raise HTTPException(422, "Invalid steps — " + "; ".join(str(x["msg"]) for x in e.errors()[:5]))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---- project automation settings ----------------------------------------------------
class SettingsIn(BaseModel):
    setup_steps: list[dict] = Field(default_factory=list)
    app_notes: str = Field(default="", max_length=20000)


@router.get("/projects/{pid}/automation-settings")
def get_settings_(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _project(db, pid, me)
    return {"setup_steps": p.setup_steps or [], "app_notes": p.app_notes or ""}


@router.put("/projects/{pid}/automation-settings")
def put_settings(pid: int, body: SettingsIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _project(db, pid, me, "project.update")
    p.setup_steps, p.app_notes = _steps(body.setup_steps), body.app_notes
    audit(db, request, me, "automation_settings_updated", p.name, project_id=pid, setup_steps=len(p.setup_steps))
    db.commit()
    return {"setup_steps": p.setup_steps, "app_notes": p.app_notes}


# ---- jobs -----------------------------------------------------------------------------------
class JobOptions(BaseModel):
    verify: bool = True           # execute each draft in the browser before trusting it
    auto_apply: bool = True       # save verified drafts to the case without review
    record_results: bool = True   # a verified pass counts as a Pass in the source test run
    max_attempts: int = Field(default=2, ge=1, le=3)


class JobIn(BaseModel):
    case_ids: list[int] | None = Field(default=None, max_length=5000)
    test_run_id: int | None = None
    item_ids: list[int] | None = Field(default=None, max_length=5000)
    all_manual: bool = False
    options: JobOptions = Field(default_factory=JobOptions)
    confirm: bool = False


def _counts(db: Session, job_ids: list[int]) -> dict[int, dict[str, int]]:
    """job_ids must already be scoped to the caller's organization."""
    out: dict[int, dict[str, int]] = {j: {} for j in job_ids}
    if job_ids:
        for jid, st, n in db.execute(select(AutomationItem.job_id, AutomationItem.status, func.count())
                                     .where(AutomationItem.job_id.in_(job_ids))
                                     .group_by(AutomationItem.job_id, AutomationItem.status)).all():
            out[jid][st] = n
    return out


def _job_out(db: Session, j: AutomationJob, counts: dict[str, int]) -> dict:
    total = sum(counts.values())
    pending = sum(counts.get(s, 0) for s in ACTIVE_ITEM)
    tr = db.get(TestRun, j.test_run_id) if j.test_run_id else None
    tr = tr if tr and tr.tenant_id == j.tenant_id else None
    return {"id": j.id, "project_id": j.project_id, "status": j.status, "options": j.options,
            "test_run": {"id": tr.id, "name": tr.name} if tr else None, "created_at": j.created_at,
            "finished_at": j.finished_at, "claude_calls": j.claude_calls, "total": total, "counts": counts,
            "progress": round(100 * (total - pending) / total) if total else 100,
            "explore_log": j.explore_log, "has_map": bool(j.app_map)}


@router.post("/projects/{pid}/automation-jobs", status_code=201)
def create_job(pid: int, body: JobIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _project(db, pid, me)
    _ai_allowed(db, me, pid)
    st = ai.status()
    if not st["configured"]:
        raise HTTPException(503, "Claude API is not configured; AI automation needs it")
    if not body.confirm:
        raise HTTPException(422, "Review the summary and confirm to start AI automation")
    run_items: dict[int, int] = {}
    if body.test_run_id:
        tr = db.get(TestRun, body.test_run_id)
        if not tr or tr.tenant_id != me.tenant_id or tr.project_id != pid:
            raise HTTPException(404, "Test run not found")
        q = select(TestRunItem).where(TestRunItem.tenant_id == me.tenant_id, TestRunItem.test_run_id == tr.id)
        if body.item_ids:
            q = q.where(TestRunItem.id.in_(body.item_ids))
        for ti in db.scalars(q).all():
            run_items[ti.test_case_id] = ti.id
        ids = list(run_items)
    elif body.case_ids:
        ids = body.case_ids
    elif body.all_manual:
        ids = None
    else:
        raise HTTPException(422, "Choose the cases to automate")
    q = select(TestCase).where(TestCase.tenant_id == me.tenant_id, TestCase.project_id == pid,
                               TestCase.archived.is_(False)).order_by(TestCase.id)
    if ids is not None:
        q = q.where(TestCase.id.in_(ids))
    cases = [c for c in db.scalars(q).all() if not c.steps]  # only manual cases
    busy = set(db.scalars(select(AutomationItem.test_case_id).join(AutomationJob).where(
        AutomationJob.tenant_id == me.tenant_id, AutomationJob.project_id == pid,
        AutomationItem.status.in_(ACTIVE_ITEM))).all())
    cases = [c for c in cases if c.id not in busy]
    if not cases:
        raise HTTPException(422, "No manual test cases to automate in that selection (cases that already have "
                                 "automated steps, or are already being automated, are skipped)")
    if len(cases) > MAX_JOB_CASES:
        raise HTTPException(422, f"At most {MAX_JOB_CASES} cases per automation job; select fewer")
    job = AutomationJob(tenant_id=me.tenant_id, project_id=pid, test_run_id=body.test_run_id, created_by=me.id, status="exploring",
                        options=body.options.model_dump())
    db.add(job)
    db.flush()
    for pos, c in enumerate(cases):
        db.add(AutomationItem(tenant_id=me.tenant_id, job_id=job.id, test_case_id=c.id, position=pos, test_run_item_id=run_items.get(c.id)))
    audit(db, request, me, "automation_job_created", p.name, project_id=pid, job_id=job.id, cases=len(cases),
          options=body.options.model_dump())
    db.commit()
    return _job_out(db, job, _counts(db, [job.id])[job.id])


@router.get("/projects/{pid}/automation-jobs")
def list_jobs(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    jobs = db.scalars(select(AutomationJob).where(AutomationJob.tenant_id == me.tenant_id, AutomationJob.project_id == pid).order_by(AutomationJob.id.desc())
                      .limit(100)).all()
    counts = _counts(db, [j.id for j in jobs])
    return [_job_out(db, j, counts[j.id]) for j in jobs]


def _get_job(db: Session, jid: int, me: User, perm: str = "project.read") -> AutomationJob:
    """A job of the caller's organization in a project the caller may access now; otherwise not found."""
    j = db.get(AutomationJob, jid)
    if not j or j.tenant_id != me.tenant_id:
        raise HTTPException(404, "Automation job not found")
    try:
        access.require(db, me, db.get(Project, j.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Automation job not found")
        raise
    return j


@router.get("/automation-jobs/{jid}")
def get_job(jid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    j = _get_job(db, jid, me)
    items = db.scalars(select(AutomationItem).where(AutomationItem.tenant_id == me.tenant_id, AutomationItem.job_id == jid)
                       .options(selectinload(AutomationItem.case)).order_by(AutomationItem.position)).all()
    rids = [i.trial_run_id for i in items if i.trial_run_id]
    rstat = dict(db.execute(select(Run.id, Run.status).where(Run.tenant_id == me.tenant_id, Run.id.in_(rids))).all()) if rids else {}
    out = _job_out(db, j, _counts(db, [jid])[jid])
    out["app_map"] = j.app_map
    out["items"] = [{
        "id": i.id, "status": i.status, "attempts": i.attempts, "steps": i.steps, "use_setup": i.use_setup,
        "reason": i.reason, "error": i.error, "trial_run_id": i.trial_run_id,
        "trial_status": rstat.get(i.trial_run_id), "updated_at": i.updated_at,
        "case": {"id": i.case.id, "external_id": i.case.external_id, "name": i.case.name, "module": i.case.module,
                 "procedure": i.case.procedure, "expected_result": i.case.expected_result,
                 "has_steps": bool(i.case.steps)},
    } for i in items]
    return out


@router.post("/automation-jobs/{jid}/cancel")
def cancel_job(jid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    j = _get_job(db, jid, me, "case.write")
    if j.status in ("done", "cancelled"):
        raise HTTPException(409, f"Job already {j.status}")
    j.status, j.finished_at = "cancelled", utcnow()
    db.execute(update(AutomationItem).where(AutomationItem.tenant_id == me.tenant_id, AutomationItem.job_id == jid,
                                            AutomationItem.status.in_(("queued", "repair")))
               .values(status="cancelled"))
    audit(db, request, me, "automation_job_cancelled", str(jid), job_id=jid)
    db.commit()
    return _job_out(db, j, _counts(db, [jid])[jid])


class ApproveIn(BaseModel):
    steps: list[dict] | None = None
    use_setup: bool | None = None


def _get_item(db: Session, iid: int, me: User, perm: str = "case.write") -> AutomationItem:
    it = db.get(AutomationItem, iid)
    if not it or it.tenant_id != me.tenant_id:
        raise HTTPException(404, "Item not found")
    try:
        _get_job(db, it.job_id, me, perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Item not found")
        raise
    return it


def _apply(db: Session, it: AutomationItem, steps: list[dict], use_setup: bool, me: User) -> None:
    c = db.get(TestCase, it.test_case_id)
    if not c or c.tenant_id != it.tenant_id:
        raise HTTPException(404, "Test case not found")
    c.steps, c.use_setup = steps, use_setup
    c.attributes = {**(c.attributes or {}), "Automation": f"AI automated (job #{it.job_id}), reviewed"}
    it.steps, it.use_setup, it.status = steps, use_setup, "applied"
    db.flush()
    artifacts.queue_case_version(db, c, me.id)  # organizations with their own storage: versioned copy


@router.post("/automation-items/{iid}/approve")
def approve_item(iid: int, body: ApproveIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    it = _get_item(db, iid, me)
    if it.status in ACTIVE_ITEM:
        raise HTTPException(409, "This case is still being automated")
    steps = _steps(body.steps) if body.steps is not None else list(it.steps)
    if not steps:
        raise HTTPException(422, "There are no steps to save")
    _apply(db, it, steps, it.use_setup if body.use_setup is None else body.use_setup, me)
    audit(db, request, me, "automation_item_approved", str(iid), case_id=it.test_case_id, job_id=it.job_id)
    db.commit()
    return {"ok": True, "status": it.status}


@router.post("/automation-items/{iid}/reject")
def reject_item(iid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    it = _get_item(db, iid, me)
    if it.status in ACTIVE_ITEM:
        raise HTTPException(409, "This case is still being automated")
    it.status = "rejected"
    audit(db, request, me, "automation_item_rejected", str(iid), job_id=it.job_id)
    db.commit()
    return {"ok": True}


@router.post("/automation-items/{iid}/retry")
def retry_item(iid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    it = _get_item(db, iid, me)
    job = db.get(AutomationJob, it.job_id)
    _ai_allowed(db, me, job.project_id)
    if it.status in ACTIVE_ITEM:
        raise HTTPException(409, "This case is already being automated")
    job = db.get(AutomationJob, it.job_id)
    it.status, it.attempts, it.error = "queued", 0, ""
    if job.status in ("done", "cancelled"):
        job.status, job.finished_at = "running", None
    audit(db, request, me, "automation_item_retry", str(iid), job_id=it.job_id)
    db.commit()
    return {"ok": True}


class BulkApproveIn(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: ["verified"])


@router.post("/automation-jobs/{jid}/approve")
def approve_all(jid: int, body: BulkApproveIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    _get_job(db, jid, me, "case.write")
    allowed = {s for s in body.statuses if s in ("verified", "drafted", "failed")}
    items = db.scalars(select(AutomationItem).where(AutomationItem.tenant_id == me.tenant_id, AutomationItem.job_id == jid,
                                                    AutomationItem.status.in_(allowed))).all()
    n = 0
    for it in items:
        if it.steps:
            _apply(db, it, list(it.steps), it.use_setup, me)
            n += 1
    audit(db, request, me, "automation_bulk_approved", str(jid), job_id=jid, count=n, statuses=sorted(allowed))
    db.commit()
    return {"applied": n}
