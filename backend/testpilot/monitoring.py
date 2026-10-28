"""API uptime monitoring (release 1.6).

The worker runs a scheduler thread that claims monitors whose next check is due, probes them through
the same per-check EgressProxy as test runs (allowlist + private-address rules), records every check,
and turns consecutive failures into incidents. Alerts go to the channels chosen per monitor:
  email   - to the listed addresses through the platform SMTP settings
  webhook - Slack, Microsoft Teams (Workflows / incoming webhook) or a generic JSON endpoint
  inapp   - the monitor appears in the in-app banner and dashboard while it is down
Downtime is the time from the first failed check of an incident to the first successful check after it.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, func, literal_column, or_, select
from sqlalchemy.orm import Session

from . import apiexec, apitesting, mailer
from .config import get_settings
from .db import session_scope
from .models import ApiMonitor, ApiSuite, MonitorCheck, MonitorIncident, Tenant, utcnow
from .security import SecretsUnavailable, decrypt_secret, encrypt_secret
from .urlguard import TargetRejected, check_ip, parse_target, resolve

log = logging.getLogger("testpilot.monitoring")
INTERVALS = (60, 120, 300, 600, 900, 1800, 3600)
MAX_PARALLEL = 8
RETENTION_DAYS = 90
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}
WEBHOOK_KINDS = ("slack", "teams", "generic")


# --------------------------------------------------------------------------- configuration helpers
def validate_webhook_url(url: str) -> None:
    """Webhooks go to the customer's chat/automation service: HTTPS on a public address only."""
    scheme, host, port = parse_target(url)
    if scheme != "https":
        raise TargetRejected("webhook URLs must use https")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = resolve(host, port)
    for ip in ips:
        check_ip(ip, private_ok=False)


def set_webhook(m: ApiMonitor, url: str) -> None:
    if not url:
        m.webhook_url_enc, m.webhook_hint = "", ""
        return
    validate_webhook_url(url)
    m.webhook_url_enc = encrypt_secret(url)
    host = parse_target(url)[1]
    m.webhook_hint = f"{host} · …{url[-6:]}"[:120]


def webhook_url(m: ApiMonitor) -> str:
    return decrypt_secret(m.webhook_url_enc) if m.webhook_url_enc else ""


def monitor_policy(db: Session, m: ApiMonitor, suite: ApiSuite | None):
    hosts, ports = set(), {80, 443}
    _, h, p = parse_target(m.url)
    hosts.add(h)
    ports.add(p)
    if suite and m.use_suite_auth and suite.auth_type == "oauth2" and (suite.auth_config or {}).get("token_url"):
        _, h2, p2 = parse_target(suite.auth_config["token_url"])
        hosts.add(h2)
        ports.add(p2)
    return apitesting.policy_for_hosts(db, m.tenant_id, hosts, ports)


def link(tenant_slug: str, monitor_id: int) -> str:
    return f"{get_settings().public_url}/{tenant_slug}/monitors/{monitor_id}"


# --------------------------------------------------------------------------- scheduler
def due(limit: int = 20) -> list[int]:
    now = utcnow()
    with session_scope() as db:
        rows = db.scalars(select(ApiMonitor).join(Tenant, Tenant.id == ApiMonitor.tenant_id)
                          .where(ApiMonitor.enabled.is_(True), Tenant.status == "active",
                                 or_(ApiMonitor.next_check_at.is_(None), ApiMonitor.next_check_at <= now))
                          .order_by(ApiMonitor.next_check_at.nullsfirst())
                          .with_for_update(of=ApiMonitor, skip_locked=True).limit(limit)).all()
        for m in rows:
            m.next_check_at = now + timedelta(seconds=max(60, m.interval_seconds))
        return [m.id for m in rows]


def probe(mid: int) -> dict | None:
    """Runs one check for a monitor (network call outside any DB transaction). None = not checked."""
    with session_scope() as db:
        m = db.get(ApiMonitor, mid)
        if not m or not m.enabled:
            return None
        suite = db.get(ApiSuite, m.suite_id) if m.suite_id else None
        pol = monitor_policy(db, m, suite)
        if pol.problems:
            m.status, m.last_error = "paused", "Not checked: " + "; ".join(pol.problems)
            m.last_check_at = utcnow()
            return None
        auth = None
        verify = suite.verify_tls if suite else True
        cfg = dict(url=m.url, method=m.method, headers=dict(m.headers or {}), expected=list(m.expected_status or ["2xx"]),
                   body_contains=m.body_contains, max_ms=m.max_response_ms, timeout_s=max(1, min(m.timeout_ms, 60_000)) / 1000)
        auth_args = None
        if suite and m.use_suite_auth and suite.auth_type != "none":
            try:
                auth_args = (suite.auth_type, dict(suite.auth_config or {}), apitesting.load_secrets(suite))
            except (SecretsUnavailable, ValueError) as e:
                return {"ok": False, "http_status": None, "duration_ms": 0, "error": f"Credentials cannot be read: {e}"}
    with apitesting.guarded_client(pol, verify_tls=verify) as client:
        if auth_args:
            auth = apiexec.Authenticator(*auth_args, client=client, timeout_s=cfg["timeout_s"])
        res = apiexec.check_once(client, cfg["url"], method=cfg["method"], headers=cfg["headers"], auth=auth,
                                 expected=cfg["expected"], body_contains=cfg["body_contains"], max_ms=cfg["max_ms"],
                                 timeout_s=cfg["timeout_s"])
        if auth:
            red = apiexec.Redactor(auth.secret_values())
            res["error"] = red.text(res.get("error", ""))
    return res


def record(mid: int, res: dict, now: datetime | None = None) -> str | None:
    """Stores a check result and updates the monitor state. Returns 'down' / 'recovered' when an alert
    must be sent (the caller sends it after the transaction)."""
    now = now or utcnow()
    event = None
    with session_scope() as db:
        m = db.scalars(select(ApiMonitor).where(ApiMonitor.id == mid).with_for_update()).first()
        if not m:
            return None
        db.add(MonitorCheck(tenant_id=m.tenant_id, monitor_id=m.id, checked_at=now, ok=bool(res["ok"]),
                            http_status=res.get("http_status"), duration_ms=int(res.get("duration_ms") or 0),
                            error=str(res.get("error", ""))[:1000]))
        m.last_check_at = now
        open_inc = db.scalars(select(MonitorIncident).where(MonitorIncident.monitor_id == m.id,
                                                            MonitorIncident.ended_at.is_(None))
                              .order_by(MonitorIncident.id.desc()).limit(1)).first()
        if res["ok"]:
            if open_inc:
                open_inc.ended_at = now
                event = "recovered"
            if m.status != "up":
                m.last_status_change_at = now
            m.status, m.consecutive_failures, m.failing_since, m.last_ok_at, m.last_error = "up", 0, None, now, ""
        else:
            if m.consecutive_failures == 0 or not m.failing_since:
                m.failing_since = now
            m.consecutive_failures += 1
            m.last_error = str(res.get("error", ""))[:2000]
            if open_inc:
                open_inc.failed_checks += 1
            elif m.consecutive_failures >= max(1, m.failure_threshold):
                db.add(MonitorIncident(tenant_id=m.tenant_id, monitor_id=m.id, started_at=m.failing_since or now,
                                       cause=m.last_error[:1000], failed_checks=m.consecutive_failures))
                m.status, m.last_status_change_at = "down", now
                event = "down"
            elif m.status in ("paused", "unknown") and m.consecutive_failures < m.failure_threshold:
                pass  # not enough evidence yet
    return event


def check(mid: int) -> None:
    try:
        res = probe(mid)
        if res is None:
            return
        event = record(mid, res)
        if event:
            send_alerts(mid, event)
    except Exception:
        log.exception("monitor %s check failed", mid)


def scheduler(stop: threading.Event) -> None:
    pool = ThreadPoolExecutor(MAX_PARALLEL, thread_name_prefix="monitor")
    log.info("API monitoring scheduler started")
    while not stop.wait(5):
        try:
            for mid in due():
                pool.submit(check, mid)
        except Exception:
            log.exception("monitor scheduler error")
    pool.shutdown(wait=False, cancel_futures=True)


def start(stop: threading.Event) -> threading.Thread:
    t = threading.Thread(target=scheduler, args=(stop,), name="monitor-scheduler", daemon=True)
    t.start()
    return t


def purge_old() -> int:
    with session_scope() as db:
        return db.execute(delete(MonitorCheck).where(MonitorCheck.checked_at < utcnow() - timedelta(days=RETENTION_DAYS))).rowcount or 0


# --------------------------------------------------------------------------- alerts
def _dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    mnt, sec = divmod(rem, 60)
    return (f"{h} h " if h else "") + (f"{mnt} min " if mnt or h else "") + f"{sec} s"


def _message(m: ApiMonitor, tenant: Tenant, event: str, inc: MonitorIncident | None) -> tuple[str, str, dict]:
    url = link(tenant.slug, m.id)
    if event == "down":
        title = f"DOWN: {m.name}"
        body = (f"TestAgent detected that the API monitor '{m.name}' is DOWN.\n\nEndpoint: {m.method} {m.url}\n"
                f"Failing since: {(inc.started_at if inc else utcnow()).strftime('%d %b %Y %H:%M UTC')}\n"
                f"Reason: {m.last_error or 'check failed'}\nConsecutive failed checks: {m.consecutive_failures}\n\n"
                f"Monitor: {url}\n")
    elif event == "recovered":
        dur = (inc.ended_at - inc.started_at).total_seconds() if inc and inc.ended_at else 0
        title = f"RECOVERED: {m.name} (down for {_dur(dur).strip()})"
        body = (f"The API monitor '{m.name}' is back UP.\n\nEndpoint: {m.method} {m.url}\n"
                f"Downtime: {_dur(dur).strip()}\nMonitor: {url}\n")
    else:
        title = f"Test alert: {m.name}"
        body = (f"This is a test alert from TestAgent for the API monitor '{m.name}' ({m.method} {m.url}).\n"
                f"If you received it, alerts for this monitor are configured correctly.\n\nMonitor: {url}\n")
    payload = {"event": f"monitor.{event}", "organization": tenant.slug,
               "monitor": {"id": m.id, "name": m.name, "url": m.url, "method": m.method, "status": m.status},
               "incident": ({"id": inc.id, "started_at": inc.started_at.isoformat(),
                             "ended_at": inc.ended_at.isoformat() if inc.ended_at else None, "cause": inc.cause}
                            if inc else None),
               "message": title, "link": url, "sent_at": utcnow().isoformat()}
    return title, body, payload


def _webhook_body(kind: str, event: str, title: str, body: str, payload: dict) -> dict:
    if kind == "slack":
        emoji = {"down": ":red_circle:", "recovered": ":large_green_circle:"}.get(event, ":bell:")
        return {"text": f"{emoji} *{title}*\n{body}"}
    if kind == "teams":
        color = {"down": "Attention", "recovered": "Good"}.get(event, "Accent")
        return {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                        "version": "1.4", "body": [
                            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "color": color, "wrap": True},
                            {"type": "TextBlock", "text": body, "wrap": True}],
                        "actions": [{"type": "Action.OpenUrl", "title": "Open in TestAgent", "url": payload["link"]}]}}]}
    return payload


def post_webhook(url: str, kind: str, event: str, title: str, body: str, payload: dict) -> None:
    validate_webhook_url(url)
    with httpx.Client(follow_redirects=False, timeout=10, trust_env=False) as c:
        r = c.post(url, json=_webhook_body(kind, event, title, body, payload),
                   headers={"User-Agent": apiexec.USER_AGENT + " alerts"})
    if r.status_code >= 300:
        raise RuntimeError(f"webhook returned HTTP {r.status_code}")


def deliver(db: Session, m: ApiMonitor, event: str, inc: MonitorIncident | None) -> list[dict]:
    tenant = db.get(Tenant, m.tenant_id)
    title, body, payload = _message(m, tenant, event, inc)
    log_rows: list[dict] = []
    chans = set(m.alert_channels or [])
    if "email" in chans:
        for to in (m.alert_emails or [])[:20]:
            try:
                mailer.send(db, to, f"[TestAgent] {title}", body)
                log_rows.append({"channel": "email", "to": to, "ok": True, "at": utcnow().isoformat()})
            except Exception as e:
                log_rows.append({"channel": "email", "to": to, "ok": False, "at": utcnow().isoformat(), "error": str(e)[:300]})
    if "webhook" in chans and m.webhook_url_enc:
        try:
            post_webhook(webhook_url(m), m.webhook_kind, event, title, body, payload)
            log_rows.append({"channel": "webhook", "ok": True, "at": utcnow().isoformat()})
        except Exception as e:
            log_rows.append({"channel": "webhook", "ok": False, "at": utcnow().isoformat(), "error": str(e)[:300]})
    if "inapp" in chans:
        log_rows.append({"channel": "inapp", "ok": True, "at": utcnow().isoformat()})
    return log_rows


def send_alerts(mid: int, event: str) -> None:
    with session_scope() as db:
        m = db.get(ApiMonitor, mid)
        inc = db.scalars(select(MonitorIncident).where(MonitorIncident.monitor_id == mid)
                         .order_by(MonitorIncident.id.desc()).limit(1)).first()
        rows = deliver(db, m, event, inc)
        if inc:
            inc.alerts = [*(inc.alerts or []), *({**r, "event": event} for r in rows)][-50:]
    bad = [r for r in rows if not r["ok"]]
    if bad:
        log.warning("monitor %s: %s alert(s) failed: %s", mid, len(bad), "; ".join(r.get("error", "") for r in bad)[:500])


# --------------------------------------------------------------------------- statistics & reports
def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    s, e = max(a0, b0), min(a1, b1)
    return max(0.0, (e - s).total_seconds())


def stats(db: Session, m: ApiMonitor, since: datetime, until: datetime | None = None) -> dict:
    until = until or utcnow()
    start = max(_aware(since), _aware(m.created_at))
    period = max(0.0, (until - start).total_seconds())
    q = select(func.count(), func.count().filter(MonitorCheck.ok.is_(False)),
               func.avg(MonitorCheck.duration_ms).filter(MonitorCheck.ok.is_(True)),
               func.percentile_cont(0.95).within_group(MonitorCheck.duration_ms).filter(MonitorCheck.ok.is_(True))
               ).where(MonitorCheck.monitor_id == m.id, MonitorCheck.checked_at >= start, MonitorCheck.checked_at <= until)
    total, failed, avg, p95 = db.execute(q).one()
    incs = db.scalars(select(MonitorIncident).where(
        MonitorIncident.monitor_id == m.id, MonitorIncident.started_at <= until,
        or_(MonitorIncident.ended_at.is_(None), MonitorIncident.ended_at >= start))).all()
    down = sum(_overlap(_aware(i.started_at), _aware(i.ended_at) if i.ended_at else until, start, until) for i in incs)
    closed = [(_aware(i.ended_at) - _aware(i.started_at)).total_seconds() for i in incs if i.ended_at]
    return {
        "checks": total or 0, "failed_checks": failed or 0,
        "uptime_pct": None if not total else round(max(0.0, 100 * (1 - down / period)) if period else 100.0, 3),
        "downtime_seconds": int(down), "incidents": len(incs),
        "mttr_seconds": int(sum(closed) / len(closed)) if closed else 0,
        "avg_ms": int(avg or 0), "p95_ms": int(p95 or 0),
    }


def daily(db: Session, m: ApiMonitor, days: int) -> list[dict]:
    now = utcnow()
    first = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(select(func.date_trunc("day", MonitorCheck.checked_at).label("d"), func.count(),
                             func.avg(MonitorCheck.duration_ms).filter(MonitorCheck.ok.is_(True)))
                      .where(MonitorCheck.monitor_id == m.id, MonitorCheck.checked_at >= first)
                      .group_by("d")).all()
    by_day = {_aware(d).date(): (n, int(a or 0)) for d, n, a in rows}
    incs = db.scalars(select(MonitorIncident).where(MonitorIncident.monitor_id == m.id,
                                                    or_(MonitorIncident.ended_at.is_(None), MonitorIncident.ended_at >= first))).all()
    created = _aware(m.created_at)
    out = []
    for i in range(days):
        d0 = first + timedelta(days=i)
        d1 = min(d0 + timedelta(days=1), now)
        s0 = max(d0, created)
        span = max(0.0, (d1 - s0).total_seconds())
        down = sum(_overlap(_aware(x.started_at), _aware(x.ended_at) if x.ended_at else now, s0, d1) for x in incs) if span else 0
        n, avg = by_day.get(d0.date(), (0, 0))
        out.append({"date": d0.date().isoformat(), "checks": n, "avg_ms": avg, "downtime_seconds": int(down),
                    "uptime_pct": None if not n or not span else round(max(0.0, 100 * (1 - down / span)), 2)})
    return out


def series(db: Session, m: ApiMonitor, hours: int = 24) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    bucket = "15 minutes" if hours <= 24 else "2 hours"
    b = func.date_bin(literal_column(f"interval '{bucket}'"), MonitorCheck.checked_at,
                      literal_column("timestamptz '2000-01-01 00:00:00+00'")).label("b")
    rows = db.execute(select(b, func.count().filter(MonitorCheck.ok.is_(True)), func.count().filter(MonitorCheck.ok.is_(False)),
                             func.avg(MonitorCheck.duration_ms).filter(MonitorCheck.ok.is_(True)))
                      .where(MonitorCheck.monitor_id == m.id, MonitorCheck.checked_at >= since).group_by(b).order_by(b)).all()
    return [{"t": _aware(t).isoformat(), "ok": ok, "failed": bad, "avg_ms": int(a or 0)} for t, ok, bad, a in rows]


def incidents(db: Session, m: ApiMonitor, since: datetime | None = None, limit: int = 100) -> list[dict]:
    q = select(MonitorIncident).where(MonitorIncident.monitor_id == m.id)
    if since:
        q = q.where(or_(MonitorIncident.ended_at.is_(None), MonitorIncident.ended_at >= since))
    now = utcnow()
    return [{"id": i.id, "started_at": i.started_at, "ended_at": i.ended_at, "cause": i.cause,
             "failed_checks": i.failed_checks,
             "duration_seconds": int(((_aware(i.ended_at) if i.ended_at else now) - _aware(i.started_at)).total_seconds()),
             "alerts": i.alerts or []}
            for i in db.scalars(q.order_by(MonitorIncident.started_at.desc()).limit(limit)).all()]


def report_data(db: Session, m: ApiMonitor, days: int) -> dict:
    now = utcnow()
    since = now - timedelta(days=days)
    tenant = db.get(Tenant, m.tenant_id)
    return {"format": "testagent.uptime_report", "version": 1, "organization": tenant.name if tenant else "",
            "monitor": {"id": m.id, "name": m.name, "url": m.url, "method": m.method, "status": m.status,
                        "interval_seconds": m.interval_seconds, "failure_threshold": m.failure_threshold},
            "days": days, "period_start": since, "period_end": now, "generated_at": now,
            "stats": stats(db, m, since, now), "daily": daily(db, m, days), "incidents": incidents(db, m, since, 500)}
