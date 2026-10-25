"""MySQL report store tests. Skipped unless TEST_MYSQL_URL points at a disposable MySQL/MariaDB,
e.g. TEST_MYSQL_URL=mysql://user:pass@127.0.0.1:3306/tp_reports_test"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, text

from testpilot import report_store, worker
from testpilot.config import get_settings

from .test_worker import run_case, sample_project  # noqa: F401  (fixture)

URL = os.environ.get("TEST_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_MYSQL_URL not set")


@pytest.fixture
def mysql_store(monkeypatch, tmp_path):
    u = urlsplit(URL)
    s = get_settings()
    for k, v in dict(mysql_reports_enabled=True, mysql_reports_host=u.hostname, mysql_reports_port=u.port or 3306,
                     mysql_reports_database=u.path.lstrip("/"), mysql_reports_user=u.username,
                     mysql_reports_table_prefix="tptest_").items():
        monkeypatch.setattr(s, k, v)
    from pydantic import SecretStr
    monkeypatch.setattr(s, "mysql_reports_password", SecretStr(u.password))
    report_store.engine.cache_clear()
    report_store._tables.cache_clear()
    eng = create_engine(URL.replace("mysql://", "mysql+pymysql://"))
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS tptest_reports, tptest_report_steps"))
    yield eng
    report_store.engine.cache_clear()
    report_store._tables.cache_clear()


def test_report_copied_to_mysql(admin, sample_project, mysql_store):  # noqa: F811
    run = run_case(admin, sample_project["id"], [{"action": "goto", "url": "/"}, {"action": "expect_visible", "selector": "h1"}])
    assert run["status"] == "passed"
    with mysql_store.connect() as c:
        row = c.execute(text("SELECT status, steps_total, steps_passed, report_json, report_html FROM tptest_reports "
                             "WHERE run_id=:r"), {"r": run["id"]}).one()
        steps = c.execute(text("SELECT COUNT(*) FROM tptest_report_steps WHERE run_id=:r"), {"r": run["id"]}).scalar()
    assert row.status == "passed" and row.steps_total == 2 and row.steps_passed == 2 and steps == 2
    assert '"report_version": 1' in row.report_json and row.report_html.startswith("<!doctype html>")
    assert admin.get(f"/api/runs/{run['id']}").json()["report_synced_at"]


def test_mysql_outage_does_not_break_runs_and_retries(admin, sample_project, mysql_store, monkeypatch):  # noqa: F811
    s = get_settings()
    monkeypatch.setattr(s, "mysql_reports_port", 1)  # nothing listens there
    report_store.engine.cache_clear()
    run = run_case(admin, sample_project["id"], [{"action": "goto", "url": "/"}])
    assert run["status"] == "passed"
    r = admin.get(f"/api/runs/{run['id']}").json()
    assert r["report_synced_at"] is None and "MySQL error" in r["report_sync_error"]
    assert "Password" not in r["report_sync_error"]
    monkeypatch.setattr(s, "mysql_reports_port", urlsplit(URL).port or 3306)
    report_store.engine.cache_clear()
    assert report_store.sync_pending() == (1, 0)
    assert admin.get(f"/api/runs/{run['id']}").json()["report_sync_error"] == ""


def test_one_bad_report_does_not_block_others(admin, sample_project, mysql_store, monkeypatch):  # noqa: F811
    s = get_settings()
    monkeypatch.setattr(s, "mysql_reports_enabled", False)  # queue two runs without syncing
    a = run_case(admin, sample_project["id"], [{"action": "goto", "url": "/"}], "a")
    b = run_case(admin, sample_project["id"], [{"action": "goto", "url": "/"}], "b")
    monkeypatch.setattr(s, "mysql_reports_enabled", True)
    real_push = report_store.push

    def flaky(db, run):
        if run.id == a["id"]:
            raise report_store.ReportStoreError("MySQL error 1153: packet too large")
        return real_push(db, run)

    monkeypatch.setattr(report_store, "push", flaky)
    assert report_store.sync_pending() == (1, 1)  # b synced even though a failed
    assert admin.get(f"/api/runs/{b['id']}").json()["report_synced_at"]
