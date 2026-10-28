"""API uptime monitoring endpoints (release 1.6)."""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import access, apireport, apitesting, monitoring
from ..db import get_db
from ..models import ALERT_CHANNELS, ApiMonitor, ApiSuite, MonitorCheck, Project, User, utcnow
from ..security import SecretsUnavailable
from ..urlguard import TargetRejected, parse_target
from .deps import audit, member

router = APIRouter(tags=["monitoring"])
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,100}$")


class MonitorIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2048)
    method: str = Field(default="GET", pattern="^(GET|HEAD)$")
    suite_id: int | None = None
    use_suite_auth: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    expected_status: list[int | str] = Field(default_factory=lambda: ["2xx"], min_length=1, max_length=10)
    body_contains: str = Field(default="", max_length=500)
    max_response_ms: int = Field(default=0, ge=0, le=120_000)
    timeout_ms: int = Field(default=10_000, ge=1000, le=60_000)
    interval_seconds: int = 300
    failure_threshold: int = Field(default=2, ge=1, le=10)
    enabled: bool = True
    alert_channels: list[str] = Field(default_factory=lambda: ["inapp"], max_length=3)
    alert_emails: list[str] = Field(default_factory=list, max_length=10)
    webhook_kind: str = Field(default="slack", pattern="^(slack|teams|generic)$")
    webhook_url: str | None = Field(default=None, max_length=2048)  # None = keep the stored one, "" = remove


def _project(db: Session, pid: int, me: User, perm: str = "project.read") -> Project:
    return access.require(db, me, db.get(Project, pid), perm)


def _monitor(db: Session, mid: int, me: User, perm: str = "project.read") -> ApiMonitor:
    m = db.get(ApiMonitor, mid)
    if not m or m.tenant_id != me.tenant_id:
        raise HTTPException(404, "Monitor not found")
    try:
        access.require(db, me, db.get(Project, m.project_id), perm)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "Monitor not found")
        raise
    return m


def _apply(db: Session, m: ApiMonitor, body: MonitorIn, project: Project) -> None:
    try:
        _, host, _ = parse_target(body.url.strip())
    except TargetRejected as e:
        raise HTTPException(422, f"URL: {e}")
    if body.interval_seconds not in monitoring.INTERVALS:
        raise HTTPException(422, "The check interval must be 1, 2, 5, 10, 15, 30 or 60 minutes")
    for k in body.headers:
        if not HEADER_NAME.match(k):
            raise HTTPException(422, f"Invalid header name '{k}'")
        if k.lower() in monitoring.SENSITIVE_HEADERS:
            raise HTTPException(422, f"'{k}' looks like a credential: link the monitor to an API suite and use its credentials instead")
    for st in body.expected_status:
        if not ((isinstance(st, int) and 100 <= st <= 599) or (isinstance(st, str) and re.match(r"^[1-5](xx|\d\d)$", st.lower()))):
            raise HTTPException(422, "Expected status must be codes like 200 or classes like 2xx")
    chans = [c for c in dict.fromkeys(body.alert_channels) if c in ALERT_CHANNELS]
    if len(chans) != len(set(body.alert_channels)):
        raise HTTPException(422, "Alert channels must be email, webhook and/or inapp")
    emails = [e.strip().lower() for e in body.alert_emails if e.strip()]
    if any(not EMAIL_RE.match(e) for e in emails):
        raise HTTPException(422, "One of the alert e-mail addresses is not valid")
    if "email" in chans and not emails:
        raise HTTPException(422, "Add at least one e-mail address for e-mail alerts")
    suite = None
    if body.suite_id:
        suite = db.get(ApiSuite, body.suite_id)
        if not suite or suite.archived or suite.tenant_id != m.tenant_id or suite.project_id != project.id:
            raise HTTPException(422, "The linked API suite is not part of this project")
    if body.use_suite_auth:
        if not suite or suite.auth_type == "none":
            raise HTTPException(422, "Choose an API suite with credentials to monitor an authenticated endpoint")
        if not suite.base_url or parse_target(suite.base_url)[1] != host:
            raise HTTPException(422, "Credentials are only sent to the suite's own host: the monitor URL must be on "
                                     f"{parse_target(suite.base_url)[1] if suite.base_url else 'the suite host'}")
    if body.webhook_url is not None:
        try:
            monitoring.set_webhook(m, body.webhook_url.strip())
        except TargetRejected as e:
            raise HTTPException(422, f"Webhook URL: {e}")
        except SecretsUnavailable as e:
            raise HTTPException(503, str(e))
    if "webhook" in chans and not m.webhook_url_enc:
        raise HTTPException(422, "Add a webhook URL for webhook alerts")
    m.name, m.url, m.method = body.name.strip(), body.url.strip(), body.method
    m.suite_id, m.use_suite_auth = (suite.id if suite else None), body.use_suite_auth
    m.headers = {k: v[:500] for k, v in body.headers.items()}
    m.expected_status = [s.lower() if isinstance(s, str) else s for s in body.expected_status]
    m.body_contains, m.max_response_ms, m.timeout_ms = body.body_contains, body.max_response_ms, body.timeout_ms
    m.interval_seconds, m.failure_threshold = body.interval_seconds, body.failure_threshold
    m.alert_channels, m.alert_emails, m.webhook_kind = chans, emails, body.webhook_kind
    if body.enabled and not m.enabled:
        m.next_check_at = utcnow()
    if not body.enabled:
        m.status = "paused"
    elif m.status == "paused":
        m.status = "unknown"
    m.enabled = body.enabled


def monitor_out(db: Session, m: ApiMonitor, *, detail: bool = False) -> dict:
    suite = db.get(ApiSuite, m.suite_id) if m.suite_id else None
    pol = monitoring.monitor_policy(db, m, suite)
    now = utcnow()
    out = {
        "id": m.id, "project_id": m.project_id, "suite_id": m.suite_id, "suite_name": suite.name if suite else None,
        "name": m.name, "url": m.url, "method": m.method, "headers": m.headers or {}, "use_suite_auth": m.use_suite_auth,
        "expected_status": m.expected_status, "body_contains": m.body_contains, "max_response_ms": m.max_response_ms,
        "timeout_ms": m.timeout_ms, "interval_seconds": m.interval_seconds, "failure_threshold": m.failure_threshold,
        "enabled": m.enabled, "alert_channels": m.alert_channels or [], "alert_emails": m.alert_emails or [],
        "webhook_kind": m.webhook_kind, "webhook_hint": m.webhook_hint, "has_webhook": bool(m.webhook_url_enc),
        "status": m.status, "consecutive_failures": m.consecutive_failures, "failing_since": m.failing_since,
        "last_check_at": m.last_check_at, "next_check_at": m.next_check_at, "last_ok_at": m.last_ok_at,
        "last_status_change_at": m.last_status_change_at, "last_error": m.last_error,
        "policy_problems": pol.problems, "created_at": m.created_at,
        "uptime_24h": monitoring.stats(db, m, now - timedelta(hours=24), now)["uptime_pct"],
    }
    if detail:
        out["stats"] = {k: monitoring.stats(db, m, now - d, now) for k, d in
                        (("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30)))}
        out["series"] = monitoring.series(db, m, 24)
        out["incidents"] = monitoring.incidents(db, m, limit=25)
        out["recent_checks"] = [{"checked_at": c.checked_at, "ok": c.ok, "http_status": c.http_status,
                                 "duration_ms": c.duration_ms, "error": c.error}
                                for c in db.scalars(select(MonitorCheck).where(MonitorCheck.monitor_id == m.id)
                                                    .order_by(MonitorCheck.checked_at.desc()).limit(25)).all()]
        out["my_permissions"] = []
    return out


@router.get("/projects/{pid}/monitors")
def list_monitors(pid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    _project(db, pid, me)
    rows = db.scalars(select(ApiMonitor).where(ApiMonitor.tenant_id == me.tenant_id, ApiMonitor.project_id == pid)
                      .order_by(ApiMonitor.name)).all()
    return [monitor_out(db, m) for m in rows]


@router.post("/projects/{pid}/monitors", status_code=201)
def create_monitor(pid: int, body: MonitorIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    p = _project(db, pid, me, "project.update")
    m = ApiMonitor(tenant_id=me.tenant_id, project_id=p.id, name=body.name, url=body.url, created_by=me.id,
                   next_check_at=utcnow(), status="unknown", enabled=False)
    _apply(db, m, body, p)
    if m.enabled:
        m.next_check_at = utcnow()
    db.add(m)
    db.flush()
    audit(db, request, me, "monitor_created", m.name, project_id=p.id, monitor_id=m.id, channels=m.alert_channels,
          interval=m.interval_seconds)
    db.commit()
    return monitor_out(db, m)


@router.get("/monitors/{mid}")
def get_monitor(mid: int, db: Session = Depends(get_db), me: User = Depends(member)):
    m = _monitor(db, mid, me)
    out = monitor_out(db, m, detail=True)
    out["my_permissions"] = sorted(access.perms_for(db, me, m.project_id))
    return out


@router.put("/monitors/{mid}")
def update_monitor(mid: int, body: MonitorIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    m = _monitor(db, mid, me, "project.update")
    _apply(db, m, body, db.get(Project, m.project_id))
    audit(db, request, me, "monitor_updated", m.name, monitor_id=m.id, enabled=m.enabled, channels=m.alert_channels)
    db.commit()
    return monitor_out(db, m)


@router.delete("/monitors/{mid}")
def delete_monitor(mid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    m = _monitor(db, mid, me, "project.update")
    audit(db, request, me, "monitor_deleted", m.name, monitor_id=m.id)
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/monitors/{mid}/check-now")
def check_now(mid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    m = _monitor(db, mid, me, "run.execute")
    if not m.enabled:
        raise HTTPException(409, "The monitor is paused")
    m.next_check_at = utcnow()
    db.commit()
    return {"ok": True, "message": "A check will run within a few seconds"}


@router.post("/monitors/{mid}/test-alert")
def test_alert(mid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(member)):
    m = _monitor(db, mid, me, "project.update")
    rows = monitoring.deliver(db, m, "test", None)
    audit(db, request, me, "monitor_test_alert", m.name, monitor_id=m.id, results=[{"channel": r["channel"], "ok": r["ok"]} for r in rows])
    db.commit()
    return {"results": rows}


@router.get("/monitors/{mid}/report")
def monitor_report(mid: int, days: int = 30, format: str = "html", download: bool = True,
                   db: Session = Depends(get_db), me: User = Depends(member)):
    m = _monitor(db, mid, me, "report.read")
    days = max(1, min(days, monitoring.RETENTION_DAYS))
    data = monitoring.report_data(db, m, days)
    if format == "json":
        body, mime, ext = apireport.to_json(data), "application/json", "json"
    else:
        body, mime, ext = apireport.monitor_html(data), "text/html; charset=utf-8", "html"
    name = f"uptime-report-{m.id}-{days}d.{ext}"
    return Response(body, media_type=mime, headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"',
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------------------------- organization-wide views
def _visible(db: Session, me: User):
    q = select(ApiMonitor).join(Project, Project.id == ApiMonitor.project_id).where(
        ApiMonitor.tenant_id == me.tenant_id, Project.archived.is_(False))
    ids = access.accessible_project_ids(db, me)
    if ids is not None:
        q = q.where(ApiMonitor.project_id.in_(ids or [-1]))
    return q


@router.get("/monitoring/overview")
def overview(db: Session = Depends(get_db), me: User = Depends(member)):
    now = utcnow()
    out = []
    for m in db.scalars(_visible(db, me).order_by(ApiMonitor.name)).all():
        d = monitor_out(db, m)
        d["project_name"] = db.get(Project, m.project_id).name
        d["uptime_30d"] = monitoring.stats(db, m, now - timedelta(days=30), now)["uptime_pct"]
        out.append(d)
    return out


@router.get("/monitoring/alerts")
def in_app_alerts(db: Session = Depends(get_db), me: User = Depends(member)):
    rows = db.scalars(_visible(db, me).where(ApiMonitor.status == "down", ApiMonitor.enabled.is_(True))).all()
    return [{"id": m.id, "name": m.name, "project_id": m.project_id, "failing_since": m.failing_since,
             "last_error": m.last_error} for m in rows if "inapp" in (m.alert_channels or [])]
