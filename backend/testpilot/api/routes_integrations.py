"""Connectors to Azure DevOps, Jira, GitHub, GitLab and webhooks (release 1.7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import access, integrations as ig
from ..db import get_db
from ..models import Integration, IntegrationIssue, IntegrationSync, Project, User, utcnow
from ..security import SecretsUnavailable
from .deps import audit, member

router = APIRouter(tags=["integrations"])


class IntegrationIn(BaseModel):
    kind: str = Field(pattern="^(azure_devops|jira|github|gitlab|webhook)$")
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    trigger: str = Field(default="auto", pattern="^(auto|failures|manual)$")
    run_types: list[str] = Field(default_factory=lambda: ["api", "browser", "manual"], max_length=3)
    config: dict = Field(default_factory=dict)
    secrets: dict = Field(default_factory=dict)  # empty values keep the stored ones


class PushIn(BaseModel):
    source_type: str = Field(pattern="^(api|browser|manual)$")
    source_id: int
    integration_ids: list[int] | None = None


def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    return access.require(db, me, db.get(Project, pid), perm)


def _integ(db: Session, iid: int, me: User, perm: str = "project.read") -> Integration:
    i = db.get(Integration, iid)
    if not i or i.tenant_id != me.tenant_id:
        raise HTTPException(404, "Connector not found")
    try:
        access.require(db, me, db.get(Project, i.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Connector not found")
        raise
    return i


def out(db: Session, i: Integration) -> dict:
    counts = dict(db.execute(select(IntegrationSync.status, func.count()).where(IntegrationSync.integration_id == i.id)
                             .group_by(IntegrationSync.status)).all())
    issues = db.scalar(select(func.count()).select_from(IntegrationIssue).where(IntegrationIssue.integration_id == i.id)) or 0
    return {"id": i.id, "project_id": i.project_id, "kind": i.kind, "kind_label": ig.KIND_LABEL[i.kind], "name": i.name,
            "enabled": i.enabled, "trigger": i.trigger, "run_types": i.run_types or [], "config": ig.public_config(i),
            "secret_hint": i.secret_hint, "has_secret": bool(i.secret_enc), "status": i.status, "last_error": i.last_error,
            "last_sync_at": i.last_sync_at, "active_since": i.active_since, "created_at": i.created_at,
            "sync_counts": counts, "issue_count": issues}


def _apply(db: Session, i: Integration, body: IntegrationIn, me: User, creating: bool) -> None:
    types = [t for t in dict.fromkeys(body.run_types) if t in ig.RUN_TYPES]
    if not types:
        raise HTTPException(422, "Choose at least one run type (API, browser or manual)")
    if not creating and body.kind != i.kind:
        raise HTTPException(422, "The connector type cannot be changed; create a new connector")
    try:
        i.config = ig.clean_config(body.kind, body.config)
        if any(str(v).strip() for v in body.secrets.values()) or creating:
            if not access.has(db, me, i.project_id, "secret.write"):
                raise HTTPException(403, "You do not have permission to store tokens in this project")
            ig.set_secrets(i, body.secrets, keep_existing=not creating)
    except ig.ConnectorError as e:
        raise HTTPException(422, str(e))
    except SecretsUnavailable as e:
        raise HTTPException(503, str(e))
    if body.enabled and not i.enabled:
        i.active_since = utcnow()  # do not back-fill runs that finished while the connector was off
    i.name, i.enabled, i.trigger, i.run_types = body.name.strip(), body.enabled, body.trigger, types
    i.status = "unverified"


@router.get("/projects/{pid}/integrations")
def list_integrations(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    rows = db.scalars(select(Integration).where(Integration.tenant_id == me.tenant_id, Integration.project_id == pid)
                      .order_by(Integration.name)).all()
    return [out(db, i) for i in rows]


@router.post("/projects/{pid}/integrations", status_code=201)
def create_integration(pid: int, body: IntegrationIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _project(db, pid, me, "project.update")
    i = Integration(tenant_id=me.tenant_id, project_id=p.id, kind=body.kind, name=body.name, created_by=me.id,
                    active_since=utcnow(), enabled=False)
    _apply(db, i, body, me, creating=True)
    i.active_since = utcnow()
    db.add(i)
    db.flush()
    audit(db, request, me, "integration_created", i.name, project_id=p.id, integration_id=i.id, kind=i.kind, trigger=i.trigger)
    db.commit()
    return out(db, i)


@router.put("/integrations/{iid}")
def update_integration(iid: int, body: IntegrationIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    i = _integ(db, iid, me, "project.update")
    _apply(db, i, body, me, creating=False)
    audit(db, request, me, "integration_updated", i.name, integration_id=i.id, enabled=i.enabled, trigger=i.trigger)
    db.commit()
    return out(db, i)


@router.delete("/integrations/{iid}")
def delete_integration(iid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    i = _integ(db, iid, me, "project.update")
    audit(db, request, me, "integration_deleted", i.name, integration_id=i.id, kind=i.kind)
    db.delete(i)
    db.commit()
    return {"ok": True}


@router.post("/integrations/{iid}/test")
def test_integration(iid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    i = _integ(db, iid, me, "project.update")
    try:
        msg = ig.test_connection(i)
        i.status, i.last_error = "ok", ""
        ok = True
    except (ig.ConnectorError, SecretsUnavailable, ValueError) as e:
        msg, ok = str(e), False
        i.status, i.last_error = "error", msg[:1000]
    audit(db, request, me, "integration_tested", i.name, integration_id=i.id, ok=ok)
    db.commit()
    return {"ok": ok, "message": msg}


@router.get("/integrations/{iid}/syncs")
def list_syncs(iid: int, limit: int = 50, db: Session = Depends(get_db), me: User = Depends(member)):
    i = _integ(db, iid, me)
    rows = db.scalars(select(IntegrationSync).where(IntegrationSync.integration_id == i.id)
                      .order_by(IntegrationSync.id.desc()).limit(max(1, min(limit, 200)))).all()
    return [ig.sync_out(s) for s in rows]


@router.get("/integrations/{iid}/issues")
def list_issues(iid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    i = _integ(db, iid, me)
    rows = db.scalars(select(IntegrationIssue).where(IntegrationIssue.integration_id == i.id)
                      .order_by(IntegrationIssue.updated_at.desc()).limit(500)).all()
    return [{"case_key": r.case_key, "title": r.title, "external_id": r.external_id, "external_url": r.external_url,
             "state": r.state, "updated_at": r.updated_at} for r in rows]


@router.post("/integrations/{iid}/syncs/{sid}/retry")
def retry_sync(iid: int, sid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    i = _integ(db, iid, me, "run.execute")
    s = db.get(IntegrationSync, sid)
    if not s or s.integration_id != i.id:
        raise HTTPException(404, "Push not found")
    ig.enqueue(db, i, s.source_kind, s.source_id, me.id, force=True)
    db.commit()
    return ig.sync_out(s)


@router.get("/integrations/for-source")
def for_source(source_type: str, source_id: int, db: Session = Depends(get_db), me: User = Depends(member)):
    """Connectors that can receive this run, with the state of its push to each."""
    kind = ig.SOURCE_OF_TYPE.get(source_type)
    tp = ig.source_project(db, kind, source_id) if kind else None
    if not tp or tp[0] != me.tenant_id:
        raise HTTPException(404, "Not found")
    _project(db, tp[1], me)
    res = []
    for i in db.scalars(select(Integration).where(Integration.tenant_id == me.tenant_id, Integration.project_id == tp[1],
                                                  Integration.enabled.is_(True))).all():
        if source_type not in (i.run_types or []):
            continue
        s = db.scalars(select(IntegrationSync).where(IntegrationSync.integration_id == i.id, IntegrationSync.source_kind == kind,
                                                     IntegrationSync.source_id == source_id)).first()
        res.append({"id": i.id, "name": i.name, "kind": i.kind, "kind_label": ig.KIND_LABEL[i.kind], "trigger": i.trigger,
                    "sync": ig.sync_out(s) if s else None})
    return res


@router.post("/integrations/push")
def push(body: PushIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    kind = ig.SOURCE_OF_TYPE[body.source_type]
    tp = ig.source_project(db, kind, body.source_id)
    if not tp or tp[0] != me.tenant_id:
        raise HTTPException(404, "Not found")
    _project(db, tp[1], me, "run.execute")
    q = select(Integration).where(Integration.tenant_id == me.tenant_id, Integration.project_id == tp[1], Integration.enabled.is_(True))
    if body.integration_ids:
        q = q.where(Integration.id.in_(body.integration_ids))
    targets = [i for i in db.scalars(q).all() if body.source_type in (i.run_types or [])]
    if not targets:
        raise HTTPException(409, "No enabled connector in this project accepts this kind of run")
    rows = [ig.enqueue(db, i, kind, body.source_id, me.id, force=True) for i in targets]
    audit(db, request, me, "integration_push", f"{body.source_type} {body.source_id}", project_id=tp[1],
          integrations=[i.id for i in targets])
    db.commit()
    return [ig.sync_out(s) for s in rows]
