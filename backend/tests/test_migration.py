"""Upgrading a populated release-1.0 database (revision 0002) to multi-tenancy (0003) and back.

Uses a separate disposable database: TEST_MIGRATION_DATABASE_URL (skipped when unset)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

URL = os.environ.get("TEST_MIGRATION_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_MIGRATION_DATABASE_URL not set")
BACKEND = Path(__file__).resolve().parents[1]


def _cfg() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", URL)
    return cfg


@pytest.fixture
def legacy_db(monkeypatch):
    eng = create_engine(URL)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    monkeypatch.setenv("DATABASE_URL", URL)
    from testpilot.config import get_settings
    from testpilot.db import get_engine
    get_settings.cache_clear(); get_engine.cache_clear()
    command.upgrade(_cfg(), "0002")
    with eng.begin() as c:
        c.execute(text("""
          INSERT INTO users (id, username, display_name, password_hash, role, is_active, must_change_password, failed_logins, created_at)
          VALUES (1,'admin','Admin','h','admin',true,false,0,now()), (2,'tess','T','h','tester',true,false,0,now()),
                 (3,'vic','V','h','viewer',true,false,0,now());
          INSERT INTO allowed_targets (host, note, created_at) VALUES ('connect.unical.in','', now());
          INSERT INTO projects (id, name, description, base_url, extra_hosts, archived, created_at, updated_at)
          VALUES (1,'Connect','','https://connect.unical.in/','[]',false,now(),now());
          INSERT INTO project_secrets (project_id, name, value_encrypted, updated_at) VALUES (1,'PW','x',now());
          INSERT INTO test_cases (id, project_id, name, description, steps, tags, source, archived, created_at, updated_at)
          VALUES (1,1,'Login','','[]','[]','manual',false,now(),now());
          INSERT INTO runs (id, project_id, test_case_id, status, case_snapshot, options, cancel_requested, worker_id, error,
                            summary, ai_analysis, created_at, report_sync_error)
          VALUES (1,1,1,'passed','{}','{}',false,'w','','{}','',now(),'');
          INSERT INTO run_steps (run_id, idx, action, description, status, attempts, duration_ms, error)
          VALUES (1,0,'goto','','passed',1,5,'');
          INSERT INTO evidence (project_id, test_case_id, run_id, kind, rel_path, mime, size, sha256, created_at)
          VALUES (1,1,1,'log','projects/1/runs/1/run.log','text/plain',3,'',now());
          INSERT INTO audit_log (user_id, action, target, detail, ip, created_at) VALUES (1,'login','admin','{}','',now());
        """))
    yield eng
    get_settings.cache_clear(); get_engine.cache_clear()


def test_upgrade_assigns_everything_to_default_tenant_and_maps_roles(legacy_db):
    command.upgrade(_cfg(), "head")
    with legacy_db.connect() as c:
        assert c.execute(text("SELECT id, slug, status FROM tenants")).all() == [(1, "unical", "active")]
        for t in ("users", "sessions", "allowed_targets", "projects", "project_secrets", "test_cases", "runs",
                  "run_steps", "evidence", "audit_log"):
            assert c.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id IS DISTINCT FROM 1")).scalar() == 0, t
        roles = dict(c.execute(text("SELECT username, role FROM users")).all())
        assert roles == {"admin": "tenant_admin", "tess": "qa_engineer", "vic": "auditor"}
        assert c.execute(text("SELECT username FROM users WHERE is_platform_admin")).scalars().all() == ["admin"]
        assert c.execute(text("SELECT rel_path FROM evidence")).scalar() == "projects/1/runs/1/run.log"
        # 0004: release-1.1 non-administrators keep tenant-wide access through an all-projects assignment
        rows = c.execute(text("SELECT u.username, a.scope, a.role, a.project_id FROM project_assignments a "
                              "JOIN users u ON u.id = a.user_id ORDER BY u.username")).all()
        assert rows == [("tess", "all", "qa_engineer", None), ("vic", "all", "auditor", None)]
        assert c.execute(text("SELECT count(*) FROM platform_settings")).scalar() == 0  # all defaults
        # 0005: the existing organization keeps Unical-managed storage (legacy agreement), evidence stays local
        assert c.execute(text("SELECT mode, provider, unical_consent_source, status FROM tenant_storage")).all() == \
            [("unical", "local", "legacy", "ok")]
        assert c.execute(text("SELECT storage, upload_status FROM evidence")).all() == [("local", "")]
        assert c.execute(text("SELECT storage_mode, details_external FROM runs")).all() == [("unical", False)]


def test_downgrade_restores_release_1_schema(legacy_db):
    command.upgrade(_cfg(), "head")
    command.downgrade(_cfg(), "0002")
    with legacy_db.connect() as c:
        roles = dict(c.execute(text("SELECT username, role FROM users")).all())
        assert roles == {"admin": "admin", "tess": "tester", "vic": "viewer"}
        assert c.execute(text("SELECT count(*) FROM projects")).scalar() == 1
    command.upgrade(_cfg(), "head")  # and forward again
