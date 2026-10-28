"""FastAPI application. Run: uvicorn testpilot.api.app:app"""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__, ai, logging_setup, report_store, storage_policy
from ..config import get_settings
from ..db import get_db
from ..models import Run, User, WorkerHeartbeat, utcnow
from . import (routes_access, routes_admin, routes_auth, routes_oidc, routes_platform, routes_privacy, routes_projects,
               routes_automation, routes_public, routes_runs, routes_storage, routes_testmgmt,
               routes_apitest, routes_monitoring, routes_integrations, routes_mobile, routes_billing)
from .deps import csrf_guard, require_perm

log = logging.getLogger("testpilot.api")


def create_app() -> FastAPI:
    s = get_settings()
    logging_setup.setup("api")
    for p in s.problems():
        log.error("configuration problem: %s", p)
    if s.problems() and s.app_env == "production":
        raise RuntimeError("Refusing to start with an unsafe production configuration: " + "; ".join(s.problems()))

    docs = s.app_env != "production"
    app = FastAPI(title="TestAgent", version=__version__,
                  docs_url="/api/docs" if docs else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if docs else None)
    public_host = urlsplit(s.public_url).hostname or "localhost"
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=[public_host, *{urlsplit(o).hostname for o in s.origins if urlsplit(o).hostname},
                                      "localhost", "127.0.0.1", "api", "testserver"])

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors()[:8]:
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else e.get("msg"))
        return JSONResponse({"detail": "; ".join(msgs)}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    api = APIRouter(prefix="/api", dependencies=[Depends(csrf_guard)])

    @api.get("/health", tags=["system"])
    def health():
        """Liveness: the process is up. No dependencies checked."""
        return {"status": "ok", "version": __version__}

    @api.get("/ready", tags=["system"])
    def ready(db: Session = Depends(get_db)):
        """Readiness: database reachable and migrated."""
        try:
            db.execute(text("SELECT 1 FROM alembic_version"))
        except Exception:
            return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    @api.get("/system/status", tags=["system"])
    def system_status(db: Session = Depends(get_db), me: User = Depends(require_perm("tenant.member"))):
        cutoff = utcnow() - timedelta(seconds=60)
        workers = db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen >= cutoff)).all()
        # Queue and report-store figures are this tenant's only; workers are shared but anonymised.
        q = dict(db.execute(select(Run.status, func.count()).where(Run.tenant_id == me.tenant_id,
                                                                  Run.status.in_(("queued", "running")))
                            .group_by(Run.status)).all())
        return {
            "version": __version__,
            "environment": s.app_env,
            "public_url": s.public_url,
            "default_tenant": s.default_tenant_slug,
            "ai": ai.status(),
            "workers": [{"id": f"worker-{i + 1}", "last_seen": w.last_seen} for i, w in enumerate(workers)],
            "queue": {"queued": q.get("queued", 0), "running": q.get("running", 0)},
            "report_store": {
                "enabled": report_store.enabled(),
                "pending": db.scalar(select(func.count()).select_from(Run).where(
                    Run.tenant_id == me.tenant_id,
                    Run.status.in_(("passed", "failed", "error", "cancelled")), Run.report_synced_at.is_(None))) or 0,
                "last_error": db.scalar(select(Run.report_sync_error).where(Run.tenant_id == me.tenant_id,
                                                                           Run.report_sync_error != "")
                                        .order_by(Run.id.desc()).limit(1)) or "",
            } if report_store.enabled() else {"enabled": False},
            "storage": _storage_summary(db, me.tenant_id),
            "limits": {"max_concurrent_runs": s.max_concurrent_runs, "run_timeout_seconds": s.run_timeout_seconds,
                       "step_timeout_ms": s.step_timeout_ms, "max_step_retries": s.max_step_retries},
        }

    def _storage_summary(db: Session, tenant_id: int) -> dict:
        r = storage_policy.row(db, tenant_id)
        db.commit()
        return {"mode": r.mode, "provider": r.provider, "status": r.status, "ai_enabled": r.ai_enabled,
                "run_block_reason": storage_policy.run_block_reason(db, tenant_id),
                "consent_legacy": r.mode == "unical" and r.unical_consent_source == "legacy"}

    tenant_routers = (routes_auth.router, routes_oidc.scoped, routes_admin.router, routes_access.router,
                      routes_projects.router, routes_runs.router, routes_testmgmt.router, routes_automation.router, routes_storage.router, routes_privacy.scoped,
                      routes_apitest.router, routes_monitoring.router, routes_integrations.router, routes_mobile.router, routes_billing.router)
    # Tenant-scoped API: /api/t/<slug>/...  (tenant taken from the URL, checked against the session)
    scoped = APIRouter(prefix="/t/{tenant_slug}")
    scoped.add_api_route("/system/status", system_status, methods=["GET"], tags=["system"])
    for r in tenant_routers:
        scoped.include_router(r)
    # Release-1.0 API: /api/...  == the default tenant. Kept for existing clients and bookmarks.
    for r in tenant_routers:
        api.include_router(r)
    api.include_router(scoped)
    api.include_router(routes_public.router)
    api.include_router(routes_mobile.public)
    api.include_router(routes_billing.public)  # price list, Razorpay webhook
    api.include_router(routes_billing.platform)  # signed app downloads for Appium servers
    api.include_router(routes_oidc.public)
    api.include_router(routes_privacy.public)
    api.include_router(routes_platform.router)
    api.include_router(routes_storage.platform)
    api.include_router(routes_privacy.platform)
    app.include_router(api)
    return app


app = create_app()
