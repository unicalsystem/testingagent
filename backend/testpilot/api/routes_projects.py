from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import access, ai, artifacts, permissions, storage_policy, targets
from ..config import get_settings
from ..db import get_db
from ..models import Project, ProjectAssignment, ProjectSecret, Run, TestCase, User
from ..security import SecretsUnavailable, encrypt_secret
from ..steps import ACTIONS, secret_names, validate_steps
from ..urlguard import TargetRejected, parse_target
from .deps import audit, member, require_perm

router = APIRouter(tags=["projects"])
read = member


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=5000)
    base_url: str = Field(min_length=8, max_length=2048)
    extra_hosts: list[str] = Field(default_factory=list, max_length=20)


class SecretIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    value: str = Field(min_length=1, max_length=5000)


class CaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    steps: list[dict] = Field(default_factory=list)
    source: str = Field(default="manual", pattern="^(manual|ai)$")
    # test-management fields (optional; None = leave unchanged on update)
    external_id: str | None = Field(default=None, max_length=100)
    module: str | None = Field(default=None, max_length=200)
    sub_module: str | None = Field(default=None, max_length=200)
    priority: str | None = Field(default=None, max_length=40)
    scenario_type: str | None = Field(default=None, max_length=80)
    assigned_to: str | None = Field(default=None, max_length=120)
    requirement: str | None = Field(default=None, max_length=20000)
    preconditions: str | None = Field(default=None, max_length=20000)
    test_data: str | None = Field(default=None, max_length=20000)
    procedure: str | None = Field(default=None, max_length=20000)
    expected_result: str | None = Field(default=None, max_length=20000)
    use_setup: bool | None = None


CASE_META = ("external_id", "module", "sub_module", "priority", "scenario_type", "assigned_to", "requirement",
             "preconditions", "test_data", "procedure", "expected_result")


class BulkCasesIn(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=5000)


class GenerateIn(BaseModel):
    requirement: str = Field(min_length=10, max_length=20000)
    page_notes: str = Field(default="", max_length=20000)
    count: int = Field(default=3, ge=1, le=10)


def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    """A project of the caller's tenant that the caller may access now (assignments are evaluated on
    every call). Invisible projects are reported as not found; missing permissions as 403."""
    return access.require(db, me, db.get(Project, pid), perm)


def _p_out(db: Session, p: Project, me: User | None = None) -> dict:
    pol = targets.build(db, p)
    cases = db.scalar(select(func.count()).select_from(TestCase).where(TestCase.project_id == p.id, TestCase.archived.is_(False)))
    last = db.scalars(select(Run).where(Run.project_id == p.id).order_by(Run.id.desc()).limit(1)).first()
    return {"id": p.id, "name": p.name, "description": p.description, "base_url": p.base_url,
            "extra_hosts": p.extra_hosts, "created_at": p.created_at, "updated_at": p.updated_at,
            "policy_problems": pol.problems, "case_count": cases,
            "setup_steps": p.setup_steps or [], "app_notes": p.app_notes or "",
            "last_run": {"id": last.id, "status": last.status, "created_at": last.created_at} if last else None,
            "my_permissions": sorted(access.perms_for(db, me, p.id)) if me else []}


def _validate_project(body: ProjectIn) -> None:
    try:
        parse_target(body.base_url)
        targets.project_hosts(body.base_url, body.extra_hosts)
    except TargetRejected as e:
        raise HTTPException(422, str(e))


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), me: User = Depends(read)):
    q = select(Project).where(Project.tenant_id == me.tenant_id, Project.archived.is_(False)).order_by(Project.name)
    ids = access.accessible_project_ids(db, me)
    if ids is not None:
        q = q.where(Project.id.in_(ids or [-1]))
    return [_p_out(db, p, me) for p in db.scalars(q).all()]


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, request: Request, db: Session = Depends(get_db),
                   me: User = Depends(require_perm("project.create"))):
    _validate_project(body)
    p = Project(tenant_id=me.tenant_id, name=body.name, description=body.description, base_url=body.base_url.strip(),
                extra_hosts=[h.strip().lower() for h in body.extra_hosts if h.strip()], created_by=me.id)
    db.add(p)
    db.flush()
    if not access.is_admin(me):
        # the creator keeps access to what they created, whatever their other assignments say
        db.add(ProjectAssignment(tenant_id=me.tenant_id, user_id=me.id, scope="project", project_id=p.id,
                                 role=me.role if me.role in access.ROLE_RANK else access.QA, created_by=me.id,
                                 reason="Project creator"))
    audit(db, request, me, "project_created", p.name, project_id=p.id)
    db.commit()
    return _p_out(db, p, me)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = Depends(get_db), me: User = Depends(read)):
    return _p_out(db, _project(db, pid, me), me)


@router.put("/projects/{pid}")
def update_project(pid: int, body: ProjectIn, request: Request, db: Session = Depends(get_db),
                   me: User = Depends(member)):
    p = _project(db, pid, me, "project.update")
    _validate_project(body)
    new_hosts = [h.strip().lower() for h in body.extra_hosts if h.strip()]
    retarget = body.base_url.strip() != p.base_url or sorted(new_hosts) != sorted(p.extra_hosts or [])
    has_secrets = db.scalar(select(func.count()).select_from(ProjectSecret).where(ProjectSecret.project_id == pid))
    if retarget and has_secrets and not access.has(db, me, p.id, "project.retarget_with_secrets"):
        raise HTTPException(403, "This project holds secrets: only an admin can change its base URL or hosts")
    p.name, p.description, p.base_url = body.name, body.description, body.base_url.strip()
    p.extra_hosts = new_hosts
    audit(db, request, me, "project_updated", p.name, project_id=p.id, retargeted=retarget)
    db.commit()
    return _p_out(db, p, me)


@router.delete("/projects/{pid}")
def archive_project(pid: int, request: Request, db: Session = Depends(get_db),
                    me: User = Depends(member)):
    """Archives (hides) a project. Runs, reports and evidence are retained."""
    p = _project(db, pid, me, "project.archive")
    p.archived = True
    audit(db, request, me, "project_archived", p.name, project_id=p.id)
    db.commit()
    return {"ok": True}


# ---- secrets (write-only) ---------------------------------------------------
@router.get("/projects/{pid}/secrets")
def list_secrets(pid: int, db: Session = Depends(get_db), me: User = Depends(read)):
    _project(db, pid, me)
    return [{"name": s.name, "updated_at": s.updated_at}
            for s in db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == pid).order_by(ProjectSecret.name)).all()]


@router.put("/projects/{pid}/secrets")
def set_secret(pid: int, body: SecretIn, request: Request, db: Session = Depends(get_db),
               me: User = Depends(member)):
    _project(db, pid, me, "secret.write")
    try:
        enc = encrypt_secret(body.value)
    except SecretsUnavailable as e:
        raise HTTPException(503, str(e))
    s = db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == pid, ProjectSecret.name == body.name)).first()
    if s:
        s.value_encrypted = enc
    else:
        db.add(ProjectSecret(tenant_id=me.tenant_id, project_id=pid, name=body.name, value_encrypted=enc))
    audit(db, request, me, "secret_set", body.name, project_id=pid)
    db.commit()
    return {"name": body.name}


@router.delete("/projects/{pid}/secrets/{name}")
def delete_secret(pid: int, name: str, request: Request, db: Session = Depends(get_db),
                  me: User = Depends(member)):
    _project(db, pid, me, "secret.write")
    s = db.scalars(select(ProjectSecret).where(ProjectSecret.tenant_id == me.tenant_id, ProjectSecret.project_id == pid,
                                               ProjectSecret.name == name)).first()
    if not s:
        raise HTTPException(404, "Not found")
    db.delete(s)
    audit(db, request, me, "secret_deleted", name, project_id=pid)
    db.commit()
    return {"ok": True}


# ---- test cases --------------------------------------------------------------
def _c_out(c: TestCase) -> dict:
    out = {"id": c.id, "project_id": c.project_id, "name": c.name, "description": c.description, "tags": c.tags,
           "steps": c.steps, "source": c.source, "created_at": c.created_at, "updated_at": c.updated_at,
           "attributes": c.attributes or {}, "import_batch": c.import_batch or "", "use_setup": bool(c.use_setup)}
    for k in CASE_META:
        out[k] = getattr(c, k) or ""
    return out


def _clean_steps(steps: list[dict]) -> list[dict]:
    try:
        return validate_steps(steps, get_settings().max_steps_per_case)
    except ValidationError as e:
        errs = [f"step {err['loc'][0] + 1 if err['loc'] and isinstance(err['loc'][0], int) else '?'}: {err['msg']}"
                for err in e.errors()[:10]]
        raise HTTPException(422, "Invalid steps — " + "; ".join(errs))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/steps/actions")
def step_actions(_: User = Depends(read)):
    return ACTIONS


@router.get("/projects/{pid}/cases")
def list_cases(pid: int, db: Session = Depends(get_db), me: User = Depends(read)):
    _project(db, pid, me)
    cases = db.scalars(select(TestCase).where(TestCase.tenant_id == me.tenant_id, TestCase.project_id == pid,
                                              TestCase.archived.is_(False)).order_by(TestCase.id)).all()
    # latest automated run per case in one query (projects can hold thousands of imported cases)
    latest = (select(func.max(Run.id).label("rid")).where(Run.tenant_id == me.tenant_id, Run.project_id == pid,
                                                         Run.test_case_id.is_not(None))
              .group_by(Run.test_case_id).subquery())
    last = {r.test_case_id: r for r in db.scalars(select(Run).join(latest, Run.id == latest.c.rid)).all()}
    out = []
    for c in cases:
        d = _c_out(c)
        lr = last.get(c.id)
        d["last_run"] = {"id": lr.id, "status": lr.status, "created_at": lr.created_at} if lr else None
        out.append(d)
    return out


@router.post("/projects/{pid}/cases/archive")
def archive_cases(pid: int, body: BulkCasesIn, request: Request, db: Session = Depends(get_db),
                  me: User = Depends(member)):
    """Bulk archive (e.g. to undo an import). History is kept."""
    _project(db, pid, me, "case.write")
    rows = db.scalars(select(TestCase).where(TestCase.tenant_id == me.tenant_id, TestCase.project_id == pid, TestCase.id.in_(body.case_ids),
                                             TestCase.archived.is_(False))).all()
    for c in rows:
        c.archived = True
    audit(db, request, me, "cases_archived", str(len(rows)), project_id=pid, count=len(rows))
    db.commit()
    return {"archived": len(rows)}


@router.post("/projects/{pid}/cases", status_code=201)
def create_case(pid: int, body: CaseIn, request: Request, db: Session = Depends(get_db),
                me: User = Depends(member)):
    _project(db, pid, me, "case.write")
    c = TestCase(tenant_id=me.tenant_id, project_id=pid, name=body.name, description=body.description, tags=body.tags[:20],
                 steps=_clean_steps(body.steps), source=body.source, created_by=me.id,
                 use_setup=bool(body.use_setup), **{k: getattr(body, k) or "" for k in CASE_META})
    db.add(c)
    db.flush()
    artifacts.queue_case_version(db, c, me.id)  # organizations with their own storage: versioned JSON copy
    audit(db, request, me, "case_created", c.name, project_id=pid, case_id=c.id)
    db.commit()
    return _c_out(c)


def _case(db: Session, cid: int, me: User, perm: str = "project.read") -> TestCase:
    c = db.get(TestCase, cid)
    if not c or c.archived or c.tenant_id != me.tenant_id:
        raise HTTPException(404, "Test case not found")
    try:
        access.require(db, me, db.get(Project, c.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Test case not found")
        raise
    return c


@router.get("/cases/{cid}")
def get_case(cid: int, db: Session = Depends(get_db), me: User = Depends(read)):
    return _c_out(_case(db, cid, me))


@router.put("/cases/{cid}")
def update_case(cid: int, body: CaseIn, request: Request, db: Session = Depends(get_db),
                me: User = Depends(member)):
    c = _case(db, cid, me, "case.write")
    c.name, c.description, c.tags, c.steps = body.name, body.description, body.tags[:20], _clean_steps(body.steps)
    for k in CASE_META:
        if getattr(body, k) is not None:
            setattr(c, k, getattr(body, k))
    if body.use_setup is not None:
        c.use_setup = body.use_setup
    db.flush()
    artifacts.queue_case_version(db, c, me.id)
    audit(db, request, me, "case_updated", c.name, case_id=c.id)
    db.commit()
    return _c_out(c)


@router.delete("/cases/{cid}")
def archive_case(cid: int, request: Request, db: Session = Depends(get_db),
                 me: User = Depends(member)):
    c = _case(db, cid, me, "case.write")
    c.archived = True  # keep history: runs and evidence still reference it
    audit(db, request, me, "case_archived", c.name, case_id=c.id)
    db.commit()
    return {"ok": True}


# ---- AI generation -------------------------------------------------------------
@router.post("/projects/{pid}/ai/generate")
def ai_generate(pid: int, body: GenerateIn, request: Request, db: Session = Depends(get_db),
                me: User = Depends(member)):
    p = _project(db, pid, me, "ai.use")
    if not storage_policy.row(db, me.tenant_id).ai_enabled:
        raise HTTPException(403, "AI features are turned off in your organization's data-residency settings")
    pol = targets.build(db, p)
    secrets = [s.name for s in db.scalars(select(ProjectSecret).where(ProjectSecret.tenant_id == me.tenant_id,
                                                                      ProjectSecret.project_id == pid)).all()]
    try:
        out = ai.generate_test_cases(base_url=p.base_url, hosts=sorted(pol.policy.allowed_hosts) or [p.base_url],
                                     secret_names=secrets, requirement=body.requirement,
                                     page_notes=body.page_notes, count=body.count)
    except ai.AIUnavailable as e:
        raise HTTPException(503, str(e))
    audit(db, request, me, "ai_generate", p.name, project_id=pid, generated=len(out["test_cases"]))
    db.commit()
    return out  # drafts only: the user reviews and saves them explicitly


@router.post("/cases/{cid}/ai/automate")
def ai_automate(cid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    """Drafts automated steps for a manual (e.g. imported) case from its procedure and expected result.
    Returns a draft only; the tester reviews it in the editor and saves explicitly."""
    c = _case(db, cid, me, "ai.use")
    if not storage_policy.row(db, me.tenant_id).ai_enabled:
        raise HTTPException(403, "AI features are turned off in your organization's data-residency settings")
    if not (c.procedure or c.expected_result or c.description):
        raise HTTPException(422, "The case has no procedure, expected result or description to automate from")
    p = db.get(Project, c.project_id)
    pol = targets.build(db, p)
    secrets = [s.name for s in db.scalars(select(ProjectSecret).where(ProjectSecret.tenant_id == me.tenant_id,
                                                                      ProjectSecret.project_id == p.id)).all()]
    req = (f"Automate this existing manual test case as ONE test case.\nTitle: {c.name}\n"
           + (f"Description: {c.description}\n" if c.description else "")
           + (f"Pre-conditions: {c.preconditions}\n" if c.preconditions else "")
           + (f"Test data: {c.test_data}\n" if c.test_data else "")
           + (f"Procedure:\n{c.procedure}\n" if c.procedure else "")
           + (f"Expected result: {c.expected_result}\n" if c.expected_result else ""))
    try:
        out = ai.generate_test_cases(base_url=p.base_url, hosts=sorted(pol.policy.allowed_hosts) or [p.base_url],
                                     secret_names=secrets, requirement=req[:20000], count=1)
    except ai.AIUnavailable as e:
        raise HTTPException(503, str(e))
    audit(db, request, me, "ai_automate", c.name, case_id=c.id)
    db.commit()
    if not out["test_cases"]:
        raise HTTPException(502, "Claude did not return valid steps; try again or write them by hand")
    return {"steps": out["test_cases"][0]["steps"], "model": out["model"]}
