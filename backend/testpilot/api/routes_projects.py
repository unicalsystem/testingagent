from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import ai, targets
from ..config import get_settings
from ..db import get_db
from ..models import Project, ProjectSecret, Run, TestCase, User
from ..security import SecretsUnavailable, encrypt_secret
from ..steps import ACTIONS, secret_names, validate_steps
from ..urlguard import TargetRejected, parse_target
from .deps import audit, require

router = APIRouter(tags=["projects"])
viewer, tester = require("viewer"), require("tester")


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


class GenerateIn(BaseModel):
    requirement: str = Field(min_length=10, max_length=20000)
    page_notes: str = Field(default="", max_length=20000)
    count: int = Field(default=3, ge=1, le=10)


def _project(db: Session, pid: int) -> Project:
    p = db.get(Project, pid)
    if not p or p.archived:
        raise HTTPException(404, "Project not found")
    return p


def _p_out(db: Session, p: Project) -> dict:
    pol = targets.build(db, p)
    cases = db.scalar(select(func.count()).select_from(TestCase).where(TestCase.project_id == p.id, TestCase.archived.is_(False)))
    last = db.scalars(select(Run).where(Run.project_id == p.id).order_by(Run.id.desc()).limit(1)).first()
    return {"id": p.id, "name": p.name, "description": p.description, "base_url": p.base_url,
            "extra_hosts": p.extra_hosts, "created_at": p.created_at, "updated_at": p.updated_at,
            "policy_problems": pol.problems, "case_count": cases,
            "last_run": {"id": last.id, "status": last.status, "created_at": last.created_at} if last else None}


def _validate_project(body: ProjectIn) -> None:
    try:
        parse_target(body.base_url)
        targets.project_hosts(body.base_url, body.extra_hosts)
    except TargetRejected as e:
        raise HTTPException(422, str(e))


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), _: User = Depends(viewer)):
    return [_p_out(db, p) for p in db.scalars(select(Project).where(Project.archived.is_(False)).order_by(Project.name)).all()]


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    _validate_project(body)
    p = Project(name=body.name, description=body.description, base_url=body.base_url.strip(),
                extra_hosts=[h.strip().lower() for h in body.extra_hosts if h.strip()], created_by=me.id)
    db.add(p)
    db.flush()
    audit(db, request, me, "project_created", p.name, project_id=p.id)
    db.commit()
    return _p_out(db, p)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = Depends(get_db), _: User = Depends(viewer)):
    return _p_out(db, _project(db, pid))


@router.put("/projects/{pid}")
def update_project(pid: int, body: ProjectIn, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    p = _project(db, pid)
    _validate_project(body)
    new_hosts = [h.strip().lower() for h in body.extra_hosts if h.strip()]
    retarget = body.base_url.strip() != p.base_url or sorted(new_hosts) != sorted(p.extra_hosts or [])
    has_secrets = db.scalar(select(func.count()).select_from(ProjectSecret).where(ProjectSecret.project_id == pid))
    if retarget and has_secrets and me.role != "admin":
        raise HTTPException(403, "This project holds secrets: only an admin can change its base URL or hosts")
    p.name, p.description, p.base_url = body.name, body.description, body.base_url.strip()
    p.extra_hosts = new_hosts
    audit(db, request, me, "project_updated", p.name, project_id=p.id, retargeted=retarget)
    db.commit()
    return _p_out(db, p)


@router.delete("/projects/{pid}")
def archive_project(pid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(require("admin"))):
    """Archives (hides) a project. Runs, reports and evidence are retained."""
    p = _project(db, pid)
    p.archived = True
    audit(db, request, me, "project_archived", p.name, project_id=p.id)
    db.commit()
    return {"ok": True}


# ---- secrets (write-only) ---------------------------------------------------
@router.get("/projects/{pid}/secrets")
def list_secrets(pid: int, db: Session = Depends(get_db), _: User = Depends(viewer)):
    _project(db, pid)
    return [{"name": s.name, "updated_at": s.updated_at}
            for s in db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == pid).order_by(ProjectSecret.name)).all()]


@router.put("/projects/{pid}/secrets")
def set_secret(pid: int, body: SecretIn, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    _project(db, pid)
    try:
        enc = encrypt_secret(body.value)
    except SecretsUnavailable as e:
        raise HTTPException(503, str(e))
    s = db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == pid, ProjectSecret.name == body.name)).first()
    if s:
        s.value_encrypted = enc
    else:
        db.add(ProjectSecret(project_id=pid, name=body.name, value_encrypted=enc))
    audit(db, request, me, "secret_set", body.name, project_id=pid)
    db.commit()
    return {"name": body.name}


@router.delete("/projects/{pid}/secrets/{name}")
def delete_secret(pid: int, name: str, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    s = db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == pid, ProjectSecret.name == name)).first()
    if not s:
        raise HTTPException(404, "Not found")
    db.delete(s)
    audit(db, request, me, "secret_deleted", name, project_id=pid)
    db.commit()
    return {"ok": True}


# ---- test cases --------------------------------------------------------------
def _c_out(c: TestCase) -> dict:
    return {"id": c.id, "project_id": c.project_id, "name": c.name, "description": c.description, "tags": c.tags,
            "steps": c.steps, "source": c.source, "created_at": c.created_at, "updated_at": c.updated_at}


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
def step_actions(_: User = Depends(viewer)):
    return ACTIONS


@router.get("/projects/{pid}/cases")
def list_cases(pid: int, db: Session = Depends(get_db), _: User = Depends(viewer)):
    _project(db, pid)
    cases = db.scalars(select(TestCase).where(TestCase.project_id == pid, TestCase.archived.is_(False)).order_by(TestCase.id)).all()
    out = []
    for c in cases:
        last = db.scalars(select(Run).where(Run.test_case_id == c.id).order_by(Run.id.desc()).limit(1)).first()
        d = _c_out(c)
        d["last_run"] = {"id": last.id, "status": last.status, "created_at": last.created_at} if last else None
        out.append(d)
    return out


@router.post("/projects/{pid}/cases", status_code=201)
def create_case(pid: int, body: CaseIn, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    _project(db, pid)
    c = TestCase(project_id=pid, name=body.name, description=body.description, tags=body.tags[:20],
                 steps=_clean_steps(body.steps), source=body.source, created_by=me.id)
    db.add(c)
    db.flush()
    audit(db, request, me, "case_created", c.name, project_id=pid, case_id=c.id)
    db.commit()
    return _c_out(c)


@router.get("/cases/{cid}")
def get_case(cid: int, db: Session = Depends(get_db), _: User = Depends(viewer)):
    c = db.get(TestCase, cid)
    if not c or c.archived:
        raise HTTPException(404, "Test case not found")
    return _c_out(c)


@router.put("/cases/{cid}")
def update_case(cid: int, body: CaseIn, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    c = db.get(TestCase, cid)
    if not c or c.archived:
        raise HTTPException(404, "Test case not found")
    c.name, c.description, c.tags, c.steps = body.name, body.description, body.tags[:20], _clean_steps(body.steps)
    audit(db, request, me, "case_updated", c.name, case_id=c.id)
    db.commit()
    return _c_out(c)


@router.delete("/cases/{cid}")
def archive_case(cid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    c = db.get(TestCase, cid)
    if not c or c.archived:
        raise HTTPException(404, "Test case not found")
    c.archived = True  # keep history: runs and evidence still reference it
    audit(db, request, me, "case_archived", c.name, case_id=c.id)
    db.commit()
    return {"ok": True}


# ---- AI generation -------------------------------------------------------------
@router.post("/projects/{pid}/ai/generate")
def ai_generate(pid: int, body: GenerateIn, request: Request, db: Session = Depends(get_db), me: User = Depends(tester)):
    p = _project(db, pid)
    pol = targets.build(db, p)
    secrets = [s.name for s in db.scalars(select(ProjectSecret).where(ProjectSecret.project_id == pid)).all()]
    try:
        out = ai.generate_test_cases(base_url=p.base_url, hosts=sorted(pol.policy.allowed_hosts) or [p.base_url],
                                     secret_names=secrets, requirement=body.requirement,
                                     page_notes=body.page_notes, count=body.count)
    except ai.AIUnavailable as e:
        raise HTTPException(503, str(e))
    audit(db, request, me, "ai_generate", p.name, project_id=pid, generated=len(out["test_cases"]))
    db.commit()
    return out  # drafts only: the user reviews and saves them explicitly
