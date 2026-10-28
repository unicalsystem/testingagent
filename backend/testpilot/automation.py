"""AI automation of manual test cases, executed by the worker.

Per job:  explore the application once (setup steps + a crawl of same-host pages, GET only)
          -> an "application map" of pages, fields, buttons and links with ready-made selectors.
Per case: Claude drafts steps from the manual procedure + map -> optional trial run in the browser
          -> on failure Claude repairs the draft using the error and screenshot (bounded attempts)
          -> verified drafts are applied to the case (automatically or after human review).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select, update

from . import ai, artifacts, storage, storage_policy, targets
from .db import session_scope
from .logging_setup import redact, register_secret
from .models import (AutomationItem, AutomationJob, Evidence, Project, ProjectSecret, Run, RunStep, Tenant, TestCase,
                     TestRunItem, utcnow)
from .security import SecretsUnavailable, decrypt_secret
from .steps import describe, secret_names

log = logging.getLogger("testpilot.automation")
MAX_PAGES = 15
SKIP_LINK_WORDS = ("logout", "log-out", "signout", "sign-out", "logoff", "delete", "remove", "destroy", "unsubscribe")
MAPPING_STALE = timedelta(minutes=15)

INVENTORY_JS = r"""() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (e) => (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const q = (v) => JSON.stringify(v);
  const sel = (e) => {
    if (e.dataset && e.dataset.testid) return `[data-testid=${e.dataset.testid}]`;
    if (e.id && /^[A-Za-z][\w-]*$/.test(e.id)) return `#${e.id}`;
    const n = e.getAttribute('name'); if (n) return `${e.tagName.toLowerCase()}[name=${q(n)}]`;
    return '';
  };
  const label = (e) => {
    if (e.labels && e.labels[0]) return e.labels[0].innerText.trim().slice(0, 60);
    return (e.getAttribute('aria-label') || e.placeholder || '').trim().slice(0, 60);
  };
  const out = { title: document.title, heads: [], fields: [], buttons: [], links: [], tests: [] };
  document.querySelectorAll('h1,h2,h3').forEach((h) => { if (vis(h) && out.heads.length < 12) out.heads.push(txt(h)); });
  document.querySelectorAll('input,select,textarea').forEach((e) => {
    if (out.fields.length >= 40 || e.type === 'hidden' || !vis(e)) return;
    out.fields.push({ tag: e.tagName.toLowerCase(), type: e.type || '', sel: sel(e), label: label(e),
      options: e.tagName === 'SELECT' ? [...e.options].slice(0, 8).map((o) => o.text.trim()) : undefined });
  });
  document.querySelectorAll('button,input[type=submit],input[type=button],[role=button]').forEach((e) => {
    if (out.buttons.length >= 40 || !vis(e)) return;
    const t = txt(e); if (!t && !sel(e)) return;
    out.buttons.push({ text: t, sel: sel(e) });
  });
  document.querySelectorAll('a[href]').forEach((a) => {
    if (out.links.length >= 60 || !vis(a)) return;
    const t = txt(a); if (!t) return;
    out.links.push({ text: t, href: a.href });
  });
  document.querySelectorAll('[data-testid]').forEach((e) => { if (out.tests.length < 30) out.tests.push(`[data-testid=${e.dataset.testid}] ${txt(e).slice(0, 40)}`); });
  return out;
}"""


def full_steps(project: Project, steps: list[dict], use_setup: bool) -> list[dict]:
    """The steps actually executed for a case: the project's setup steps first when requested."""
    pre = [dict(s, description=s.get("description") or f"Setup: {describe(s)}") for s in (project.setup_steps or [])]
    return (pre if use_setup and pre else []) + [dict(s) for s in steps]


def _fmt_page(path: str, inv: dict) -> str:
    lines = [f"## Page {path}  (title: {inv.get('title', '')[:80]})"]
    if inv.get("heads"):
        lines.append("Headings: " + " | ".join(inv["heads"]))
    for f in inv.get("fields", []):
        extra = f" options={f['options']}" if f.get("options") else ""
        lines.append(f"- field {f['tag']}{'/' + f['type'] if f['type'] else ''} label=\"{f['label']}\" selector={f['sel'] or '(none)'}{extra}")
    for b in inv.get("buttons", []):
        lines.append(f"- button \"{b['text']}\" selector={b['sel'] or ('role=button[name=\"' + b['text'] + '\"]')}")
    for t in inv.get("tests", []):
        lines.append(f"- element {t}")
    for a in inv.get("links", [])[:40]:
        lines.append(f"- link \"{a['text']}\" -> {a['href']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- exploration
def explore(job_id: int, worker_id: str = "") -> None:
    """Builds the job's application map. Runs the project setup steps first (if any), then visits
    the base URL and up to MAX_PAGES same-host links. Navigation only; no clicks on page content."""
    from playwright.sync_api import sync_playwright

    from .egress_proxy import EgressProxy
    from .urlguard import TargetRejected, check_url
    from .worker import Execution

    notes: list[str] = []
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        project = db.get(Project, job.project_id)
        pol = targets.build(db, project)
        setup = list(project.setup_steps or [])
        base_url = project.base_url
        needed = secret_names(setup)
        secrets: dict[str, str] = {}
        if needed:
            for r in db.scalars(select(ProjectSecret).where(ProjectSecret.tenant_id == job.tenant_id,
                                                             ProjectSecret.project_id == project.id,
                                                             ProjectSecret.name.in_(needed))).all():
                try:
                    secrets[r.name] = decrypt_secret(r.value_encrypted)
                    register_secret(secrets[r.name])
                except SecretsUnavailable:
                    pass
    if pol.problems:
        _set_map(job_id, "", "Exploration skipped: " + "; ".join(pol.problems))
        return
    ex = Execution(0, worker_id)
    ex.secrets = secrets
    ex.deadline = time.monotonic() + 240
    ex.log = lambda m: notes.append(redact(m)[:300])  # type: ignore[method-assign]
    proxy = EgressProxy(pol.policy, pol.ports, on_block=lambda m: notes.append(f"blocked {m}"))
    port = proxy.start()
    pages: list[str] = []
    host = (urlsplit(base_url).hostname or "").lower()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy={"server": f"http://127.0.0.1:{port}"},
                                        args=["--proxy-bypass-list=<-loopback>",
                                              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                                              "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                                              "--disable-extensions", "--no-first-run"],
                                        handle_sigint=False, handle_sigterm=False, handle_sighup=False)
            try:
                ctx = browser.new_context(base_url=base_url, viewport={"width": 1366, "height": 768},
                                          service_workers="block", accept_downloads=False)
                ctx.set_default_timeout(8000)
                ctx.set_default_navigation_timeout(20000)
                page = ctx.new_page()
                import tempfile
                from pathlib import Path

                rd = Path(tempfile.mkdtemp(prefix='tp-explore-'))
                # 1. signed-out entry page
                try:
                    page.goto(base_url, wait_until="load")
                    pages.append(_fmt_page(urlsplit(page.url).path or "/", page.evaluate(INVENTORY_JS)) + "\n(signed out)")
                except Exception as e:
                    notes.append(f"could not open base URL: {str(e)[:200]}")
                # 2. setup steps (sign-in)
                if setup:
                    try:
                        for i, st in enumerate(setup):
                            ex._do(page, st, rd, i, base_url, pol)
                        page.wait_for_load_state("load")
                        notes.append(f"setup steps completed; now at {urlsplit(page.url).path}")
                    except Exception as e:
                        notes.append(f"setup steps failed at runtime: {redact(str(e))[:300]}")
                # 3. crawl same-host links
                seen: set[str] = set()
                queue = [page.url] if setup else []
                while queue and len(seen) < MAX_PAGES and time.monotonic() < ex.deadline:
                    url = queue.pop(0).split("#")[0]
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        check_url(url, pol.policy, resolve_dns=False)
                        if page.url.split("#")[0] != url:
                            page.goto(url, wait_until="load")
                        inv = page.evaluate(INVENTORY_JS)
                    except (TargetRejected, Exception) as e:  # noqa: BLE001 - keep exploring
                        notes.append(f"skipped {url[:120]}: {str(e)[:120]}")
                        continue
                    sp = urlsplit(page.url)
                    pages.append(_fmt_page(sp.path + (f"?{sp.query}" if sp.query else ""), inv))
                    for a in inv.get("links", []):
                        h = a.get("href", "")
                        u = urlsplit(urljoin(page.url, h))
                        if u.scheme in ("http", "https") and (u.hostname or "").lower() == host \
                                and not any(w in h.lower() for w in SKIP_LINK_WORDS):
                            full = u._replace(fragment="").geturl()
                            if full not in seen and full not in queue:
                                queue.append(full)
                if not setup and pages:
                    notes.append("no project setup steps: only signed-out pages were explored")
            finally:
                browser.close()
    except Exception as e:  # browser could not start, etc.
        notes.append(f"exploration error: {type(e).__name__}: {str(e)[:300]}")
    finally:
        proxy.stop()
    _set_map(job_id, "\n\n".join(pages), "\n".join(notes))


def _set_map(job_id: int, app_map: str, notes: str) -> None:
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        job.app_map = app_map[:60000]
        job.explore_log = notes[:8000]
        if job.status == "mapping":
            job.status = "running"


# --------------------------------------------------------------------------- per-case processing
def _claim_job_to_map() -> int | None:
    with session_scope() as db:
        # Jobs of suspended organizations wait until the organization is reactivated (like runs).
        active = select(Tenant.id).where(Tenant.status == "active")
        job = db.scalars(select(AutomationJob).where(AutomationJob.status == "exploring", AutomationJob.tenant_id.in_(active))
                         .order_by(AutomationJob.id).with_for_update(skip_locked=True).limit(1)).first()
        if not job:
            # recover a mapping that a crashed worker left behind
            job = db.scalars(select(AutomationJob).where(
                AutomationJob.status == "mapping", AutomationJob.tenant_id.in_(active),
                AutomationJob.created_at < utcnow() - MAPPING_STALE).with_for_update(skip_locked=True).limit(1)).first()
        if not job:
            return None
        job.status = "mapping"
        return job.id


def _claim_item() -> int | None:
    with session_scope() as db:
        it = db.scalars(select(AutomationItem).join(AutomationJob, AutomationJob.id == AutomationItem.job_id)
                        .where(AutomationJob.status == "running", AutomationItem.status.in_(("queued", "repair")),
                               AutomationJob.tenant_id.in_(select(Tenant.id).where(Tenant.status == "active")))
                        .order_by(AutomationItem.job_id, AutomationItem.position, AutomationItem.id)
                        .with_for_update(of=AutomationItem, skip_locked=True).limit(1)).first()
        if not it:
            return None
        it.error = it.error if it.status == "repair" else ""
        it.status = "drafting" if it.status == "queued" else "repairing"
        return it.id


def _finish_jobs() -> None:
    with session_scope() as db:
        for job in db.scalars(select(AutomationJob).where(AutomationJob.status == "running")).all():
            active = db.scalar(select(AutomationItem.id).where(
                AutomationItem.job_id == job.id,
                AutomationItem.status.in_(("queued", "repair", "drafting", "repairing", "trial"))).limit(1))
            if active is None:
                job.status, job.finished_at = "done", utcnow()


def _failure_shot(db, run_id: int, tenant_id: int) -> tuple[bytes | None, str]:
    failed = db.scalars(select(RunStep).where(RunStep.tenant_id == tenant_id, RunStep.run_id == run_id,
                                              RunStep.status == "failed")).first()
    if not failed:
        return None, "image/jpeg"
    ev = db.scalars(select(Evidence).where(Evidence.tenant_id == tenant_id, Evidence.step_id == failed.id,
                                           Evidence.kind == "screenshot").order_by(Evidence.id.desc())).first()
    # Only a copy still held by TestAgent (Unical storage or not yet uploaded) is read; files already in
    # the organization's own SharePoint / Google Drive are not fetched back.
    if not ev or not ev.rel_path or ev.deleted_at is not None:
        return None, "image/jpeg"
    try:
        return storage.resolve_evidence(ev.rel_path).read_bytes(), ev.mime
    except (OSError, PermissionError):
        return None, "image/jpeg"


def _apply(db, it: AutomationItem, job: AutomationJob) -> None:
    c = db.get(TestCase, it.test_case_id)
    c.steps, c.use_setup = list(it.steps), it.use_setup
    attrs = dict(c.attributes or {})
    attrs["Automation"] = f"AI automated (job #{job.id})"
    c.attributes = attrs
    it.status = "applied"
    db.flush()
    artifacts.queue_case_version(db, c, job.created_by)  # organizations with their own storage


def process_item(item_id: int) -> None:
    with session_scope() as db:
        it = db.get(AutomationItem, item_id)
        job = db.get(AutomationJob, it.job_id)
        project = db.get(Project, job.project_id)
        c = db.get(TestCase, it.test_case_id)
        if not storage_policy.row(db, job.tenant_id).ai_enabled:
            it.status = "error"
            it.error = "AI features are turned off in your organization's data-residency settings"
            return
        pol = targets.build(db, project)
        secrets = [x.name for x in db.scalars(select(ProjectSecret).where(ProjectSecret.tenant_id == job.tenant_id,
                                                                          ProjectSecret.project_id == project.id)).all()]
        case = {k: getattr(c, k) for k in ("external_id", "name", "module", "sub_module", "scenario_type", "description",
                                           "preconditions", "test_data", "procedure", "expected_result")}
        previous, shot, mime = None, None, "image/jpeg"
        if it.status == "repairing":
            previous = {"steps": it.steps, "error": it.error}
            if it.trial_run_id:
                shot, mime = _failure_shot(db, it.trial_run_id, job.tenant_id)
        args = dict(case=case, base_url=project.base_url, hosts=sorted(pol.policy.allowed_hosts) or [project.base_url],
                    secret_names=secrets, setup_steps=list(project.setup_steps or []), app_map=job.app_map,
                    app_notes=project.app_notes, previous=previous, screenshot=shot, screenshot_mime=mime)
        opts = dict(job.options or {})
    try:
        out = ai.automate_case(**args)
        err = ""
    except ai.AIUnavailable as e:
        with session_scope() as db:  # put it back; the AI cooldown spaces out retries
            it = db.get(AutomationItem, item_id)
            it.status = "repair" if previous else "queued"
            it.error = it.error if previous else f"waiting for Claude: {e}"
        return
    except ValueError as e:
        out, err = None, str(e)
    with session_scope() as db:
        it = db.get(AutomationItem, item_id)
        job = db.get(AutomationJob, it.job_id)
        job.claude_calls += 1
        it.attempts += 1
        max_attempts = int(opts.get("max_attempts", 2))
        if out is None:
            it.error = err[:4000]
            it.status = ("repair" if previous else "queued") if it.attempts < max_attempts else "error"
            return
        it.reason = out["reason"]
        if not out["automatable"]:
            it.status, it.steps = "manual_only", []
            return
        it.steps, it.use_setup = out["steps"], out["use_setup"]
        if opts.get("verify", True):
            project = db.get(Project, job.project_id)
            c = db.get(TestCase, it.test_case_id)
            steps = full_steps(project, it.steps, it.use_setup)
            missing = secret_names(steps) - {x.name for x in db.scalars(select(ProjectSecret).where(
                ProjectSecret.tenant_id == job.tenant_id, ProjectSecret.project_id == project.id)).all()}
            block = storage_policy.run_block_reason(db, job.tenant_id)
            if missing or block or targets.build(db, project).problems:
                it.status = "drafted"
                it.error = ("Not verified: missing secrets " + ", ".join(sorted(missing))) if missing else \
                    (f"Not verified: {block}" if block else "Not verified: the target host is not authorised")
                return
            srow = storage_policy.row(db, job.tenant_id)
            r = Run(tenant_id=job.tenant_id, project_id=project.id, test_case_id=c.id, requested_by=job.created_by,
                    status="queued",
                    case_snapshot={"name": f"[AI trial {it.attempts}] {c.name}", "description": c.description,
                                   "steps": steps},
                    options={"trial": True, "automation_item": it.id},
                    storage_mode=srow.mode or "unical",
                    storage_provider=srow.provider if srow.mode == "external" else "local")
            db.add(r)
            db.flush()
            for i, st in enumerate(steps):
                db.add(RunStep(tenant_id=job.tenant_id, run_id=r.id, idx=i, action=st["action"],
                               description=describe(st)[:500]))
            it.trial_run_id, it.status = r.id, "trial"
        else:
            it.status = "drafted"
            if opts.get("auto_apply"):
                _apply(db, it, job)
        log.info("automation item %s -> %s", item_id, it.status)


def on_run_finished(db, run: Run) -> None:
    """Called by the worker when a run ends: advances the automation item it was a trial for."""
    it = db.scalars(select(AutomationItem).where(AutomationItem.tenant_id == run.tenant_id,
                                                 AutomationItem.trial_run_id == run.id)).first()
    if not it or it.status != "trial":
        return
    job = db.get(AutomationJob, it.job_id)
    opts = job.options or {}
    if run.status == "passed":
        it.status, it.error = "verified", ""
        if opts.get("auto_apply", True):
            _apply(db, it, job)
        if opts.get("record_results", True) and it.test_run_item_id:
            ti = db.get(TestRunItem, it.test_run_item_id)
            if ti and ti.tenant_id == it.tenant_id and ti.status == "untested":
                ti.status, ti.executed_at = "passed", utcnow()
                ti.executed_by_name = "TestAgent (AI automated)"
                ti.actual_result = f"Automated run #{run.id}: all steps passed"
                ti.automated_run_id = run.id
    elif run.status == "cancelled" or job.status == "cancelled":
        it.status, it.error = "cancelled", "Trial run cancelled"
    else:
        it.error = (run.error or run.status)[:4000]
        it.status = "repair" if it.attempts < int(opts.get("max_attempts", 2)) else "failed"


def reap_stale() -> int:
    cutoff = utcnow() - timedelta(minutes=10)
    with session_scope() as db:
        n = db.execute(update(AutomationItem).where(AutomationItem.status == "drafting", AutomationItem.updated_at < cutoff)
                       .values(status="queued")).rowcount or 0
        n += db.execute(update(AutomationItem).where(AutomationItem.status == "repairing", AutomationItem.updated_at < cutoff)
                        .values(status="repair")).rowcount or 0
        return n


def tick(worker_id: str = "") -> bool:
    """One unit of automation work. Returns True if something was done."""
    jid = _claim_job_to_map()
    if jid:
        explore(jid, worker_id)
        return True
    iid = _claim_item()
    if iid:
        try:
            process_item(iid)
        except Exception as e:  # never wedge an item in drafting
            log.exception("automation item %s crashed", iid)
            with session_scope() as db:
                db.execute(update(AutomationItem).where(AutomationItem.id == iid).values(
                    status="error", error=f"Internal error: {type(e).__name__}: {str(e)[:500]}"))
        return True
    _finish_jobs()
    return False
