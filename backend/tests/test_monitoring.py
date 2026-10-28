"""API uptime monitoring (release 1.6): checks, incidents, in-app alerts, validation, reports."""
from __future__ import annotations

from datetime import timedelta

import pytest

from .conftest import allow
from .test_apitest import STATE, fake_api, project, reset_state  # noqa: F401  (fixtures)


def monitor(c, pid, url, **kw):
    body = {"name": "Health", "url": url, "interval_seconds": 60, "failure_threshold": 2, "alert_channels": ["inapp"], **kw}
    r = c.post(f"/api/projects/{pid}/monitors", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_down_incident_alert_and_recovery(tester, fake_api):
    from testpilot import monitoring

    allow("127.0.0.1")
    pid = project(tester, fake_api)
    m = monitor(tester, pid, f"{fake_api}/health", body_contains='"ok"')
    assert monitoring.due() and not monitoring.due()  # claimed once, rescheduled
    monitoring.check(m["id"])
    assert tester.get(f"/api/monitors/{m['id']}").json()["status"] == "up"
    STATE["down"] = True
    monitoring.check(m["id"])
    d = tester.get(f"/api/monitors/{m['id']}").json()
    assert d["status"] == "up" and d["consecutive_failures"] == 1  # one failure is not an outage yet
    monitoring.check(m["id"])
    d = tester.get(f"/api/monitors/{m['id']}").json()
    assert d["status"] == "down" and d["incidents"][0]["ended_at"] is None and "503" in d["incidents"][0]["cause"]
    assert d["incidents"][0]["alerts"][0]["channel"] == "inapp"
    alerts = tester.get("/api/monitoring/alerts").json()
    assert [a["id"] for a in alerts] == [m["id"]]
    STATE["down"] = False
    monitoring.check(m["id"])
    d = tester.get(f"/api/monitors/{m['id']}").json()
    assert d["status"] == "up" and d["incidents"][0]["ended_at"] is not None
    assert tester.get("/api/monitoring/alerts").json() == []
    assert d["stats"]["24h"]["checks"] == 4 and d["stats"]["24h"]["failed_checks"] == 2 and d["stats"]["24h"]["incidents"] == 1
    assert d["stats"]["24h"]["uptime_pct"] < 100
    rep = tester.get(f"/api/monitors/{m['id']}/report?days=7")
    assert rep.status_code == 200 and "Incidents" in rep.text and "Daily availability" in rep.text
    js = tester.get(f"/api/monitors/{m['id']}/report?days=7&format=json").json()
    assert js["stats"]["incidents"] == 1 and len(js["daily"]) == 7
    ov = tester.get("/api/monitoring/overview").json()
    assert ov[0]["project_name"] == "Pets" and ov[0]["uptime_30d"] is not None


def test_unauthorised_host_is_paused_not_down(tester, fake_api):
    from testpilot import monitoring

    pid = project(tester, fake_api)
    m = monitor(tester, pid, f"{fake_api}/health")
    monitoring.check(m["id"])
    d = tester.get(f"/api/monitors/{m['id']}").json()
    assert d["status"] == "paused" and "allowlist" in d["last_error"] and not d["incidents"]


def test_validation(tester, fake_api):
    pid = project(tester, fake_api)
    url = f"{fake_api}/health"
    for bad in ({"webhook_url": "http://hooks.example.com/x", "alert_channels": ["webhook"]},
                {"webhook_url": "https://127.0.0.1/x", "alert_channels": ["webhook"]},
                {"alert_channels": ["email"]},
                {"alert_channels": ["sms"]},
                {"headers": {"Authorization": "Bearer x"}},
                {"interval_seconds": 7},
                {"expected_status": ["9xx"]},
                {"method": "DELETE"},
                {"use_suite_auth": True}):
        r = tester.post(f"/api/projects/{pid}/monitors", json={"name": "x", "url": url, **bad})
        assert r.status_code == 422, (bad, r.text)


def test_viewer_cannot_manage_monitors(tester, viewer, fake_api):
    pid = project(tester, fake_api)
    m = monitor(tester, pid, f"{fake_api}/health")
    assert viewer.get(f"/api/monitors/{m['id']}").status_code == 200
    assert viewer.post(f"/api/projects/{pid}/monitors", json={"name": "v", "url": f"{fake_api}/health"}).status_code == 403
    assert viewer.delete(f"/api/monitors/{m['id']}").status_code == 403
    assert viewer.post(f"/api/monitors/{m['id']}/test-alert").status_code == 403


def test_stats_clip_ongoing_incident():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from testpilot import monitoring

    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert monitoring._overlap(now - timedelta(hours=2), now, now - timedelta(hours=1), now) == 3600
    assert monitoring._dur(3725).strip() == "1 h 2 min 5 s"
    body = monitoring._webhook_body("teams", "down", "DOWN: x", "body", {"link": "https://t/x"})
    assert body["attachments"][0]["content"]["type"] == "AdaptiveCard"
    assert monitoring._webhook_body("slack", "down", "DOWN: x", "b", {"link": ""})["text"].startswith(":red_circle:")
    _ = SimpleNamespace
    with pytest.raises(Exception):
        monitoring.validate_webhook_url("https://169.254.169.254/latest")
