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

from .. import __version__, ai, logging_setup, report_store
from ..config import get_settings
from ..db import get_db
from ..models import Run, User, WorkerHeartbeat, utcnow
from . import routes_admin, routes_auth, routes_projects, routes_runs
from .deps import csrf_guard, require

log = logging.getLogger("testpilot.api")


def create_app() -> FastAPI:
    s = get_settings()
    logging_setup.setup("api")
    for p in s.problems():
        log.error("configuration problem: %s", p)
    if s.problems() and s.app_env == "production":
        raise RuntimeError("Refusing to start with an unsafe production configuration: " + "; ".join(s.problems()))

    docs = s.app_env != "production"
    app = FastAPI(title="TestPilot AI", version=__version__,
                  docs_url="/api/docs" if docs else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if docs else None)
    public_host = urlsplit(s.public_url).hostname or "localhost"
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=[public_host, "localhost", "127.0.0.1", "api", "testserver"])

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
    def system_status(db: Session = Depends(get_db), _: User = Depends(require("viewer"))):
        cutoff = utcnow() - timedelta(seconds=60)
        workers = db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen >= cutoff)).all()
        q = dict(db.execute(select(Run.status, func.count()).where(Run.status.in_(("queued", "running")))
                            .group_by(Run.status)).all())
        return {
            "version": __version__,
            "environment": s.app_env,
            "public_url": s.public_url,
            "ai": ai.status(),
            "workers": [{"id": w.worker_id, "last_seen": w.last_seen, "current_run_id": w.current_run_id} for w in workers],
            "queue": {"queued": q.get("queued", 0), "running": q.get("running", 0)},
            "report_store": {
                "enabled": report_store.enabled(),
                "pending": db.scalar(select(func.count()).select_from(Run).where(
                    Run.status.in_(("passed", "failed", "error", "cancelled")), Run.report_synced_at.is_(None))) or 0,
                "last_error": db.scalar(select(Run.report_sync_error).where(Run.report_sync_error != "")
                                        .order_by(Run.id.desc()).limit(1)) or "",
            } if report_store.enabled() else {"enabled": False},
            "limits": {"max_concurrent_runs": s.max_concurrent_runs, "run_timeout_seconds": s.run_timeout_seconds,
                       "step_timeout_ms": s.step_timeout_ms, "max_step_retries": s.max_step_retries},
        }

    for r in (routes_auth.router, routes_admin.router, routes_projects.router, routes_runs.router):
        api.include_router(r)
    app.include_router(api)
    return app


app = create_app()
