"""Background worker: claims queued runs from PostgreSQL and executes them in isolated
Chromium contexts. Start with `python -m testpilot.worker`."""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from sqlalchemy import func, select, text, update

from . import access, apitesting, artifacts, automation, integrations, logging_setup, mobilerun, monitoring, privacy, report, report_store, storage, storage_policy, targets
from .config import get_settings
from .db import session_scope
from .egress_proxy import EgressProxy
from .logging_setup import redact, register_secret
from .models import TERMINAL, AuditLog, Project, ProjectSecret, Run, RunStep, Tenant, TestRunItem, User, WorkerHeartbeat
from .security import SecretsUnavailable, decrypt_secret
from .storage_providers import StorageError
from .steps import SECRET_RE, describe, secret_names
from .urlguard import TargetRejected, check_url

log = logging.getLogger("testpilot.worker")
_shutdown = threading.Event()
CLAIM_LOCK_KEY = 74_220_001


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StepFailed(Exception):
    pass


class RunAborted(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


# --------------------------------------------------------------------------- claiming
def reap_stale() -> int:
    s = get_settings()
    cutoff = utcnow() - timedelta(minutes=s.stale_run_minutes)
    with session_scope() as db:
        stale = func.coalesce(Run.heartbeat_at, Run.started_at) < cutoff
        # Organizations with their own storage: whatever was captured is uploaded (or expires) like any run.
        db.execute(update(Run).where(Run.status == "running", stale, Run.storage_mode == "external")
                   .values(upload_state="pending"))
        res = db.execute(
            update(Run)
            .where(Run.status == "running", stale)
            .values(status="error", finished_at=utcnow(),
                    error="Worker stopped responding (no heartbeat); the run was abandoned. Re-run it.")
        )
        return res.rowcount or 0


def claim(worker_id: str) -> int | None:
    s = get_settings()
    with session_scope() as db:
        # Serialise claims across all workers so the global concurrency limit holds.
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLAIM_LOCK_KEY})
        running = db.scalar(select(func.count()).select_from(Run).where(Run.status == "running")) or 0
        if running >= s.max_concurrent_runs:
            return None
        # Runs of suspended organizations stay queued until the organization is reactivated.
        run = db.scalars(
            select(Run).join(Tenant, Tenant.id == Run.tenant_id)
            .where(Run.status == "queued", Tenant.status == "active").order_by(Run.created_at, Run.id)
            .with_for_update(of=Run, skip_locked=True).limit(1)
        ).first()
        if not run:
            return None
        if run.cancel_requested:
            run.status, run.finished_at = "cancelled", utcnow()
            return None
        run.status, run.started_at, run.heartbeat_at, run.worker_id = "running", utcnow(), utcnow(), worker_id
        return run.id


# --------------------------------------------------------------------------- execution
class Execution:
    def __init__(self, run_id: int, worker_id: str = ""):
        self.s = get_settings()
        self.run_id = run_id
        self.worker_id = worker_id
        self.cancel = threading.Event()
        self.started = time.monotonic()
        self.deadline = self.started + self.s.run_timeout_seconds
        self.secrets: dict[str, str] = {}
        self.blocked_count = 0
        self._log_fh = None
        self.storage_down = False  # storage not usable at start: nothing is recorded
        self.storage_block: str | None = None

    # ---- logging ----
    def log(self, msg: str) -> None:
        line = f"{utcnow().isoformat(timespec='milliseconds')} {redact(msg)}\n"
        if self._log_fh:
            self._log_fh.write(line)
            self._log_fh.flush()

    # ---- heartbeat / cancellation / watchdog ----
    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(self.s.worker_heartbeat_seconds):
            try:
                Path("/tmp/worker-alive").write_text(str(time.time()))  # container healthcheck
            except OSError:
                pass
            try:
                with session_scope() as db:
                    db.execute(update(Run).where(Run.id == self.run_id).values(heartbeat_at=utcnow()))
                    if self.worker_id:
                        db.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == self.worker_id)
                                   .values(last_seen=utcnow()))
                    if db.scalar(select(Run.cancel_requested).where(Run.id == self.run_id)):
                        self.cancel.set()
            except Exception as e:  # keep beating through transient DB errors
                log.warning("heartbeat failed for run %s: %s", self.run_id, e)
            if time.monotonic() > self.deadline + 120:
                # A step is hung beyond every timeout; exit so the container restarts cleanly.
                log.error("run %s exceeded its hard deadline; exiting worker", self.run_id)
                os._exit(3)

    def _check_continue(self) -> None:
        if _shutdown.is_set():
            raise RunAborted("error", "Worker was shut down during the run. Re-run the test.")
        if self.cancel.is_set():
            raise RunAborted("cancelled", "Cancelled by user")
        if time.monotonic() > self.deadline:
            raise RunAborted("error", f"Run exceeded the {self.s.run_timeout_seconds}s execution limit")

    def _remaining_ms(self, want: int) -> int:
        left = int((self.deadline - time.monotonic()) * 1000)
        return max(100, min(want, left))

    # ---- main ----
    def execute(self) -> None:
        stop_hb = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(stop_hb,), daemon=True)
        hb.start()
        final_status, final_error = "error", ""
        try:
            self._decide_storage()
            with session_scope() as db:
                run = db.get(Run, self.run_id)
                project = db.get(Project, run.project_id)
                tenant = db.get(Tenant, run.tenant_id)
                # Re-authorise at execution time: the queue entry alone is not trusted.
                if not tenant or tenant.status != "active":
                    raise RunAborted("error", "The organization is not active; the run was not executed")
                if not project or project.tenant_id != run.tenant_id or project.archived:
                    raise RunAborted("error", "The project is not available to this organization")
                if run.requested_by:
                    # The requester's access is re-evaluated now: an assignment that expired or was
                    # revoked while the run waited in the queue stops it here.
                    u = db.get(User, run.requested_by)
                    if (not u or not u.is_active or u.tenant_id != run.tenant_id
                            or not access.has(db, u, project.id, "run.execute")):
                        raise RunAborted("error", "The person who started this run no longer has permission "
                                                  "to run tests in this project")
                if self.storage_block:
                    raise RunAborted("error", self.storage_block)
                srow = storage_policy.row(db, run.tenant_id)
                if run.storage_mode == "external":
                    # Fail safe before anything is captured: the destination must be reachable now.
                    try:
                        prov = storage_policy.provider_for(db, srow)
                        prov.ensure_folder(storage_policy.run_parts(storage_policy.folders(db, srow), project, run.id))
                    except Exception as e:  # any failure here (not only StorageError) must fail safe
                        self.storage_down = True
                        self._storage_health(str(e))
                        raise RunAborted("error", f"Your organization's {storage_policy.PROVIDER_LABELS[run.storage_provider]} "
                                                  f"storage is unavailable ({e}). The test was not run and nothing was stored.")
                rd = storage.work_dir(run)
                if run.storage_mode == "external":
                    import json as _json
                    (rd / "test-case.json").write_text(_json.dumps(
                        {"format": "testpilot.test_case", "run_id": run.id, "test_case_id": run.test_case_id,
                         **{k: run.case_snapshot.get(k) for k in ("name", "description", "steps")}},
                        indent=2, ensure_ascii=False), encoding="utf-8")
                pol = targets.build(db, project)
                snapshot = dict(run.case_snapshot)
                needed = secret_names(snapshot.get("steps", []))
                if needed:
                    rows = {r.name: r for r in db.scalars(select(ProjectSecret).where(
                        ProjectSecret.tenant_id == run.tenant_id, ProjectSecret.project_id == project.id,
                        ProjectSecret.name.in_(needed))).all()}
                    missing = needed - rows.keys()
                    if missing:
                        raise RunAborted("error", "Missing project secrets: " + ", ".join(sorted(missing)))
                    for n, r in rows.items():
                        try:
                            self.secrets[n] = decrypt_secret(r.value_encrypted)
                        except SecretsUnavailable as e:
                            raise RunAborted("error", str(e))
                        register_secret(self.secrets[n])
                base_url = project.base_url
            self._log_fh = (rd / "run.log").open("a", encoding="utf-8")
            os.chmod(rd / "run.log", 0o640)
            self.log(f"run {self.run_id} started on {socket.gethostname()} — case '{snapshot.get('name')}'")
            if pol.problems:
                raise RunAborted("error", "Target policy rejected this project: " + "; ".join(pol.problems))
            self.log(f"authorised hosts: {', '.join(sorted(pol.policy.allowed_hosts))}; ports: {sorted(pol.ports)}")
            final_status, final_error = self._browse(rd, base_url, pol, snapshot)
        except RunAborted as a:
            final_status, final_error = a.status, a.reason
            self.log(f"run aborted: {a.reason}")
        except Exception as e:
            final_status, final_error = "error", f"Internal error: {type(e).__name__}: {redact(str(e))[:1500]}"
            self.log("internal error:\n" + traceback.format_exc())
            log.exception("run %s crashed", self.run_id)
        finally:
            stop_hb.set()
            self._finish(final_status, final_error)

    def _browse(self, rd: Path, base_url: str, pol: targets.ProjectPolicy, snapshot: dict) -> tuple[str, str]:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import sync_playwright

        steps: list[dict] = snapshot.get("steps", [])
        uses_secrets = bool(self.secrets)
        proxy = EgressProxy(pol.policy, pol.ports, on_block=lambda m: self.log(f"BLOCKED {m}"))
        pport = proxy.start()
        status, error = "passed", ""
        video_tmp = rd / "video_tmp"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    proxy={"server": f"http://127.0.0.1:{pport}"},
                    args=["--proxy-bypass-list=<-loopback>",
                          # WebRTC UDP would bypass the egress proxy: allow only proxied traffic.
                          "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                          "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                          "--disable-extensions", "--disable-background-networking",
                          "--no-first-run", "--disable-sync", "--disable-default-apps"],
                    handle_sigint=False, handle_sigterm=False, handle_sighup=False,
                )
                try:
                    ctx_opts = dict(base_url=base_url, viewport={"width": 1366, "height": 768},
                                    service_workers="block", accept_downloads=False, locale="en-IN",
                                    timezone_id="Asia/Kolkata")
                    if self.s.capture_video:
                        video_tmp.mkdir(exist_ok=True)
                        ctx_opts.update(record_video_dir=str(video_tmp), record_video_size={"width": 1366, "height": 768})
                    context = browser.new_context(**ctx_opts)  # fresh, isolated context per run
                    context.set_default_timeout(self.s.step_timeout_ms)
                    context.set_default_navigation_timeout(self.s.navigation_timeout_ms)
                    if self.s.capture_trace:
                        # DOM snapshots can capture typed values; omit them when secrets are in play.
                        context.tracing.start(screenshots=True, snapshots=not uses_secrets, sources=False)
                    page = context.new_page()
                    page.on("console", lambda m: self.log(f"console.{m.type}: {m.text[:500]}"))
                    page.on("pageerror", lambda e: self.log(f"page error: {str(e)[:500]}"))
                    page.on("requestfailed", lambda r: self.log(f"request failed: {r.url[:300]} {r.failure or ''}"))
                    try:
                        status, error = self._run_steps(page, steps, rd, base_url, pol, proxy)
                    finally:
                        if self.s.capture_trace:
                            try:
                                context.tracing.stop(path=str(rd / "trace.zip"))
                                if self.secrets:
                                    self._scrub_trace(rd / "trace.zip")
                            except PWError as e:
                                self.log(f"trace not saved: {e}")
                        vid = page.video
                        context.close()
                        if vid:
                            try:
                                src = Path(vid.path())
                                if src.exists():
                                    shutil.move(str(src), rd / "video.webm")
                            except PWError as e:
                                self.log(f"video not saved: {e}")
                finally:
                    browser.close()
        finally:
            proxy.stop()
            shutil.rmtree(video_tmp, ignore_errors=True)
        self.blocked_count = len(proxy.blocked)
        return status, error

    def _scrub_trace(self, path: Path) -> None:
        """Traces record action parameters (e.g. fill values). Remove secret values from every
        entry, verify, and discard the trace entirely if any occurrence survives."""
        import json as _json
        import zipfile

        needles: list[tuple[bytes, bytes]] = []
        for v in self.secrets.values():
            if not v:
                continue
            forms = {v, _json.dumps(v)[1:-1], _json.dumps(v, ensure_ascii=False)[1:-1]}
            for f in forms:
                needles.append((f.encode(), b"[secret]"))
        tmp = path.with_suffix(".scrub")
        try:
            with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    for n, r in needles:
                        data = data.replace(n, r)
                    dst.writestr(info, data)
            with zipfile.ZipFile(tmp) as chk:
                leaked = any(n in chk.read(i) for i in chk.namelist() for n, _ in needles)
            if leaked:
                raise ValueError("secret survived scrubbing")
            tmp.replace(path)
            self.log("trace scrubbed of secret values")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            self.log(f"trace discarded: could not guarantee secret removal ({e})")

    def _shot(self, page, rd: Path, name: str, full_page: bool = False) -> Path | None:
        try:
            path = rd / name
            page.screenshot(path=str(path), type="jpeg", quality=80, full_page=full_page,
                            timeout=self._remaining_ms(10_000), animations="disabled")
            os.chmod(path, 0o640)
            return path
        except Exception as e:
            self.log(f"screenshot {name} failed: {e}")
            return None

    def _run_steps(self, page, steps, rd, base_url, pol, proxy) -> tuple[str, str]:
        with session_scope() as db:
            step_ids = {r.idx: r.id for r in db.scalars(select(RunStep).where(RunStep.run_id == self.run_id)).all()}
        failed_at: int | None = None
        abort: RunAborted | None = None
        error = ""
        for i, step in enumerate(steps):
            try:
                self._check_continue()
            except RunAborted as a:
                abort = a
                failed_at = i
                break
            t0 = time.monotonic()
            started = utcnow()
            retries = min(step.get("retries", 0), self.s.max_step_retries)
            attempts, last_err = 0, ""
            ok = False
            self.log(f"step {i + 1}: {describe(step)}")
            while attempts <= retries:
                attempts += 1
                try:
                    blocked_before = len(proxy.blocked)
                    try:
                        self._do(page, step, rd, i, base_url, pol)
                    except Exception as e:
                        # An HTTPS navigation refused at CONNECT surfaces as a tunnel error.
                        if step["action"] == "goto" and "ERR_TUNNEL_CONNECTION_FAILED" in str(e) \
                                and len(proxy.blocked) > blocked_before:
                            raise StepFailed("Navigation blocked by egress policy: " + proxy.blocked[-1]) from e
                        raise
                    ok = True
                    break
                except RunAborted:
                    raise
                except Exception as e:
                    last_err = redact(_clean_error(e))
                    self.log(f"step {i + 1} attempt {attempts} failed: {last_err}")
                    if attempts <= retries:
                        time.sleep(0.5)
            shot = None
            if self.s.screenshot_mode == "each_step" or not ok:
                shot = self._shot(page, rd, f"step-{i + 1:03d}.jpg")
            with session_scope() as db:
                rs = db.get(RunStep, step_ids[i])
                rs.status = "passed" if ok else "failed"
                rs.attempts, rs.error = attempts, "" if ok else last_err[:4000]
                rs.started_at, rs.finished_at = started, utcnow()
                rs.duration_ms = int((time.monotonic() - t0) * 1000)
                run = db.get(Run, self.run_id)
                if shot:
                    storage.record(db, run, shot, "screenshot", "image/jpeg", step_id=rs.id)
                extra = rd / f"step-{i + 1:03d}-{_slug(step.get('name', ''))}.jpg"
                if step["action"] == "screenshot" and extra.exists():
                    storage.record(db, run, extra, "screenshot", "image/jpeg", step_id=rs.id)
            if not ok:
                failed_at, error = i, f"Step {i + 1} failed: {last_err[:1500]}"
                break
        if failed_at is not None:
            with session_scope() as db:
                db.execute(update(RunStep).where(RunStep.run_id == self.run_id, RunStep.idx >= (failed_at + (0 if abort else 1)),
                                                 RunStep.status == "pending").values(status="skipped"))
        if abort:
            raise abort
        return ("failed", error) if failed_at is not None else ("passed", "")

    def _do(self, page, step: dict, rd: Path, i: int, base_url: str, pol) -> None:
        from playwright.sync_api import expect

        a = step["action"]
        tmo = self._remaining_ms(step.get("timeout_ms") or self.s.step_timeout_ms)
        loc = page.locator(step["selector"]) if "selector" in step else None
        if a == "goto":
            url = step["url"]
            if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
                url = urljoin(base_url.rstrip("/") + "/", url.lstrip("/")) if url.startswith("/") else urljoin(base_url, url)
            try:
                check_url(url, pol.policy, resolve_dns=False)
            except TargetRejected as e:
                raise StepFailed(f"Navigation refused: {e}")
            resp = page.goto(url, wait_until="load", timeout=self._remaining_ms(step.get("timeout_ms") or self.s.navigation_timeout_ms))
            if resp is not None and resp.headers.get("x-testpilot-blocked") == "1":
                try:
                    reason = resp.text()[:500].split(": ", 1)[-1]
                except Exception:
                    reason = "target not authorised"
                raise StepFailed(f"Navigation blocked by egress policy: {resp.url} — {reason}")
            self.log(f"navigated to {page.url} (HTTP {resp.status if resp else 'n/a'})")
        elif a == "click":
            loc.click(timeout=tmo)
        elif a == "fill":
            value = step["value"]
            if SECRET_RE.search(value):
                # Secrets may only be typed into password inputs, so they cannot be echoed on
                # screen (screenshots) or into ordinary fields that the page might display.
                kind = (loc.get_attribute("type", timeout=tmo) or "").lower()
                if kind != "password":
                    raise StepFailed("Project secrets can only be filled into password fields (input type=password)")
            loc.fill(self._subst(value), timeout=tmo)
        elif a == "press":
            loc.press(step["key"], timeout=tmo)
        elif a == "select":
            loc.select_option(self._subst(step["value"]), timeout=tmo)
        elif a == "check":
            loc.check(timeout=tmo)
        elif a == "uncheck":
            loc.uncheck(timeout=tmo)
        elif a == "hover":
            loc.hover(timeout=tmo)
        elif a == "wait_for":
            loc.wait_for(state=step.get("state", "visible"), timeout=tmo)
        elif a == "expect_visible":
            expect(loc).to_be_visible(timeout=tmo)
        elif a == "expect_hidden":
            expect(loc).to_be_hidden(timeout=tmo)
        elif a == "expect_text":
            if step.get("match", "contains") == "equals":
                expect(loc).to_have_text(step["text"], timeout=tmo)
            else:
                expect(loc).to_contain_text(step["text"], timeout=tmo)
        elif a == "expect_url":
            expect(page).to_have_url(re.compile(re.escape(step["contains"])), timeout=tmo)
        elif a == "expect_title":
            expect(page).to_have_title(re.compile(re.escape(step["contains"])), timeout=tmo)
        elif a == "screenshot":
            path = self._shot(page, rd, f"step-{i + 1:03d}-{_slug(step.get('name', ''))}.jpg", step.get("full_page", False))
            if not path:
                raise StepFailed("screenshot could not be captured")
        elif a == "wait":
            page.wait_for_timeout(min(step["ms"], self._remaining_ms(step["ms"])))
        else:  # unreachable: validated at save time
            raise StepFailed(f"unknown action {a}")

    def _subst(self, value: str) -> str:
        return SECRET_RE.sub(lambda m: self.secrets.get(m.group(1), ""), value)

    # ---- completion ----
    def _finish(self, status: str, error: str) -> None:
        try:
            if self._log_fh:
                self.log(f"run finished: {status}{(' — ' + error) if error else ''}")
                self._log_fh.close()
                self._log_fh = None
            if self.storage_down:
                self._finish_without_artifacts(status, error)
                return
            with session_scope() as db:
                run = db.get(Run, self.run_id)
                rd = storage.work_dir(run)
                db.execute(update(RunStep).where(RunStep.run_id == run.id, RunStep.status.in_(("pending", "running")))
                           .values(status="skipped"))
                for fname, kind, mime in (("trace.zip", "trace", "application/zip"),
                                          ("video.webm", "video", "video/webm"),
                                          ("run.log", "log", "text/plain"),
                                          ("test-case.json", "test_case", "application/json")):
                    p = rd / fname
                    if p.exists():
                        os.chmod(p, 0o640)
                        storage.record(db, run, p, kind, mime)
                db.flush()
                db.refresh(run)
                counts = {k: sum(1 for s in run.steps if s.status == k) for k in ("passed", "failed", "skipped")}
                counts.update(total=len(run.steps), blocked_requests=self.blocked_count)
                run.status = status if status in TERMINAL else "error"
                run.error = error[:4000]
                run.summary = counts
                run.finished_at = utcnow()
                # Write the outcome into any test-run items that requested this automated run.
                item_status = {"passed": "passed", "failed": "failed"}.get(run.status, "retest")
                actual = (f"Automated run #{run.id}: all {counts['total']} steps passed" if run.status == "passed"
                          else f"Automated run #{run.id} {run.status}: {run.error[:1500]}")
                db.execute(update(TestRunItem).where(TestRunItem.automated_run_id == run.id,
                                                 TestRunItem.tenant_id == run.tenant_id).values(
                    status=item_status, actual_result=actual, executed_at=utcnow(),
                    executed_by_name="TestAgent (automated)", updated_at=utcnow()))
                automation.on_run_finished(db, run)  # AI-automation trial runs
                db.flush()
                data = report.build_data(db, run)
                (rd / "report.json").write_bytes(report.to_json(data))
                (rd / "report.html").write_bytes(report.to_html(db, data))
                for fname, kind, mime in (("report.json", "report_json", "application/json"),
                                          ("report.html", "report_html", "text/html")):
                    os.chmod(rd / fname, 0o640)
                    storage.record(db, run, rd / fname, kind, mime)
                external = run.storage_mode == "external"
                if external:
                    run.upload_state = "pending"
            log.info("run %s finished: %s", self.run_id, status)
            if external:
                try:
                    if not artifacts.upload_run(self.run_id):
                        log.warning("run %s: artifacts wait for the organization's storage", self.run_id)
                except Exception:
                    log.exception("run %s: upload failed; will retry", self.run_id)
            elif report_store.enabled():
                report_store.sync_pending(limit=5)  # push this report now; retries happen in the main loop
        except Exception:
            log.exception("could not finalise run %s", self.run_id)
            with session_scope() as db:
                db.execute(update(Run).where(Run.id == self.run_id, Run.status == "running").values(
                    status="error", finished_at=utcnow(), error=(error or "Failed to finalise run")[:4000]))
                db.execute(update(Run).where(Run.id == self.run_id, Run.storage_mode == "external")
                           .values(upload_state="pending"))


    def _decide_storage(self) -> None:
        """Fixes where this run's artifacts go before anything else happens, so every later path
        (including aborts) follows the organization's storage choice."""
        self.storage_block = None
        with session_scope() as db:
            run = db.get(Run, self.run_id)
            block = storage_policy.run_block_reason(db, run.tenant_id)
            if block:
                self.storage_block, self.storage_down = block, True
                return
            srow = storage_policy.row(db, run.tenant_id)
            run.storage_mode = srow.mode
            run.storage_provider = srow.provider if srow.mode == "external" else "local"

    def _storage_health(self, err: str) -> None:
        with session_scope() as db:
            run = db.get(Run, self.run_id)
            srow = storage_policy.row(db, run.tenant_id)
            srow.status, srow.last_error, srow.last_check_at = "error", err[:1000], utcnow()

    def _finish_without_artifacts(self, status: str, error: str) -> None:
        """The organization's storage could not be used: record only the outcome, keep no files."""
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        with session_scope() as db:
            run = db.get(Run, self.run_id)
            db.execute(update(RunStep).where(RunStep.run_id == run.id, RunStep.status.in_(("pending", "running")))
                       .values(status="skipped"))
            run.status, run.error, run.finished_at = "error", error[:4000], utcnow()
            run.summary = {"total": len(run.steps), "passed": 0, "failed": 0, "skipped": len(run.steps),
                           "blocked_requests": 0}
            run.report_synced_at = utcnow()  # nothing to copy
            if run.storage_mode == "external":
                shutil.rmtree(storage.spool_root() / "tenants" / str(run.tenant_id) / "runs" / str(run.id),
                              ignore_errors=True)
        log.info("run %s not executed: %s", self.run_id, error)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s or "checkpoint").strip("-")[:60] or "checkpoint"


def _clean_error(e: Exception) -> str:
    msg = str(e)
    # Playwright errors carry a verbose call log; keep the headline and the first few log lines.
    lines = [l for l in msg.splitlines() if l.strip()]
    return "\n".join(lines[:8])[:2000]


# --------------------------------------------------------------------------- main loop
def beat(worker_id: str, run_id: int | None = None) -> None:
    from sqlalchemy.dialects.postgresql import insert

    with session_scope() as db:
        stmt = insert(WorkerHeartbeat).values(worker_id=worker_id, last_seen=utcnow(), current_run_id=run_id)
        db.execute(stmt.on_conflict_do_update(index_elements=["worker_id"],
                                              set_={"last_seen": utcnow(), "current_run_id": run_id}))
        db.execute(WorkerHeartbeat.__table__.delete().where(WorkerHeartbeat.last_seen < utcnow() - timedelta(days=1)))


def expire_assignments() -> int:
    """Records assignments that reached their end (enforcement itself happens on every request)."""
    with session_scope() as db:
        rows = access.mark_expired(db)
        for a in rows:
            db.add(AuditLog(tenant_id=a.tenant_id, scope="tenant", action="assignment_expired",
                            target=f"assignment {a.id}",
                            detail={"assignment_id": a.id, "user_id": a.user_id, "group_id": a.group_id,
                                    "project_id": a.project_id, "scope": a.scope}))
        return len(rows)


def _guard(name: str, fn) -> None:
    try:
        fn()
    except Exception:
        log.exception("%s job failed", name)


def run_once(worker_id: str = "test-worker") -> int | None:
    rid = claim(worker_id)
    if rid:
        beat(worker_id, rid)
        Execution(rid, worker_id).execute()
        beat(worker_id)
    return rid


def main() -> None:
    logging_setup.setup("worker")
    s = get_settings()
    problems = s.problems()
    if problems:
        for p in problems:
            log.error("configuration problem: %s", p)
        raise SystemExit(2)
    storage.root()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: _shutdown.set())
    log.info("worker %s started (max concurrent runs %s)", worker_id, s.max_concurrent_runs)
    monitoring.start(_shutdown)  # API uptime monitors (release 1.6)
    mobilerun.start(_shutdown, worker_id)  # mobile runs and live device sessions (release 1.8)
    hb_file = Path("/tmp/worker-alive")
    last_reap = last_sync = last_upload = last_daily = 0.0
    if report_store.enabled():
        r = report_store.check()
        log.info("MySQL report store: %s", "connected to " + r.get("server", "") if r.get("ok") else "UNAVAILABLE — " + r.get("error", ""))
    while not _shutdown.is_set():
        try:
            hb_file.write_text(str(time.time()))
            beat(worker_id)
            if time.time() - last_reap > 60:
                n = reap_stale() + automation.reap_stale() + apitesting.reap_stale() + integrations.reap_stale() + mobilerun.reap_stale()
                if n:
                    log.warning("marked %s stale run(s) as error", n)
                expire_assignments()
                last_reap = time.time()
            # Housekeeping: each job is isolated and rescheduled before it runs, so a failing job can
            # neither repeat on every loop nor stop test runs from being claimed.
            if time.time() - last_upload > 30:
                last_upload = time.time()
                _guard("upload retry", artifacts.retry_pending)
                _guard("connectors", integrations.tick)  # push results to ADO / Jira / GitHub / GitLab / webhooks
            if time.time() - last_daily > 6 * 3600:
                last_daily = time.time()
                _guard("retention", artifacts.retention_purge)
                _guard("privacy retention", privacy.purge_expired)
                _guard("monitor retention", monitoring.purge_old)
                _guard("mobile session screens", mobilerun.purge_old)
            if report_store.enabled() and time.time() - last_sync > s.mysql_reports_sync_interval_seconds:
                ok, bad = report_store.sync_pending()
                if ok:
                    log.info("pushed %s report(s) to MySQL", ok)
                last_sync = time.time()
            if run_once(worker_id):
                continue
            if apitesting.tick(worker_id):  # API test runs and OpenAPI imports
                continue
            if automation.tick(worker_id):  # AI automation of manual cases, when no run is waiting
                continue
        except Exception:
            log.exception("worker loop error")
            time.sleep(5)
        _shutdown.wait(s.worker_poll_seconds)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
