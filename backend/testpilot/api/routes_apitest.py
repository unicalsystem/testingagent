"""API testing endpoints (release 1.6): suites, generated cases, runs and reports."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import access, ai, apispec, apitesting, storage_policy
from ..db import get_db
from ..models import ApiCase, ApiRun, ApiSuite, Project, User, utcnow
from ..urlguard import TargetRejected, parse_target
from .deps import audit, member

router = APIRouter(tags=["api-testing"])
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,100}$")


class AuthIn(BaseModel):
    type: str = Field(pattern="^(none|api_key|bearer|basic|oauth2)$")
    config: dict = Field(default_factory=dict)
    secrets: dict = Field(default_factory=dict)


class SuiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(default="", max_length=2048)
    spec_source: str = Field(default="manual", pattern="^(url|upload|manual)$")
    spec_url: str = Field(default="", max_length=2048)
    spec_text: str | None = Field(default=None, max_length=apispec.MAX_SPEC_BYTES)
    endpoints_text: str = Field(default="", max_length=100_000)
    requires_auth: bool = True
    allow_writes: bool = False
    verify_tls: bool = True
    timeout_ms: int = Field(default=15000, ge=1000, le=120_000)
    max_response_ms: int = Field(default=2000, ge=50, le=120_000)
    default_headers: dict[str, str] = Field(default_factory=dict)


class SuiteCreate(SuiteIn):
    auth: AuthIn | None = None
    generate: bool = True
    run: bool = False


class GenerateIn(BaseModel):
    run: bool = False


class CaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    category: str = Field(default="positive", pattern="^(positive|auth|negative|performance)$")
    definition: dict


class CasePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, pattern="^(positive|auth|negative|performance)$")
    definition: dict | None = None
    enabled: bool | None = None


class BulkCasesIn(BaseModel):
    cases: list[CaseIn] = Field(min_length=1, max_length=50)
    source: str = Field(default="ai", pattern="^(ai|manual)$")


class SuggestIn(BaseModel):
    instructions: str = Field(default="", max_length=4000)
    count: int = Field(default=8, ge=1, le=25)


class RunIn(BaseModel):
    include_writes: bool = False


class EnableIn(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=5000)
    enabled: bool


# --------------------------------------------------------------------------- helpers
def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    return access.require(db, me, db.get(Project, pid), perm)


def _suite(db: Session, sid: int, me: User, perm: str = "project.read") -> ApiSuite:
    s = db.get(ApiSuite, sid)
    if not s or s.archived or s.tenant_id != me.tenant_id:
        raise HTTPException(404, "API suite not found")
    try:
        access.require(db, me, db.get(Project, s.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "API suite not found")
        raise
    return s


def _run(db: Session, rid: int, me: User, perm: str = "project.read") -> ApiRun:
    r = db.get(ApiRun, rid)
    if not r or r.tenant_id != me.tenant_id:
        raise HTTPException(404, "API run not found")
    try:
        access.require(db, me, db.get(Project, r.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "API run not found")
        raise
    return r


def _validate(body: SuiteIn) -> None:
    if body.base_url:
        try:
            parse_target(body.base_url.strip())
        except TargetRejected as e:
            raise HTTPException(422, f"Base URL: {e}")
    elif body.spec_source == "manual":
        raise HTTPException(422, "The API base URL is required")
    if body.spec_source == "url":
        if not body.spec_url:
            raise HTTPException(422, "The OpenAPI / Swagger URL is required")
        try:
            parse_target(body.spec_url.strip())
        except TargetRejected as e:
            raise HTTPException(422, f"Specification URL: {e}")
    if body.spec_source == "manual":
        try:
            apispec.parse_endpoints(body.endpoints_text)
        except apispec.SpecError as e:
            raise HTTPException(422, f"Endpoints: {e}")
    if len(body.default_headers) > 20:
        raise HTTPException(422, "At most 20 default headers")
    for k in body.default_headers:
        if not HEADER_NAME.match(k):
            raise HTTPException(422, f"Invalid header name '{k}'")
        if k.lower() in ("authorization", "cookie", "x-api-key", "host", "content-length"):
            raise HTTPException(422, f"Set '{k}' through the suite's credentials, not as a default header")


def _apply(s: ApiSuite, body: SuiteIn) -> dict | None:
    """Copies fields; returns the parsed spec document when an uploaded one was given."""
    doc = None
    s.name, s.spec_source = body.name.strip(), body.spec_source
    s.spec_url = body.spec_url.strip() if body.spec_source == "url" else ""
    s.endpoints_text = body.endpoints_text if body.spec_source == "manual" else ""
    if body.spec_source == "upload":
        if body.spec_text is not None:
            try:
                doc = apispec.parse_spec_text(body.spec_text)
            except apispec.SpecError as e:
                raise HTTPException(422, f"Specification: {e}")
            s.spec_text, s.spec_title = body.spec_text, apispec.spec_title(doc)[:300]
        elif not s.spec_text:
            raise HTTPException(422, "Upload or paste the OpenAPI / Swagger document")
    elif body.spec_source == "manual":
        s.spec_text, s.spec_title = "", ""
    base = body.base_url.strip()
    if not base and doc is not None:
        base = apispec.spec_base_url(doc)
        if not base:
            raise HTTPException(422, "The document does not name an absolute server URL; enter the API base URL")
        try:
            parse_target(base)
        except TargetRejected as e:
            raise HTTPException(422, f"Base URL from the document: {e}")
    s.base_url = base.rstrip("/") if base else s.base_url or ""
    s.requires_auth, s.allow_writes, s.verify_tls = body.requires_auth, body.allow_writes, body.verify_tls
    s.timeout_ms, s.max_response_ms = body.timeout_ms, body.max_response_ms
    s.default_headers = {k: str(v)[:500] for k, v in body.default_headers.items()}
    return doc


def suite_out(db: Session, s: ApiSuite, me: User | None = None) -> dict:
    counts = dict(db.execute(select(ApiCase.enabled, func.count()).where(ApiCase.suite_id == s.id).group_by(ApiCase.enabled)).all())
    writes = db.scalar(select(func.count()).select_from(ApiCase).where(ApiCase.suite_id == s.id, ApiCase.is_write.is_(True))) or 0
    last = db.scalars(select(ApiRun).where(ApiRun.suite_id == s.id).order_by(ApiRun.id.desc()).limit(1)).first()
    pol = apitesting.suite_policy(db, s, include_spec=s.spec_source == "url") if s.base_url or s.spec_url else None
    return {
        "id": s.id, "project_id": s.project_id, "name": s.name, "base_url": s.base_url, "spec_source": s.spec_source,
        "spec_url": s.spec_url, "spec_title": s.spec_title, "has_spec": bool(s.spec_text), "endpoints_text": s.endpoints_text,
        "auth_type": s.auth_type, "auth_config": s.auth_config or {}, "auth_hint": s.auth_hint,
        "has_credentials": bool(s.auth_secret_enc), "requires_auth": s.requires_auth, "allow_writes": s.allow_writes,
        "verify_tls": s.verify_tls, "timeout_ms": s.timeout_ms, "max_response_ms": s.max_response_ms,
        "default_headers": s.default_headers or {}, "import_state": s.import_state, "import_error": s.import_error,
        "case_count": sum(counts.values()), "enabled_count": counts.get(True, 0), "write_count": writes,
        "policy_problems": pol.problems if pol else [],
        "run_block_reason": apitesting.run_block_reason(db, s) if s.base_url else "The API base URL is not known yet",
        "last_run": run_ref(last) if last else None,
        "my_permissions": sorted(access.perms_for(db, me, s.project_id)) if me else [],
        "created_at": s.created_at, "updated_at": s.updated_at,
    }


def run_ref(r: ApiRun) -> dict:
    return {"id": r.id, "status": r.status, "created_at": r.created_at, "finished_at": r.finished_at,
            "summary": r.summary or {}}


def run_out(db: Session, r: ApiRun, with_results: bool = True) -> dict:
    who = db.get(User, r.requested_by) if r.requested_by else None
    out = {**run_ref(r), "suite_id": r.suite_id, "project_id": r.project_id, "error": r.error, "options": r.options,
           "suite": r.suite_snapshot, "cancel_requested": r.cancel_requested, "started_at": r.started_at,
           "requested_by": (who.display_name or who.username) if who else None, "report_sha256": r.report_sha256}
    if with_results:
        out["results"] = [{"id": x.id, "idx": x.idx, "case_id": x.case_id, "name": x.name, "category": x.category,
                           "method": x.method, "path": x.path, "url": x.url, "status": x.status,
                           "http_status": x.http_status, "duration_ms": x.duration_ms, "assertions": x.assertions,
                           "request": x.request, "response": x.response, "error": x.error} for x in r.results]
    return out


def _start_generation(db: Session, s: ApiSuite, me: User, doc: dict | None, run: bool) -> dict:
    """Generates now (uploaded spec / endpoint list) or queues the worker import (spec URL)."""
    if s.spec_source == "url":
        s.import_state, s.import_error = "pending", ""
        s.run_after_import, s.import_requested_by = run, me.id
        return {"generated": None, "import": "pending", "run": None}
    try:
        n = apitesting.generate(db, s, doc)
    except apispec.SpecError as e:
        raise HTTPException(422, str(e))
    s.import_state, s.import_error = "done", ""
    db.flush()
    run_row = None
    if run:
        try:
            run_row = apitesting.queue_run(db, s, me)
        except apitesting.ApiTestError as e:
            return {"generated": n, "import": "done", "run": None, "run_error": str(e)}
    return {"generated": n, "import": "done", "run": run_row.id if run_row else None}


# --------------------------------------------------------------------------- suites
@router.get("/projects/{pid}/api-suites")
def list_suites(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    rows = db.scalars(select(ApiSuite).where(ApiSuite.tenant_id == me.tenant_id, ApiSuite.project_id == pid,
                                             ApiSuite.archived.is_(False)).order_by(ApiSuite.name)).all()
    return [suite_out(db, s) for s in rows]


@router.post("/projects/{pid}/api-suites", status_code=201)
def create_suite(pid: int, body: SuiteCreate, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _project(db, pid, me, "case.write")
    _validate(body)
    if body.auth and body.auth.type != "none" and not access.has(db, me, p.id, "secret.write"):
        raise HTTPException(403, "You do not have permission to store credentials in this project")
    if body.run and not access.has(db, me, p.id, "run.execute"):
        raise HTTPException(403, "You do not have permission to run tests in this project")
    s = ApiSuite(tenant_id=me.tenant_id, project_id=p.id, name=body.name, base_url="", created_by=me.id)
    doc = _apply(s, body)
    if body.auth:
        try:
            apitesting.set_credentials(s, body.auth.type, body.auth.config, body.auth.secrets, keep_existing=False)
        except apitesting.ApiTestError as e:
            raise HTTPException(422, str(e))
    db.add(s)
    db.flush()
    out = {"generated": None}
    if body.generate:
        out = _start_generation(db, s, me, doc, body.run)
    audit(db, request, me, "api_suite_created", s.name, project_id=p.id, suite_id=s.id, source=s.spec_source,
          auth=s.auth_type, generated=out.get("generated"))
    db.commit()
    return {**suite_out(db, s, me), "generation": out}


@router.get("/api-suites/{sid}")
def get_suite(sid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    return suite_out(db, _suite(db, sid, me), me)


@router.put("/api-suites/{sid}")
def update_suite(sid: int, body: SuiteIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "case.write")
    _validate(body)
    old_hosts = apitesting.suite_hosts(s)[0] if s.base_url else set()
    _apply(s, body)
    new_hosts = apitesting.suite_hosts(s)[0] if s.base_url else set()
    if s.auth_secret_enc and old_hosts and new_hosts != old_hosts and not access.has(db, me, s.project_id, "project.retarget_with_secrets"):
        raise HTTPException(403, "This suite holds credentials: only an administrator can point it at a different host")
    audit(db, request, me, "api_suite_updated", s.name, suite_id=s.id, allow_writes=s.allow_writes)
    db.commit()
    return suite_out(db, s, me)


@router.put("/api-suites/{sid}/credentials")
def set_credentials(sid: int, body: AuthIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "secret.write")
    try:
        apitesting.set_credentials(s, body.type, body.config, body.secrets)
    except apitesting.ApiTestError as e:
        raise HTTPException(422, str(e))
    audit(db, request, me, "api_suite_credentials_set", s.name, suite_id=s.id, auth=s.auth_type)
    db.commit()
    return suite_out(db, s, me)


@router.delete("/api-suites/{sid}")
def archive_suite(sid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "case.write")
    s.archived = True
    audit(db, request, me, "api_suite_archived", s.name, suite_id=s.id)
    db.commit()
    return {"ok": True}


@router.post("/api-suites/{sid}/generate")
def regenerate(sid: int, body: GenerateIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "case.write")
    if body.run and not access.has(db, me, s.project_id, "run.execute"):
        raise HTTPException(403, "You do not have permission to run tests in this project")
    if s.import_state in ("pending", "running"):
        raise HTTPException(409, "The specification is being imported; wait for it to finish")
    out = _start_generation(db, s, me, None, body.run)
    audit(db, request, me, "api_cases_generated", s.name, suite_id=s.id, generated=out.get("generated"))
    db.commit()
    return {**suite_out(db, s, me), "generation": out}


# --------------------------------------------------------------------------- cases
@router.get("/api-suites/{sid}/cases")
def list_cases(sid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me)
    rows = db.scalars(select(ApiCase).where(ApiCase.tenant_id == me.tenant_id, ApiCase.suite_id == s.id)
                      .order_by(ApiCase.position, ApiCase.id)).all()
    return [apitesting.case_out(c) for c in rows]


def _new_case(db: Session, s: ApiSuite, c: CaseIn, source: str, pos: int) -> ApiCase:
    try:
        d = apispec.clean_definition(c.definition)
    except apispec.SpecError as e:
        raise HTTPException(422, f"{c.name}: {e}")
    row = ApiCase(tenant_id=s.tenant_id, suite_id=s.id, position=pos, name=c.name, category=c.category,
                  method=d["method"], path=d["path"], definition=d, source=source, is_write=d["method"] in apispec.WRITE_METHODS)
    db.add(row)
    return row


def _next_pos(db: Session, sid: int) -> int:
    return (db.scalar(select(func.max(ApiCase.position)).where(ApiCase.suite_id == sid)) or 0) + 1


@router.post("/api-suites/{sid}/cases", status_code=201)
def create_case(sid: int, body: CaseIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "case.write")
    c = _new_case(db, s, body, "manual", _next_pos(db, s.id))
    db.flush()
    audit(db, request, me, "api_case_created", c.name, suite_id=s.id, case_id=c.id)
    db.commit()
    return apitesting.case_out(c)


@router.post("/api-suites/{sid}/cases/bulk", status_code=201)
def create_cases(sid: int, body: BulkCasesIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "case.write")
    pos = _next_pos(db, s.id)
    rows = [_new_case(db, s, c, body.source, pos + i) for i, c in enumerate(body.cases)]
    db.flush()
    audit(db, request, me, "api_cases_created", s.name, suite_id=s.id, count=len(rows), source=body.source)
    db.commit()
    return [apitesting.case_out(c) for c in rows]


def _case(db: Session, cid: int, me: User, perm: str) -> ApiCase:
    c = db.get(ApiCase, cid)
    if not c or c.tenant_id != me.tenant_id:
        raise HTTPException(404, "API test case not found")
    _suite(db, c.suite_id, me, perm)
    return c


@router.put("/api-cases/{cid}")
def update_case(cid: int, body: CasePatch, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    c = _case(db, cid, me, "case.write")
    if body.definition is not None:
        try:
            d = apispec.clean_definition(body.definition)
        except apispec.SpecError as e:
            raise HTTPException(422, str(e))
        c.definition, c.method, c.path, c.is_write = d, d["method"], d["path"], d["method"] in apispec.WRITE_METHODS
        if c.source in ("spec", "auto"):
            c.source = "manual"  # edited cases survive regeneration
    if body.name is not None:
        c.name = body.name
    if body.category is not None:
        c.category = body.category
    if body.enabled is not None:
        c.enabled = body.enabled
    audit(db, request, me, "api_case_updated", c.name, case_id=c.id)
    db.commit()
    return apitesting.case_out(c)


@router.post("/api-suites/{sid}/cases/enable")
def enable_cases(sid: int, body: EnableIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "case.write")
    rows = db.scalars(select(ApiCase).where(ApiCase.tenant_id == me.tenant_id, ApiCase.suite_id == s.id,
                                            ApiCase.id.in_(body.case_ids))).all()
    for c in rows:
        c.enabled = body.enabled
    audit(db, request, me, "api_cases_enabled" if body.enabled else "api_cases_disabled", s.name, suite_id=s.id, count=len(rows))
    db.commit()
    return {"updated": len(rows)}


@router.delete("/api-cases/{cid}")
def delete_case(cid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    c = _case(db, cid, me, "case.write")
    audit(db, request, me, "api_case_deleted", c.name, case_id=c.id)
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/api-suites/{sid}/ai/suggest")
def ai_suggest(sid: int, body: SuggestIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "ai.use")
    if not storage_policy.row(db, me.tenant_id).ai_enabled:
        raise HTTPException(403, "AI features are turned off in your organization's data-residency settings")
    existing = db.scalars(select(ApiCase).where(ApiCase.suite_id == s.id).order_by(ApiCase.position)).all()
    try:
        out = apitesting.ai_suggest(s, existing, body.instructions, body.count)
    except ai.AIUnavailable as e:
        raise HTTPException(503, str(e))
    except apispec.SpecError as e:
        raise HTTPException(422, str(e))
    audit(db, request, me, "api_ai_suggest", s.name, suite_id=s.id, suggested=len(out["cases"]))
    db.commit()
    return out  # drafts only: the tester picks which to save


# --------------------------------------------------------------------------- runs
@router.post("/api-suites/{sid}/runs", status_code=201)
def start_run(sid: int, body: RunIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me, "run.execute")
    if s.import_state in ("pending", "running"):
        raise HTTPException(409, "The specification is being imported; the run starts automatically if you asked for it")
    if body.include_writes and not s.allow_writes:
        raise HTTPException(422, "Write tests are not allowed for this suite. Enable them in the suite settings first.")
    try:
        r = apitesting.queue_run(db, s, me, include_writes=body.include_writes)
    except apitesting.ApiTestError as e:
        raise HTTPException(409, str(e))
    audit(db, request, me, "api_run_queued", s.name, suite_id=s.id, run_id=r.id, include_writes=body.include_writes)
    db.commit()
    return run_out(db, r, with_results=False)


@router.get("/api-suites/{sid}/runs")
def list_runs(sid: int, limit: int = 50, db: Session = Depends(get_db), me: User = Depends(member)):
    s = _suite(db, sid, me)
    rows = db.scalars(select(ApiRun).where(ApiRun.tenant_id == me.tenant_id, ApiRun.suite_id == s.id)
                      .order_by(ApiRun.id.desc()).limit(max(1, min(limit, 200)))).all()
    return [run_out(db, r, with_results=False) for r in rows]


@router.get("/api-runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    return run_out(db, _run(db, rid, me))


@router.post("/api-runs/{rid}/cancel")
def cancel_run(rid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    r = _run(db, rid, me, "run.cancel")
    if r.status in apitesting.TERMINAL:
        raise HTTPException(409, "The run has already finished")
    r.cancel_requested = True
    if r.status == "queued":
        r.status, r.finished_at, r.error = "cancelled", utcnow(), "Cancelled before it started"
    audit(db, request, me, "api_run_cancelled", f"api run {r.id}", run_id=r.id)
    db.commit()
    return run_out(db, r, with_results=False)


@router.get("/api-runs/{rid}/report")
def run_report(rid: int, format: str = "html", download: bool = True, db: Session = Depends(get_db), me: User = Depends(member)):
    r = _run(db, rid, me, "report.read")
    if r.status not in apitesting.TERMINAL:
        raise HTTPException(409, "The report is available when the run has finished")
    if format == "json":
        body, mime, ext = apitesting.run_json(db, r), "application/json", "json"
    else:
        body, mime, ext = apitesting.run_html(db, r), "text/html; charset=utf-8", "html"
    name = f"api-test-report-{r.id}.{ext}"
    return Response(body, media_type=mime, headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"',
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff"})
