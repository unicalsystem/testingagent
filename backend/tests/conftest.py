"""Test fixtures. Requires a PostgreSQL database (TEST_DATABASE_URL); Chromium for worker tests.

  TEST_DATABASE_URL=postgresql+psycopg://testpilot:devpass@127.0.0.1:5432/testpilot_test pytest
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

DB = os.environ.get("TEST_DATABASE_URL")
if not DB:
    pytest.exit("Set TEST_DATABASE_URL to a disposable PostgreSQL database", returncode=2)

EVIDENCE = Path(tempfile.mkdtemp(prefix="tp-evidence-"))
os.environ.update(
    APP_ENV="test",
    DATABASE_URL=DB,
    PUBLIC_URL="https://testingagent.unical.in",
    ALLOWED_ORIGINS="https://testingagent.unical.in",
    SESSION_SECRET="t" * 40,
    CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    COOKIE_SECURE="false",
    EVIDENCE_DIR=str(EVIDENCE),
    ALLOWED_PRIVATE_TARGETS="127.0.0.1",
    ANTHROPIC_API_KEY="",
    LOG_JSON="false",
    TESTPILOT_CONFIG_LOCAL="/nonexistent/config.local",
    CAPTURE_VIDEO="false",
    STEP_TIMEOUT_MS="4000",
)
for k in [k for k in os.environ if k.startswith("MYSQL_REPORTS_")]:
    del os.environ[k]

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from testpilot.db import get_engine, session_scope  # noqa: E402
from testpilot.models import AllowedTarget, Tenant, TenantBranding, User  # noqa: E402
from testpilot.permissions import normalize_role  # noqa: E402
from testpilot.security import hash_password  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
SAMPLE = BACKEND.parent / "sample-app" / "site"
H = {"X-Requested-With": "TestAgent"}


@pytest.fixture(scope="session", autouse=True)
def schema():
    with get_engine().begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")  # exercises the real migrations
    yield
    shutil.rmtree(EVIDENCE, ignore_errors=True)


DEFAULT = "unical"


@pytest.fixture(autouse=True)
def clean(schema):
    yield
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE tenants, tenant_registrations, tenant_branding, invitations, users, sessions, "
                       "allowed_targets, projects, project_secrets, test_cases, runs, run_steps, evidence, audit_log, "
                       "worker_heartbeats, platform_settings, tenant_auth_settings, identity_providers, "
                       "external_identities, oidc_transactions, password_resets, captcha_tokens_seen, user_groups, "
                       "group_members, project_assignments, tenant_storage, case_versions, privacy_requests, "
                       "legal_acceptances, test_runs, test_run_items, automation_jobs, automation_items, "
                  "api_suites, api_cases, api_runs, api_run_results, api_monitors, monitor_checks, "
                  "monitor_incidents, integrations, integration_syncs, integration_issues, mobile_providers, mobile_apps, mobile_cases, mobile_run_groups, mobile_runs, mobile_sessions, tenant_subscriptions, billing_coupons, billing_payments, "
                           "billing_coupon_redemptions, billing_usage RESTART IDENTITY CASCADE"))
        # the migration's default tenant (id 1) is recreated for the next test
        c.execute(text("INSERT INTO tenants (slug, name, status) VALUES ('unical', 'Unical Systems', 'active')"))
        c.execute(text("INSERT INTO tenant_branding (tenant_id, display_name) VALUES (1, 'Unical Systems')"))
        # release 1.9 tests switch self-service sign-up on explicitly; older tests exercise the approval flow
        c.execute(text("""INSERT INTO platform_settings (key, value) VALUES ('billing', '{"self_signup": false}')"""))
        # as migrated: existing organizations keep Unical-managed storage
        c.execute(text("INSERT INTO tenant_storage (tenant_id, mode, provider, unical_consent_at, unical_consent_source, "
                       "status) VALUES (1, 'unical', 'local', now(), 'legacy', 'ok')"))


def tenant_id(slug: str = DEFAULT) -> int:
    from sqlalchemy import select

    with session_scope() as db:
        return db.scalars(select(Tenant.id).where(Tenant.slug == slug)).one()


def make_tenant(slug: str, name: str | None = None, status: str = "active", storage: str = "unical") -> int:
    """storage="unical": Unical-managed storage already agreed (like organizations migrated from 1.2);
    storage="": not chosen yet (like an organization approved after 1.3)."""
    from testpilot.models import TenantStorage, utcnow
    with session_scope() as db:
        t = Tenant(slug=slug, name=name or slug.title(), status=status)
        db.add(t)
        db.flush()
        db.add(TenantBranding(tenant_id=t.id, display_name=t.name))
        if storage == "unical":
            db.add(TenantStorage(tenant_id=t.id, mode="unical", provider="local", unical_consent_at=utcnow(),
                                 unical_consent_source="legacy", status="ok", config={}))
        return t.id


def make_user(username: str, role: str, password: str = "Password-12345", tenant: str = DEFAULT,
              platform: bool | None = None, assign_all: bool = True, email: str = "") -> int:
    """assign_all: non-administrators get an all-projects assignment with their role, exactly like users
    migrated from release 1.1 (pass False to start with no project access)."""
    from testpilot.models import ProjectAssignment
    tid = tenant_id(tenant)
    if platform is None:  # release-1.0 "admin" in the Unical tenant == migrated Super Admin
        platform = role == "admin" and tenant == DEFAULT
    r = normalize_role(role)
    with session_scope() as db:
        u = User(tenant_id=tid, username=username, role=r, is_platform_admin=platform, email=email,
                 password_hash=hash_password(password))
        db.add(u)
        db.flush()
        if assign_all and r not in ("tenant_owner", "tenant_admin"):
            db.add(ProjectAssignment(tenant_id=tid, user_id=u.id, scope="all", role=r))
        return u.id


def api_base(tenant: str | None) -> str:
    return "/api" if tenant is None else f"/api/t/{tenant}"


@pytest.fixture
def app():
    from testpilot.api.app import create_app

    return create_app()


def login(app, username: str, role: str, tenant: str | None = None, platform: bool | None = None,
          assign_all: bool = True) -> TestClient:
    """tenant=None: legacy /api routes (default tenant). Otherwise /api/t/<tenant>/..."""
    make_user(username, role, tenant=tenant or DEFAULT, platform=platform, assign_all=assign_all)
    c = TestClient(app, base_url="https://testingagent.unical.in")
    r = c.post(f"{api_base(tenant)}/auth/login", json={"username": username, "password": "Password-12345"}, headers=H)
    assert r.status_code == 200, r.text
    c.headers.update(H)
    return c


@pytest.fixture
def admin(app):
    return login(app, "admin", "admin")


@pytest.fixture
def tester(app):
    return login(app, "tess", "tester")


@pytest.fixture
def viewer(app):
    return login(app, "vic", "viewer")


@pytest.fixture(scope="session")
def sample_server():
    """Serves the bundled sample app on 127.0.0.1:<random port>."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(SAMPLE))
    handler.log_message = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def allow(host: str, tenant: str = DEFAULT) -> None:
    tid = tenant_id(tenant)
    with session_scope() as db:
        db.add(AllowedTarget(tenant_id=tid, host=host))
