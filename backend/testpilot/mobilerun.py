"""Mobile test execution in the worker (release 1.8).

A dispatcher thread (started by worker.main) runs:
  * queued MobileRuns — one case on one device; several in parallel (cloud devices mostly wait on the network),
    at most one at a time on the built-in phone emulation (it is a local Chromium);
  * live MobileSessions — a device held open for the recorder or the AI agent. The API sends one command at a
    time through the database (cmd / cmd_seq) and this host answers in res with a fresh screen observation.

Device clouds are reached directly over HTTPS; the customer's own Appium server is re-checked (https, public
address) before every session. The built-in emulation sends all traffic through the egress proxy.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import types
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select, update

from . import access, mobileproviders as mp, storage, targets
from .config import get_settings
from .db import session_scope
from .logging_setup import redact, register_secret
from .mobiledrivers import AppiumDriver, Driver, EmulatorDriver, Observation, StepError
from .mobilesteps import describe, secret_names, validate_steps
from .models import (MobileApp, MobileProvider, MobileRun, MobileSession, Project,
                     ProjectSecret, Tenant, User, utcnow)
from .security import SecretsUnavailable, decrypt_secret
from .urlguard import normalize_host
from .webdriver import Session as WDSession
from .webdriver import WebDriverError

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


PARALLEL_RUNS = _env_int("MOBILE_PARALLEL_RUNS", 3)
MAX_LIVE = _env_int("MOBILE_MAX_LIVE_SESSIONS", 2)
IDLE_MINUTES = _env_int("MOBILE_SESSION_IDLE_MINUTES", 10)
MAX_SESSION_MINUTES = _env_int("MOBILE_SESSION_MAX_MINUTES", 60)
RUN_TIMEOUT_S = _env_int("MOBILE_RUN_TIMEOUT_SECONDS", 1800)
APP_REF_DAYS = 25  # clouds keep uploads ~30 days; re-upload before they expire


class Abort(Exception):
    pass


# --------------------------------------------------------------------------- storage
def _dir(*parts) -> Path:
    d = storage.root().joinpath(*[str(p) for p in parts])
    d.mkdir(parents=True, exist_ok=True)
    for p in [d, *d.parents]:
        if p == storage.root():
            break
        os.chmod(p, 0o750)
    return d


def run_dir(run) -> Path:
    return _dir("tenants", run.tenant_id, "projects", run.project_id, "mobile", "runs", run.id)


def session_dir(sess) -> Path:
    return _dir("tenants", sess.tenant_id, "projects", sess.project_id, "mobile", "sessions", sess.id)


def app_dir(app) -> Path:
    return _dir("tenants", app.tenant_id, "projects", app.project_id, "mobile", "apps", app.id)


def rel(p: Path) -> str:
    return str(p.resolve().relative_to(storage.root()))


# --------------------------------------------------------------------------- shared setup
def provider_snapshot(row: MobileProvider) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=row.id, kind=row.kind, name=row.name, config=dict(row.config or {}),
                                 secret_enc=row.secret_enc)


def app_snapshot(a: MobileApp | None) -> types.SimpleNamespace | None:
    if not a:
        return None
    return types.SimpleNamespace(id=a.id, tenant_id=a.tenant_id, project_id=a.project_id, name=a.name, platform=a.platform,
                                 filename=a.filename, rel_path=a.rel_path, source_url=a.source_url, app_id=a.app_id,
                                 app_activity=a.app_activity, sha256=a.sha256, refs=dict(a.provider_refs or {}))


def load_secrets(db, tenant_id: int, project_id: int, names: set[str]) -> dict[str, str]:
    if not names:
        return {}
    rows = {r.name: r for r in db.scalars(select(ProjectSecret).where(
        ProjectSecret.tenant_id == tenant_id, ProjectSecret.project_id == project_id, ProjectSecret.name.in_(names))).all()}
    missing = names - rows.keys()
    if missing:
        raise Abort("Missing project secrets: " + ", ".join(sorted(missing)))
    out = {}
    for n, r in rows.items():
        try:
            out[n] = decrypt_secret(r.value_encrypted)
        except SecretsUnavailable as e:
            raise Abort(str(e))
        register_secret(out[n])
    return out


def web_policy(db, project: Project) -> targets.ProjectPolicy:
    pol = targets.build(db, project)
    if pol.problems:
        raise Abort("Target policy rejected this project: " + "; ".join(pol.problems))
    return pol


def url_checker(pol: targets.ProjectPolicy):
    """Mobile web on real devices: the device cloud fetches the page, but the address must still be one of the
    project's authorised hosts."""
    allowed = set(pol.policy.allowed_hosts)

    def check(url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise StepError("only http(s) addresses can be opened")
        host = normalize_host(parts.hostname or "")
        if host not in allowed:
            raise StepError(f"{host or url} is not one of this project's authorised hosts")

    return check


def ensure_app_ref(prov, app) -> str | None:
    """The app reference the device provider needs: a cloud upload id (cached per provider), or a download
    URL for Appium servers (a short-lived signed TestAgent link, or the build's own URL)."""
    if app is None or not (app.rel_path or app.source_url):
        return None  # an app already installed on the devices (package / bundle id only)
    if prov.kind in ("appium", "unical_lab"):
        if app.rel_path:
            return mp.app_download_url(app.id, app.filename)
        return app.source_url or None
    key = str(prov.id)
    ref = app.refs.get(key) or {}
    fresh = ref.get("ref") and ref.get("sha") == (app.sha256 or app.source_url) and \
        time.time() - float(ref.get("ts", 0)) < APP_REF_DAYS * 86400
    if fresh:
        return ref["ref"]
    path = str(storage.resolve_evidence(app.rel_path)) if app.rel_path else None
    r = mp.provider(prov).upload(path, app.source_url or None, app.filename or f"app-{app.id}")
    with session_scope() as db:
        row = db.get(MobileApp, app.id)
        if row:
            refs = dict(row.provider_refs or {})
            refs[key] = {"ref": r, "sha": app.sha256 or app.source_url, "ts": time.time()}
            row.provider_refs = refs
    return r


def open_driver(prov, device: dict, target: dict, app, *, names: dict, secrets: dict, pol=None,
                on_block=None, video_dir: str | None = None) -> Driver:
    web = target.get("kind") == "web"
    if prov.kind == "emulator":
        if not web:
            raise Abort("The built-in phone emulation runs mobile web tests only; use a device cloud or Appium for apps")
        return EmulatorDriver(device=device.get("name") or "Pixel 7", policy=pol.policy, ports=pol.ports,
                              secrets=secrets, on_block=on_block, video_dir=video_dir, check_url=url_checker(pol))
    p = mp.provider(prov)
    if prov.kind == "appium":
        from .integrations import ConnectorError, check_endpoint
        try:
            check_endpoint(p.hub())
        except ConnectorError as e:
            raise Abort(f"Appium server address rejected: {e}")
    platform = (device.get("platform") or target.get("platform") or "android").lower()
    if not web and app is not None and app.platform != platform:
        raise Abort(f"The build is for {app.platform}, but the device is {platform}")
    tgt = dict(target)
    if app is not None:
        tgt.setdefault("app_package", app.app_id)
        tgt.setdefault("app_activity", app.app_activity)
    try:
        ref = ensure_app_ref(prov, app) if not web else None
    except mp.ProviderError as e:
        raise Abort(str(e))
    if not web and not ref and not tgt.get("app_package"):
        raise Abort("Choose an app build (or a package / bundle id already installed on the device)")
    caps = p.caps(device, tgt, ref, names)
    try:
        s = WDSession.create(mp.TEST_HUB_OVERRIDE.get(prov.kind) or p.hub(), caps, auth=p.auth(), timeout_s=420)
    except WebDriverError as e:
        raise Abort(redact(str(e))[:1500])
    return AppiumDriver(s, web=web, platform=platform, app_id=tgt.get("app_package", ""), secrets=secrets,
                        provider=p, check_url=url_checker(pol) if web and pol else None)


def save_obs(d: Path, name: str, obs: Observation) -> str:
    ext = "jpg" if obs.mime == "image/jpeg" else "png"
    p = d / f"{name}.{ext}"
    p.write_bytes(obs.image)
    os.chmod(p, 0o640)
    return rel(p)


def save_shot(d: Path, name: str, data: bytes, mime: str) -> str:
    p = d / f"{name}.{'jpg' if mime == 'image/jpeg' else 'png'}"
    p.write_bytes(data)
    os.chmod(p, 0o640)
    return rel(p)


# --------------------------------------------------------------------------- runs
def claim(worker_id: str, emulator_free: bool) -> int | None:
    with session_scope() as db:
        rows = db.scalars(select(MobileRun).join(Tenant, Tenant.id == MobileRun.tenant_id)
                          .where(MobileRun.status == "queued", Tenant.status == "active")
                          .order_by(MobileRun.created_at, MobileRun.id).with_for_update(of=MobileRun, skip_locked=True)
                          .limit(10)).all()
        for run in rows:
            if run.cancel_requested:
                run.status, run.finished_at, run.error = "cancelled", utcnow(), "Cancelled before it started"
                continue
            if (run.device or {}).get("provider_kind") == "emulator" and not emulator_free:
                continue
            run.status, run.started_at, run.heartbeat_at, run.worker_id = "running", utcnow(), utcnow(), worker_id
            return run.id
    return None


def execute(run_id: int) -> None:
    status, error, results = "error", "", []
    driver: Driver | None = None
    try:
        with session_scope() as db:
            run = db.get(MobileRun, run_id)
            project, tenant = db.get(Project, run.project_id), db.get(Tenant, run.tenant_id)
            if not tenant or tenant.status != "active":
                raise Abort("The organization is not active; the run was not executed")
            if not project or project.archived:
                raise Abort("The project is no longer available")
            if run.requested_by:
                u = db.get(User, run.requested_by)
                if not u or not u.is_active or u.tenant_id != run.tenant_id or not access.has(db, u, project.id, "run.execute"):
                    raise Abort("The person who started this run no longer has permission to run tests in this project")
            prow = db.get(MobileProvider, run.provider_id) if run.provider_id else None
            if not prow or prow.tenant_id != run.tenant_id:
                raise Abort("The device provider was removed")
            prov = provider_snapshot(prow)
            snap = dict(run.case_snapshot or {})
            steps = validate_steps(snap.get("steps") or [])
            app = app_snapshot(db.get(MobileApp, snap["app_id"])) if snap.get("app_id") else None
            if app and app.tenant_id != run.tenant_id:
                app = None
            web = snap.get("kind") == "web"
            pol = web_policy(db, project) if web else None
            secrets = load_secrets(db, run.tenant_id, project.id, secret_names(steps))
            names = {"project": project.name, "build": f"TestAgent run group {run.group_id}",
                     "session": f"{snap.get('name', 'case')} #{run.id}"}
            target = {"kind": snap.get("kind", "native"), "platform": snap.get("platform", "android")}
            device = dict(run.device or {})
            rd = run_dir(run)
        mode = get_settings().screenshot_mode
        driver = open_driver(prov, device, target, app, names=names, secrets=secrets, pol=pol,
                             video_dir=str(rd / "video_tmp") if prov.kind == "emulator" and get_settings().capture_video else None)
        with session_scope() as db:
            run = db.get(MobileRun, run_id)
            run.session_id = getattr(driver, "session_id", "") or ""
        deadline = time.monotonic() + RUN_TIMEOUT_S
        start_url = snap.get("start_url", "")
        if web and start_url and (not steps or steps[0]["action"] not in ("open_url", "launch_app")):
            try:
                driver.perform({"action": "open_url", "url": start_url}, start_url)
            except StepError as e:
                raise Abort(f"Could not open the start URL: {e}")
        status = "passed"
        for i, s in enumerate(steps):
            if _cancelled(run_id):
                status, error = "cancelled", "Cancelled by a user"
                break
            if time.monotonic() > deadline:
                status, error = "error", f"The run exceeded {RUN_TIMEOUT_S // 60} minutes"
                break
            t0 = time.monotonic()
            r = {"i": i, "action": s["action"], "description": describe(s), "status": "passed", "error": "", "note": "", "shot": ""}
            try:
                r["note"] = driver.perform(s, start_url)
            except StepError as e:
                r["status"], r["error"] = "failed", redact(str(e))[:1500]
            r["ms"] = int((time.monotonic() - t0) * 1000)
            if r["status"] == "failed" or mode == "each_step" or s["action"] == "screenshot":
                try:
                    img, mime = driver.screenshot()
                    r["shot"] = save_shot(rd, f"step-{i + 1:03d}", img, mime)
                except Exception as e:  # the device may be gone
                    r["note"] = (r["note"] + f" (no screenshot: {type(e).__name__})").strip()
            results.append(r)
            _progress(run_id, results)
            if r["status"] == "failed":
                status, error = "failed", f"Step {i + 1} failed: {r['error'][:500]}"
                break
        for j in range(len(results), len(steps)):
            results.append({"i": j, "action": steps[j]["action"], "description": describe(steps[j]), "status": "skipped",
                            "error": "", "note": "", "shot": "", "ms": 0})
    except Abort as a:
        status, error = "error", str(a)
    except Exception as e:
        log.exception("mobile run %s crashed", run_id)
        status, error = "error", f"Internal error: {type(e).__name__}: {redact(str(e))[:500]}"
    finally:
        session_url, video = "", None
        if driver is not None:
            if isinstance(driver, EmulatorDriver):
                video = driver.video_path()
            try:
                driver.close(status == "passed" if status in ("passed", "failed") else False, error or "passed")
            except Exception:
                pass
            if isinstance(driver, AppiumDriver) and driver.provider is not None:
                session_url = driver.provider.session_url(driver.session_id, driver.web)
        with session_scope() as db:
            run = db.get(MobileRun, run_id)
            if video and Path(video).exists():
                d = run_dir(run)
                shutil.move(video, d / "video.webm")
                os.chmod(d / "video.webm", 0o640)
            shutil.rmtree(run_dir(run) / "video_tmp", ignore_errors=True)
            run.status, run.error, run.finished_at = status, error, utcnow()
            if results:
                run.step_results = results
            if session_url:
                run.session_url = session_url[:1000]


def _cancelled(run_id: int) -> bool:
    with session_scope() as db:
        return bool(db.scalar(select(MobileRun.cancel_requested).where(MobileRun.id == run_id)))


def _progress(run_id: int, results: list) -> None:
    with session_scope() as db:
        db.execute(update(MobileRun).where(MobileRun.id == run_id).values(step_results=list(results), heartbeat_at=utcnow()))


# --------------------------------------------------------------------------- live sessions (recorder / AI agent)
def claim_session(worker_id: str, emulator_free: bool) -> int | None:
    with session_scope() as db:
        rows = db.scalars(select(MobileSession).where(MobileSession.status == "starting", MobileSession.worker_id == "")
                          .order_by(MobileSession.id).with_for_update(skip_locked=True).limit(5)).all()
        for s in rows:
            if (s.device or {}).get("provider_kind") == "emulator" and not emulator_free:
                continue
            s.worker_id, s.heartbeat_at = worker_id, utcnow()
            return s.id
    return None


def _set_res(sid: int, res: dict, **fields) -> None:
    with session_scope() as db:
        db.execute(update(MobileSession).where(MobileSession.id == sid).values(res=res, heartbeat_at=utcnow(), **fields))


def _observe(driver: Driver, d: Path, seq: int) -> dict:
    obs = driver.observe()
    shot = save_obs(d, f"screen-{seq:05d}", obs)
    for old in d.glob("screen-*"):
        try:
            if int(old.stem.split("-")[1]) < seq - 3:
                old.unlink()
        except (ValueError, OSError):
            pass
    return {"shot": shot, "mime": obs.mime, **obs.meta()}


def live(sid: int, stop: threading.Event) -> None:
    driver: Driver | None = None
    final, err = "closed", ""
    started = time.monotonic()
    try:
        with session_scope() as db:
            s = db.get(MobileSession, sid)
            project = db.get(Project, s.project_id)
            prow = db.get(MobileProvider, s.provider_id) if s.provider_id else None
            if not prow or prow.tenant_id != s.tenant_id or not project or project.archived:
                raise Abort("The device provider or project is no longer available")
            prov = provider_snapshot(prow)
            target = dict(s.target or {})
            app = app_snapshot(db.get(MobileApp, target["app_id"])) if target.get("app_id") else None
            if app and app.tenant_id != s.tenant_id:
                app = None
            web = target.get("kind") == "web"
            pol = web_policy(db, project) if web else None
            tenant_id, project_id = s.tenant_id, s.project_id
            names = {"project": project.name, "build": "TestAgent live sessions",
                     "session": ("AI agent" if s.purpose == "ai" else "Recorder") + f" #{s.id}"}
            device = dict(s.device or {})
            d = session_dir(s)
        driver = open_driver(prov, device, target, app, names=names, secrets={}, pol=pol)
        start_url = target.get("start_url", "")
        if web and start_url:
            try:
                driver.perform({"action": "open_url", "url": start_url}, start_url)
            except StepError as e:
                raise Abort(f"Could not open the start URL: {e}")
        url = ""
        if isinstance(driver, AppiumDriver) and driver.provider is not None:
            url = driver.provider.session_url(driver.session_id, driver.web)
        _set_res(sid, {"seq": 0, "ok": True, "obs": _observe(driver, d, 0)}, status="ready",
                 session_id=getattr(driver, "session_id", "") or "", session_url=url[:1000])
        last_beat, done = time.monotonic(), 0
        while not stop.is_set():
            with session_scope() as db:
                s = db.get(MobileSession, sid)
                status, seq, cmd, last_used = s.status, s.cmd_seq, dict(s.cmd or {}), s.last_used_at
            if status != "ready":
                break
            if seq > done:
                done = seq
                if cmd.get("op") == "close":
                    break
                res = {"seq": seq, "ok": True, "error": "", "note": ""}
                if cmd.get("op") == "perform":
                    try:
                        st = validate_steps([cmd.get("step")])[0]
                        with session_scope() as db:
                            driver.secrets = load_secrets(db, tenant_id, project_id, secret_names([st]))
                        res["note"] = driver.perform(st, start_url)
                        res["step"] = st
                    except (StepError, Abort, ValueError) as e:
                        res.update(ok=False, error=redact(str(e))[:1000])
                    finally:
                        driver.secrets = {}
                try:
                    res["obs"] = _observe(driver, d, seq)
                except Exception as e:
                    res.update(ok=False, error=(res.get("error") or "") + f" (screen not available: {type(e).__name__})")
                _set_res(sid, res)
                continue
            if utcnow() - last_used > timedelta(minutes=IDLE_MINUTES):
                final, err = "closed", f"Closed after {IDLE_MINUTES} minutes without activity"
                break
            if time.monotonic() - started > MAX_SESSION_MINUTES * 60:
                final, err = "closed", f"Closed after the {MAX_SESSION_MINUTES}-minute session limit"
                break
            if time.monotonic() - last_beat > 10:
                last_beat = time.monotonic()
                with session_scope() as db:
                    db.execute(update(MobileSession).where(MobileSession.id == sid).values(heartbeat_at=utcnow()))
            stop.wait(0.3)
    except Abort as a:
        final, err = "error", str(a)
    except Exception as e:
        log.exception("live mobile session %s crashed", sid)
        final, err = "error", f"Internal error: {type(e).__name__}: {redact(str(e))[:500]}"
    finally:
        if driver is not None:
            try:
                driver.close(None)
            except Exception:
                pass
        with session_scope() as db:
            s = db.get(MobileSession, sid)
            if s:
                if s.status != "closed" or final == "error":
                    s.status = final
                if err and not s.error:
                    s.error = err
                s.closed_at = utcnow()
                if s.purpose == "ai" and s.phase == "running" and final == "error":
                    s.phase = "failed"


# --------------------------------------------------------------------------- dispatcher
class Host:
    def __init__(self, worker_id: str, stop: threading.Event):
        self.worker_id, self.stop = worker_id, stop
        self.runs: dict[int, tuple[threading.Thread, bool]] = {}
        self.sessions: dict[int, tuple[threading.Thread, bool]] = {}

    def _emulator_busy(self) -> bool:
        return any(emu for t, emu in [*self.runs.values(), *self.sessions.values()] if t.is_alive())

    def _kind(self, model, oid: int) -> bool:
        with session_scope() as db:
            o = db.get(model, oid)
            return bool(o and (o.device or {}).get("provider_kind") == "emulator")

    def loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.runs = {k: v for k, v in self.runs.items() if v[0].is_alive()}
                self.sessions = {k: v for k, v in self.sessions.items() if v[0].is_alive()}
                if len(self.sessions) < MAX_LIVE:
                    sid = claim_session(self.worker_id, not self._emulator_busy())
                    if sid:
                        t = threading.Thread(target=live, args=(sid, self.stop), name=f"mobile-live-{sid}", daemon=True)
                        self.sessions[sid] = (t, self._kind(MobileSession, sid))
                        t.start()
                        continue
                if len(self.runs) < PARALLEL_RUNS:
                    rid = claim(self.worker_id, not self._emulator_busy())
                    if rid:
                        t = threading.Thread(target=execute, args=(rid,), name=f"mobile-run-{rid}", daemon=True)
                        self.runs[rid] = (t, self._kind(MobileRun, rid))
                        t.start()
                        continue
                if self.runs:
                    with session_scope() as db:
                        db.execute(update(MobileRun).where(MobileRun.id.in_(list(self.runs)), MobileRun.status == "running")
                                   .values(heartbeat_at=utcnow()))
            except Exception:
                log.exception("mobile dispatcher error")
                self.stop.wait(5)
            self.stop.wait(1.0)
        for t, _ in [*self.runs.values(), *self.sessions.values()]:
            t.join(timeout=30)


def start(stop: threading.Event, worker_id: str) -> threading.Thread:
    h = Host(worker_id, stop)
    t = threading.Thread(target=h.loop, name="mobile-host", daemon=True)
    t.start()
    return t


def reap_stale() -> int:
    s = get_settings()
    cutoff = utcnow() - timedelta(minutes=max(s.stale_run_minutes, 3))
    with session_scope() as db:
        n = db.execute(update(MobileRun).where(MobileRun.status == "running",
                                               func.coalesce(MobileRun.heartbeat_at, MobileRun.started_at) < cutoff)
                       .values(status="error", finished_at=utcnow(),
                               error="Worker stopped responding; the run was abandoned. Run it again.")).rowcount or 0
        n += db.execute(update(MobileSession).where(MobileSession.status.in_(("starting", "ready")), MobileSession.worker_id != "",
                                                    func.coalesce(MobileSession.heartbeat_at, MobileSession.created_at) < cutoff)
                        .values(status="error", closed_at=utcnow(), error="The device session was lost (worker restarted)")).rowcount or 0
        db.execute(update(MobileSession).where(MobileSession.status == "starting", MobileSession.worker_id == "",
                                               MobileSession.created_at < utcnow() - timedelta(minutes=15))
                   .values(status="error", closed_at=utcnow(), error="No device host picked up the session"))
        db.execute(update(MobileSession).where(MobileSession.phase == "running",
                                               MobileSession.last_used_at < utcnow() - timedelta(minutes=10))
                   .values(phase="failed", error="The AI agent stopped responding"))
    return n


def purge_old() -> None:
    """Live-session screens are temporary; recorded steps live on in the cases."""
    cutoff = utcnow() - timedelta(days=1)
    with session_scope() as db:
        rows = db.scalars(select(MobileSession).where(MobileSession.status.in_(("closed", "error")),
                                                      MobileSession.closed_at < cutoff).limit(500)).all()
        for s in rows:
            shutil.rmtree(storage.root() / "tenants" / str(s.tenant_id) / "projects" / str(s.project_id) / "mobile" / "sessions" / str(s.id),
                          ignore_errors=True)
