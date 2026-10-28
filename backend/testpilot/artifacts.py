"""Artifact placement for organizations that use their own storage (SharePoint / Google Drive).

Flow for a run in external mode:
  1. The worker writes screenshots, video, trace, log, test-case snapshot and reports into a spool
     directory (storage.work_dir) — never into the backed-up evidence area.
  2. upload_run() uploads every spooled file to
        <base>/<project>/<run>/{test-cases,evidence,reports}/
     verifies it, records the external reference, and deletes the local copy.
  3. When everything is uploaded, step error texts and the AI analysis are removed from PostgreSQL
     (details_external); the full detail lives only in the organization's report.
  4. If the store is unavailable the files wait in the spool (never copied to Unical storage) and are
     retried; after `spool_max_hours` they are deleted and recorded as lost.

Test-case definitions are written as versioned JSON files (case_versions) the same way.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, or_, select

from . import settings_store, storage, storage_policy
from .db import get_engine, session_scope
from .models import AuditLog, CaseVersion, Evidence, Project, Run, TenantStorage, TestCase, utcnow
from .storage_providers import Provider, StorageError

log = logging.getLogger("testpilot.artifacts")


def _max_bytes(db) -> int:
    return int(storage_policy.platform(db)["max_artifact_mb"]) * 1024 * 1024


def purge_details(run: Run) -> None:
    """Keep only operational metadata in PostgreSQL once the full report lives externally."""
    for s in run.steps:
        s.error = ""
    if run.error:
        head = re.split(r":\s", run.error, maxsplit=1)[0][:200]
        run.error = f"{head}. Details are in the report in your organization's storage."
    run.ai_analysis = ""
    run.details_external = True
    run.report_synced_at = utcnow()  # never pushed to the MySQL report copy
    run.report_sync_error = ""


UPLOAD_LOCK = 74_220_101  # advisory-lock namespace: one uploader per run across all workers


@contextmanager
def _run_lock(run_id: int):
    """Session-level PostgreSQL advisory lock (released automatically if the process dies)."""
    with get_engine().connect() as c:
        got = c.execute(select(func.pg_try_advisory_lock(UPLOAD_LOCK, run_id))).scalar()
        c.commit()
        try:
            yield bool(got)
        finally:
            if got:
                c.execute(select(func.pg_advisory_unlock(UPLOAD_LOCK, run_id)))
                c.commit()


def _spool_dir(run: Run) -> Path:
    return storage.spool_root() / "tenants" / str(run.tenant_id) / "runs" / str(run.id)


def upload_run(run_id: int) -> bool:
    """Uploads a run's pending artifacts. Returns True when nothing is left pending."""
    with _run_lock(run_id) as mine:
        if not mine:
            return False  # another worker is uploading this run right now
        return _upload_run(run_id)


def _upload_run(run_id: int) -> bool:
    with session_scope() as db:
        run = db.get(Run, run_id)
        if not run or run.storage_mode != "external":
            return True
        r = storage_policy.row(db, run.tenant_id)
        project = db.get(Project, run.project_id)
        if r.mode != "external" or r.provider != run.storage_provider:
            # Never fall back to Unical storage or redirect to a different provider: the files wait
            # (and expire) in the spool until the store this run was meant for is configured again.
            run.upload_state = "pending"
            log.warning("run %s: the organization's storage changed since the run; artifacts stay pending", run_id)
            return False
        try:
            prov = storage_policy.provider_for(db, r)
            f = storage_policy.folders(db, r)
            parts = storage_policy.run_parts(f, project, run.id)
        except StorageError as e:
            _fail_health(r, e)
            run.upload_state = "pending"
            return False
        limit = _max_bytes(db)
        ids = [e.id for e in _pending(db, run)]
    ok = _upload_evidence(run_id, ids, prov, f, parts, limit)
    with session_scope() as db:
        run = db.get(Run, run_id)
        r = storage_policy.row(db, run.tenant_id)
        left = _pending(db, run)
        if left:
            run.upload_state = "pending"
            return False
        lost = db.scalars(select(Evidence.id).where(Evidence.run_id == run.id, Evidence.upload_status == "lost")).first()
        run.upload_state = "failed" if lost else "done"
        purge_details(run)
        if ok:
            r.status, r.last_ok_at, r.last_error = "ok", utcnow(), ""
        d = _spool_dir(run)
    shutil.rmtree(d, ignore_errors=True)
    return True


def _pending(db, run: Run) -> list[Evidence]:
    return list(db.scalars(select(Evidence).where(Evidence.run_id == run.id, Evidence.storage == "spool",
                                                  Evidence.upload_status == "pending").order_by(Evidence.id)).all())


def _fail_health(r: TenantStorage, e: Exception) -> None:
    r.status, r.last_error, r.last_check_at = "error", str(e)[:1000], utcnow()


def _upload_evidence(run_id: int, ids: list[int], prov: Provider, f: dict, parts: list[str], limit: int) -> bool:
    folder_cache: dict[str, str] = {}
    for eid in ids:
        with session_scope() as db:
            ev = db.get(Evidence, eid)
            if not ev or ev.storage != "spool" or ev.upload_status != "pending" or not ev.rel_path:
                continue  # already handled (uploaded, expired or deleted)
            r = storage_policy.row(db, ev.tenant_id)
            try:
                path = storage.resolve_evidence(ev.rel_path)
            except PermissionError:
                ev.upload_status, ev.upload_error = "lost", "invalid spool path"
                continue
            if not path.is_file():
                ev.upload_status, ev.upload_error = "lost", "temporary file missing"
                continue
            if ev.size > limit:
                ev.upload_status, ev.upload_error = "lost", f"larger than the {limit // 1048576} MB artifact limit; not stored"
                path.unlink(missing_ok=True)
                continue
            sub = storage_policy.sub_for_kind(f, ev.kind)
            try:
                if sub not in folder_cache:
                    folder_cache[sub] = prov.ensure_folder([*parts, sub])
                up = prov.upload(folder_cache[sub], path, path.name, ev.mime, ev.sha256)
            except StorageError as e:
                ev.upload_attempts += 1
                ev.upload_error = str(e)[:1000]
                _fail_health(r, e)
                log.warning("upload of evidence %s failed: %s", eid, e)
                return False
            ev.storage, ev.external_ref, ev.upload_status, ev.upload_error = prov.name, up.ref, "uploaded", ""
            ev.external_path = "/".join([prov.base_path(), *parts, sub, path.name]).strip("/")[:1024]
            ev.rel_path = ""
        path.unlink(missing_ok=True)  # only after the database records the verified upload
    return True


# ---- test-case definitions ---------------------------------------------------------------------
_CASE_META = ("external_id", "module", "sub_module", "priority", "scenario_type", "assigned_to", "requirement",
              "preconditions", "test_data", "procedure", "expected_result")


def case_document(c: TestCase, version: int) -> bytes:
    return json.dumps({"format": "testpilot.test_case", "format_version": 1, "version": version,
                       "test_case_id": c.id, "project_id": c.project_id, "name": c.name,
                       "description": c.description, "tags": c.tags, "source": c.source, "steps": c.steps,
                       **{k: getattr(c, k, "") or "" for k in _CASE_META}, "attributes": getattr(c, "attributes", None) or {},
                       "use_setup": bool(getattr(c, "use_setup", False)),
                       "saved_at": utcnow().isoformat(timespec="seconds")}, indent=2, ensure_ascii=False).encode()


def queue_case_version(db, c: TestCase, user_id: int | None) -> CaseVersion | None:
    """Organizations with their own storage: spool a new versioned JSON of the definition for upload."""
    r = storage_policy.row(db, c.tenant_id)
    if r.mode != "external":
        return None
    last = db.scalar(select(CaseVersion.version).where(CaseVersion.test_case_id == c.id)
                     .order_by(CaseVersion.version.desc()).limit(1)) or 0
    v = last + 1
    d = storage.spool_root() / "tenants" / str(c.tenant_id) / "cases" / str(c.id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"v{v}.json"
    p.write_bytes(case_document(c, v))
    cv = CaseVersion(tenant_id=c.tenant_id, project_id=c.project_id, test_case_id=c.id, version=v,
                     size=p.stat().st_size, sha256=storage.sha256_file(p), storage="spool",
                     spool_path=str(p.relative_to(storage.root())), upload_status="pending", created_by=user_id)
    db.add(cv)
    db.flush()
    return cv


def upload_case_versions(limit: int = 50) -> int:
    done = 0
    with session_scope() as db:
        ids = db.scalars(select(CaseVersion.id).where(CaseVersion.upload_status == "pending")
                         .order_by(CaseVersion.id).limit(limit)).all()
    providers: dict[int, tuple[Provider, dict]] = {}
    for cid in ids:
        with session_scope() as db:
            cv = db.get(CaseVersion, cid)
            r = storage_policy.row(db, cv.tenant_id)
            c, project = db.get(TestCase, cv.test_case_id), db.get(Project, cv.project_id)
            if r.mode != "external" or not c or not project:
                continue
            try:
                if cv.tenant_id not in providers:
                    providers[cv.tenant_id] = (storage_policy.provider_for(db, r), storage_policy.folders(db, r))
                prov, f = providers[cv.tenant_id]
                path = storage.resolve_evidence(cv.spool_path)
                if not path.is_file():
                    cv.upload_status, cv.upload_error = "lost", "temporary file missing"
                    continue
                parts = [storage_policy.run_parts(f, project, 0)[0], storage_policy.project_folder(f, project),
                         storage_policy.sub_for_kind(f, "test_case"),
                         storage_policy.clean_name(f"{c.name} ({c.id})", f"case-{c.id}")]
                up = prov.upload(prov.ensure_folder(parts), path, path.name, "application/json", cv.sha256)
            except (StorageError, PermissionError) as e:
                cv.upload_attempts += 1
                cv.upload_error = str(e)[:1000]
                if isinstance(e, StorageError):
                    _fail_health(r, e)
                providers.pop(cv.tenant_id, None)
                continue
            cv.storage, cv.external_ref, cv.upload_status, cv.upload_error = prov.name, up.ref, "uploaded", ""
            cv.external_path = "/".join([prov.base_path(), *parts, path.name]).strip("/")[:1024]
            cv.spool_path = ""
        path.unlink(missing_ok=True)
        done += 1
    return done


# ---- background processing -----------------------------------------------------------------------
BACKOFF_SECONDS = 300   # after a failure, an organization's store is retried at most every 5 minutes
CYCLE_BUDGET = 45       # seconds of upload work per worker-loop cycle, so test runs keep starting


def retry_pending() -> None:
    """Retries pending uploads, expires spooled files that waited too long and sweeps stray spool files.
    Bounded per cycle and backed off per organization so one unreachable store cannot stall the worker."""
    started = time.monotonic()
    with session_scope() as db:
        hours = int(settings_store.get(db, "storage")["spool_max_hours"])
        cutoff = utcnow() - timedelta(hours=hours)
        rows = db.execute(select(Run.id, Run.tenant_id, Run.finished_at).where(Run.upload_state == "pending")
                          .order_by(Run.id).limit(200)).all()
        cooling = {r.tenant_id for r in db.scalars(select(TenantStorage).where(
            TenantStorage.status == "error", TenantStorage.last_check_at > utcnow() - timedelta(seconds=BACKOFF_SECONDS))).all()}
    for rid, tid, finished in rows:
        if finished and finished < cutoff:
            expire_run(rid, hours)
            continue
        if tid in cooling or time.monotonic() - started > CYCLE_BUDGET:
            continue
        try:
            if not upload_run(rid):
                cooling.add(tid)
        except Exception:
            log.exception("retrying uploads for run %s failed", rid)
            cooling.add(tid)
    if time.monotonic() - started < CYCLE_BUDGET:
        try:
            upload_case_versions()
        except Exception:
            log.exception("uploading test-case versions failed")
    with session_scope() as db:  # case versions that waited too long
        for cv in db.scalars(select(CaseVersion).where(CaseVersion.upload_status == "pending", CaseVersion.created_at < cutoff)).all():
            if cv.spool_path:
                try:
                    storage.resolve_evidence(cv.spool_path).unlink(missing_ok=True)
                except PermissionError:
                    pass
            cv.upload_status, cv.spool_path = "lost", ""
            cv.upload_error = (cv.upload_error + f" — discarded after {hours} h").strip(" —")[:1000]
    sweep_spool(hours)


def sweep_spool(hours: int) -> int:
    """Removes spool directories of runs that are finished and no longer waiting (or that do not exist),
    and anything older than the holding period — the spool must never become persistent storage."""
    base = storage.spool_root() / "tenants"
    if not base.exists():
        return 0
    n, now = 0, time.time()
    for d in base.glob("*/runs/*"):
        if not d.is_dir() or not d.name.isdigit():
            continue
        rid = int(d.name)
        with session_scope() as db:
            run = db.get(Run, rid)
            active = run is not None and run.status in ("queued", "running")
            waiting = run is not None and run.upload_state == "pending"
        if active:
            continue
        if waiting and now - d.stat().st_mtime <= hours * 3600:
            continue  # still within the holding period; retry_pending handles it
        if waiting:
            expire_run(rid, hours)
        else:
            with session_scope() as db:  # files nobody will upload any more
                for ev in db.scalars(select(Evidence).where(Evidence.run_id == rid, Evidence.storage == "spool",
                                                            Evidence.upload_status == "pending")).all():
                    ev.upload_status, ev.rel_path, ev.upload_error = "lost", "", "not uploaded (run interrupted)"
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n


def expire_run(run_id: int, hours: int) -> None:
    with _run_lock(run_id) as mine:
        if mine:
            _expire_run(run_id, hours)


def _expire_run(run_id: int, hours: int) -> None:
    with session_scope() as db:
        run = db.get(Run, run_id)
        if run.upload_state != "pending":
            return
        for ev in _pending(db, run):
            ev.upload_status = "lost"
            ev.upload_error = (ev.upload_error or "storage unavailable")[:900] + f" — discarded after {hours} h"
            ev.rel_path = ""
        run.upload_state = "failed"
        purge_details(run)
        db.add(AuditLog(tenant_id=run.tenant_id, scope="tenant", action="storage_artifacts_discarded",
                        target=f"run {run.id}", detail={"run_id": run.id, "hours": hours}))
        d = _spool_dir(run)
    shutil.rmtree(d, ignore_errors=True)


def retention_purge(batch: int = 500, max_batches: int = 20) -> int:
    """Deletes artifacts older than each organization's retention period. External files are deleted only
    when the organization allowed TestAgent to delete what it created, and only in the store it uses now."""
    n = 0
    with session_scope() as db:
        plan = [(r.tenant_id, r.retention_days, r.allow_delete, r.mode, r.provider)
                for r in db.scalars(select(TenantStorage).where(TenantStorage.retention_days > 0)).all()]
    for tid, days, allow_delete, mode, provider in plan:
        cutoff = utcnow() - timedelta(days=days)
        kinds = ["local"] + ([provider] if allow_delete and mode == "external" else [])
        last_id, done_here, prov = 0, 0, None
        for _ in range(max_batches):
            with session_scope() as db:
                evs = db.scalars(select(Evidence).where(
                    Evidence.tenant_id == tid, Evidence.deleted_at.is_(None), Evidence.created_at < cutoff,
                    Evidence.id > last_id, Evidence.storage.in_(kinds),
                    or_(Evidence.storage == "local", Evidence.upload_status == "uploaded")).order_by(Evidence.id).limit(batch)).all()
                if not evs:
                    break
                for ev in evs:
                    last_id = ev.id
                    try:
                        if ev.storage == "local":
                            if ev.rel_path:
                                storage.resolve_evidence(ev.rel_path).unlink(missing_ok=True)
                        else:
                            prov = prov or storage_policy.provider_for(db, storage_policy.row(db, tid))
                            prov.delete(ev.external_ref)
                    except Exception as e:  # one bad item never stops the purge (or the worker)
                        log.warning("retention: could not delete evidence %s: %s", ev.id, e)
                        continue
                    ev.deleted_at = utcnow()
                    done_here += 1
        if done_here:
            with session_scope() as db:
                db.add(AuditLog(tenant_id=tid, scope="tenant", action="retention_purge",
                                target=f"{done_here} artifact(s)", detail={"days": days}))
        n += done_here
    return n


def delete_local_evidence(db, tenant_id: int) -> int:
    """Organization request: remove every artifact held on Unical servers (evidence volume, spool,
    MySQL report copy) and the detailed texts kept in PostgreSQL. Run summaries stay."""
    from . import report_store

    if report_store.enabled():  # first, so a MySQL failure leaves everything untouched
        report_store.delete_tenant(tenant_id)
    n = 0
    for ev in db.scalars(select(Evidence).where(Evidence.tenant_id == tenant_id, Evidence.deleted_at.is_(None),
                                                Evidence.storage.in_(("local", "spool")))).all():
        if ev.rel_path:
            try:
                storage.resolve_evidence(ev.rel_path).unlink(missing_ok=True)
            except (PermissionError, OSError):
                pass
        ev.deleted_at = utcnow()
        if ev.storage == "spool":
            ev.upload_status, ev.rel_path = "lost", ""
        n += 1
    for run in db.scalars(select(Run).where(Run.tenant_id == tenant_id)).all():
        if run.status in ("passed", "failed", "error", "cancelled"):
            for s in run.steps:
                s.error = ""
            run.ai_analysis = ""
            if run.error:
                run.error = re.split(r":\s", run.error, maxsplit=1)[0][:200] + ". Details were deleted at the organization's request."
            if run.upload_state == "pending":
                run.upload_state = "failed"
            run.report_synced_at = utcnow()
    for cv in db.scalars(select(CaseVersion).where(CaseVersion.tenant_id == tenant_id, CaseVersion.upload_status == "pending")).all():
        cv.upload_status, cv.spool_path, cv.upload_error = "lost", "", "deleted at the organization's request"
    r = storage_policy.row(db, tenant_id)
    r.config = {**(r.config or {}), "local_data_deleted_at": utcnow().isoformat()}
    shutil.rmtree(storage.spool_root() / "tenants" / str(tenant_id) / "cases", ignore_errors=True)
    shutil.rmtree(storage.spool_root() / "tenants" / str(tenant_id) / "runs", ignore_errors=True)
    return n


def fetch(db, ev: Evidence) -> bytes:
    """Bytes of one artifact wherever it lives (for authorised downloads)."""
    if ev.deleted_at:
        raise FileNotFoundError("deleted")
    if ev.storage in ("local", "spool"):
        if not ev.rel_path:
            raise FileNotFoundError("not available")
        return storage.resolve_evidence(ev.rel_path).read_bytes()
    r = storage_policy.row(db, ev.tenant_id)
    prov = storage_policy.provider_for(db, r) if r.mode == "external" and r.provider == ev.storage else None
    if prov is None:
        # The organization changed storage since: reach the old store only if it is still configured.
        raise StorageError("This file is in storage that is no longer connected to TestAgent")
    return prov.download(ev.external_ref, _max_bytes(db) + 1)
