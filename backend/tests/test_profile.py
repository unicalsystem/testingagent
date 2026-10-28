"""Self-service profile (release 1.9.1): display name, e-mail with password confirmation, sessions."""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import H, login, make_user


def test_profile_edit_and_email_rules(app, tester):
    r = tester.patch("/api/auth/me", json={"display_name": "  Tess Tester  "})
    assert r.status_code == 200 and r.json()["display_name"] == "Tess Tester" and r.json()["has_password"] is True
    assert tester.patch("/api/auth/me", json={"email": "tess@acme.example"}).status_code == 400  # needs password
    assert tester.patch("/api/auth/me", json={"email": "not-an-email", "current_password": "Password-12345"}).status_code == 422
    ok = tester.patch("/api/auth/me", json={"email": "Tess@Acme.example", "current_password": "Password-12345"})
    assert ok.status_code == 200 and ok.json()["email"] == "tess@acme.example"
    make_user("vic2", "viewer", email="vic@acme.example")
    clash = tester.patch("/api/auth/me", json={"email": "vic@acme.example", "current_password": "Password-12345"})
    assert clash.status_code == 409
    # a username or role cannot be changed through the profile
    tester.patch("/api/auth/me", json={"display_name": "x", "role": "tenant_owner", "username": "boss"})
    me = tester.get("/api/auth/me").json()
    assert me["username"] == "tess" and me["role"] == "qa_engineer"


def test_sign_out_other_devices(app, tester):
    other = TestClient(app, base_url="https://testingagent.unical.in")
    assert other.post("/api/auth/login", json={"username": "tess", "password": "Password-12345"}, headers=H).status_code == 200
    other.headers.update(H)
    s = tester.get("/api/auth/sessions").json()
    assert len(s) == 2 and sum(x["current"] for x in s) == 1
    assert tester.post("/api/auth/sessions/revoke-others").json() == {"revoked": 1}
    assert other.get("/api/auth/me").status_code == 401
    assert tester.get("/api/auth/me").status_code == 200
