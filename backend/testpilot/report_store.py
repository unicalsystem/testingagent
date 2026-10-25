"""Copies every finished run's report into a MySQL database configured in config.local.

PostgreSQL stays the application's system of record. MySQL holds a complete, query-friendly
copy of each report: one row per run (summary + full JSON + self-contained HTML) and one row
per step. Pushes are idempotent upserts keyed on run_id, so retries and backfills are safe.
If MySQL is unreachable the run still completes; the worker retries later.
"""
from __future__ import annotations

import logging
import ssl
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import (
    BigInteger, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select,
)
from sqlalchemy.dialects.mysql import LONGTEXT, insert as mysql_insert
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from . import report
from .config import get_settings
from .models import TERMINAL, Run

log = logging.getLogger(__name__)


class ReportStoreError(RuntimeError):
    def __init__(self, msg: str, connection: bool = False):
        super().__init__(msg)
        self.connection = connection  # True: MySQL unreachable/auth failed -> stop this cycle


# PyMySQL/MySQL client error codes meaning "the server is not usable right now".
_CONNECTION_CODES = {1040, 1045, 1049, 2002, 2003, 2005, 2006, 2013, 2026}


def _is_connection_error(e: Exception) -> bool:
    a = getattr(e, "orig", e)
    args = getattr(a, "args", ())
    return bool(args) and isinstance(args[0], int) and args[0] in _CONNECTION_CODES


def enabled() -> bool:
    return get_settings().mysql_reports_enabled


@lru_cache
def _tables() -> tuple[MetaData, Table, Table]:
    pre = get_settings().mysql_reports_table_prefix
    md = MetaData()
    runs = Table(
        f"{pre}reports", md,
        Column("run_id", Integer, primary_key=True, autoincrement=False),
        Column("project_id", Integer, nullable=False, index=True),
        Column("project_name", String(120)),
        Column("base_url", String(2048)),
        Column("test_case_id", Integer, index=True),
        Column("test_case_name", String(200)),
        Column("status", String(16), nullable=False, index=True),
        Column("requested_by", String(80)),
        Column("created_at", DateTime),
        Column("started_at", DateTime),
        Column("finished_at", DateTime, index=True),
        Column("duration_ms", BigInteger),
        Column("steps_total", Integer),
        Column("steps_passed", Integer),
        Column("steps_failed", Integer),
        Column("steps_skipped", Integer),
        Column("blocked_requests", Integer),
        Column("error", Text),
        Column("ai_analysis", Text),
        Column("report_url", String(500)),
        Column("report_json", LONGTEXT),
        Column("report_html", LONGTEXT),
        Column("report_html_omitted", Integer, default=0),
        Column("synced_at", DateTime, nullable=False),
        mysql_charset="utf8mb4", mysql_engine="InnoDB",
    )
    steps = Table(
        f"{pre}report_steps", md,
        Column("run_id", Integer, primary_key=True, autoincrement=False),
        Column("step_index", Integer, primary_key=True, autoincrement=False),
        Column("action", String(32)),
        Column("description", String(500)),
        Column("status", String(16)),
        Column("attempts", Integer),
        Column("duration_ms", Integer),
        Column("error", Text),
        Column("screenshot_sha256", String(64)),
        mysql_charset="utf8mb4", mysql_engine="InnoDB",
    )
    return md, runs, steps


@lru_cache
def engine() -> Engine:
    s = get_settings()
    if not s.mysql_reports_enabled:
        raise ReportStoreError("MySQL report store is disabled")
    url = URL.create(
        "mysql+pymysql",
        username=s.mysql_reports_user,
        password=s.mysql_reports_password.get_secret_value(),
        host=s.mysql_reports_host,
        port=s.mysql_reports_port,
        database=s.mysql_reports_database,
        query={"charset": "utf8mb4"},
    )
    args: dict = {"connect_timeout": 10, "read_timeout": 60, "write_timeout": 60}
    if s.mysql_reports_ssl != "disabled":
        ctx = ssl.create_default_context(cafile=s.mysql_reports_ssl_ca or None)
        if s.mysql_reports_ssl == "required":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        args["ssl"] = ctx
    return create_engine(url, connect_args=args, pool_pre_ping=True, pool_recycle=600, pool_size=2, max_overflow=2)


def ensure_schema() -> None:
    md, _, _ = _tables()
    try:
        md.create_all(engine())  # CREATE TABLE IF NOT EXISTS; never drops or alters
    except Exception as e:
        raise ReportStoreError(_describe(e), connection=True) from e


def check() -> dict:
    """Connectivity + schema check used by the CLI and /api/system/status."""
    s = get_settings()
    if not s.mysql_reports_enabled:
        return {"enabled": False}
    try:
        ensure_schema()
        _, runs, _ = _tables()
        with engine().connect() as c:
            version = c.exec_driver_sql("SELECT VERSION()").scalar()
            count = c.execute(select(runs.c.run_id).limit(1)).first() is not None
        return {"enabled": True, "ok": True, "server": version, "host": s.mysql_reports_host,
                "database": s.mysql_reports_database, "has_reports": count}
    except Exception as e:
        return {"enabled": True, "ok": False, "host": s.mysql_reports_host,
                "database": s.mysql_reports_database, "error": _describe(e)}


def _describe(e: Exception) -> str:
    # Never include the password; PyMySQL errors are (code, message) tuples.
    a = getattr(e, "orig", e)
    parts = getattr(a, "args", ())
    if len(parts) >= 2 and isinstance(parts[0], int):
        return f"MySQL error {parts[0]}: {str(parts[1])[:300]}"
    return f"{type(a).__name__}: {str(a)[:300]}"


def _naive(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(timezone.utc).replace(tzinfo=None)  # stored as UTC


def push(db: Session, run: Run) -> None:
    """Upsert one finished run (and its steps) into MySQL."""
    if run.status not in TERMINAL:
        raise ReportStoreError(f"run {run.id} is not finished")
    s = get_settings()
    _, runs, steps = _tables()
    data = report.build_data(db, run)
    html = report.to_html(db, data).decode()
    json_text = report.to_json(data).decode()
    # The whole row must fit in MySQL's max_allowed_packet (16 MiB by default on MariaDB,
    # often 16-64 MiB on hosting), so the cap applies to HTML + JSON together.
    omitted = len(html.encode()) + len(json_text.encode()) > s.mysql_reports_max_html_mb * 1024 * 1024
    r, summ = data["run"], data["run"].get("summary") or {}
    shots = {e["id"]: e for e in data["evidence"] if e["kind"] == "screenshot"}
    row = {
        "run_id": run.id,
        "project_id": run.project_id,
        "project_name": (data["project"] or {}).get("name"),
        "base_url": (data["project"] or {}).get("base_url"),
        "test_case_id": run.test_case_id,
        "test_case_name": data["test_case"]["name"],
        "status": run.status,
        "requested_by": r["requested_by"],
        "created_at": _naive(r["created_at"]),
        "started_at": _naive(r["started_at"]),
        "finished_at": _naive(r["finished_at"]),
        "duration_ms": r["duration_ms"],
        "steps_total": summ.get("total", len(data["steps"])),
        "steps_passed": summ.get("passed", 0),
        "steps_failed": summ.get("failed", 0),
        "steps_skipped": summ.get("skipped", 0),
        "blocked_requests": summ.get("blocked_requests", 0),
        "error": run.error or None,
        "ai_analysis": run.ai_analysis or None,
        "report_url": f"{s.public_url}/runs/{run.id}",
        "report_json": json_text,
        "report_html": None if omitted else html,
        "report_html_omitted": 1 if omitted else 0,
        "synced_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    step_rows = []
    for st in data["steps"]:
        shot = next((shots[e["id"]]["sha256"] for e in st["evidence"] if e["id"] in shots), None)
        step_rows.append({"run_id": run.id, "step_index": st["index"], "action": st["action"],
                          "description": (st["description"] or "")[:500], "status": st["status"],
                          "attempts": st["attempts"], "duration_ms": st["duration_ms"],
                          "error": st["error"] or None, "screenshot_sha256": shot})
    try:
        ensure_schema()
        with engine().begin() as c:  # one transaction: report and its steps land together
            ins = mysql_insert(runs).values(**row)
            c.execute(ins.on_duplicate_key_update({k: ins.inserted[k] for k in row if k != "run_id"}))
            c.execute(steps.delete().where(steps.c.run_id == run.id))
            if step_rows:
                c.execute(steps.insert(), step_rows)
    except ReportStoreError:
        raise
    except Exception as e:
        raise ReportStoreError(_describe(e), connection=_is_connection_error(e)) from e


def sync_pending(limit: int = 50) -> tuple[int, int]:
    """Push finished runs that are not yet synced (or changed since). Returns (ok, failed)."""
    from .db import session_scope

    if not enabled():
        return 0, 0
    ok = failed = 0
    with session_scope() as db:
        # Never-attempted runs first, then earlier failures, so one bad report cannot starve the rest.
        ids = db.scalars(select(Run.id).where(Run.status.in_(TERMINAL), Run.report_synced_at.is_(None))
                         .order_by(Run.report_sync_error != "", Run.id).limit(limit)).all()
    for rid in ids:
        with session_scope() as db:
            run = db.get(Run, rid)
            try:
                push(db, run)
                run.report_synced_at, run.report_sync_error = datetime.now(timezone.utc), ""
                ok += 1
            except ReportStoreError as e:
                run.report_sync_error = str(e)[:1000]
                failed += 1
                log.warning("MySQL report sync failed for run %s: %s", rid, e)
                if e.connection:
                    break  # MySQL is down: stop and retry on the next cycle
                # otherwise a problem with this report only: carry on with the next run
    return ok, failed
