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
from testpilot.models import AllowedTarget, User  # noqa: E402
from testpilot.security import hash_password  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
SAMPLE = BACKEND.parent / "sample-app" / "site"
H = {"X-Requested-With": "TestPilot"}


@pytest.fixture(scope="session", autouse=True)
def schema():
    with get_engine().begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")  # exercises the real migrations
    yield
    shutil.rmtree(EVIDENCE, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean(schema):
    yield
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE users, sessions, allowed_targets, projects, project_secrets, test_cases, runs, "
                       "run_steps, evidence, audit_log, worker_heartbeats RESTART IDENTITY CASCADE"))


def make_user(username: str, role: str, password: str = "Password-12345") -> None:
    with session_scope() as db:
        db.add(User(username=username, role=role, password_hash=hash_password(password)))


@pytest.fixture
def app():
    from testpilot.api.app import create_app

    return create_app()


def login(app, username: str, role: str) -> TestClient:
    make_user(username, role)
    c = TestClient(app, base_url="https://testingagent.unical.in")
    r = c.post("/api/auth/login", json={"username": username, "password": "Password-12345"}, headers=H)
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


def allow(host: str) -> None:
    with session_scope() as db:
        db.add(AllowedTarget(host=host))
