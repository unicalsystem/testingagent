"""Platform authentication settings: password policy, reCAPTCHA v3 (+ trusted IPs), lockout, sessions,
SMTP and password reset, recovery."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from testpilot import captcha, mailer
from testpilot.db import session_scope
from testpilot.models import AuditLog, PlatformSetting, User

from .conftest import H, login, make_tenant, make_user

from fastapi.testclient import TestClient

HOST = "testingagent.unical.in"


def client(app, ip: str | None = None) -> TestClient:
    c = TestClient(app, base_url=f"https://{HOST}")
    c.headers.update(H)
    if ip:
        c.headers["X-Real-IP"] = ip
    return c


class FakeGoogle:
    """Stands in for https://www.google.com/recaptcha/api/siteverify."""

    def __init__(self):
        self.calls, self.down = [], False
        self.tokens = {}  # token -> response dict

    def __call__(self, data):
        self.calls.append(data)
        if self.down:
            raise captcha.CaptchaUnavailable("ConnectError")
        if data["response"] == "testpilot-configuration-check":
            if data["secret"] != "good-secret":
                return {"success": False, "error-codes": ["invalid-input-secret"]}
            return {"success": False, "error-codes": ["invalid-input-response"]}
        if data["secret"] != "good-secret":
            return {"success": False, "error-codes": ["invalid-input-secret"]}
        return self.tokens.get(data["response"], {"success": False, "error-codes": ["invalid-input-response"]})

    def token(self, name, action="login", score=0.9, host=HOST):
        self.tokens[name] = {"success": True, "action": action, "score": score, "hostname": host,
                             "challenge_ts": "2026-10-26T00:00:00Z"}
        return name


@pytest.fixture
def google(monkeypatch):
    g = FakeGoogle()
    monkeypatch.setattr(captcha, "_post", g)
    return g


def enable_captcha(admin, actions=("login", "register", "password_reset"), min_score=0.5, policy="deny"):
    r = admin.put("/api/platform/settings/recaptcha", json={
        "enabled": True, "site_key": "site-key-123", "secret": "good-secret", "min_score": min_score,
        "actions": list(actions), "unavailable_policy": policy})
    assert r.status_code == 200, r.text
    return r.json()


def creds(**kw):
    return {"username": "sam", "password": "Password-12345", **kw}


# ------------------------------------------------------------------------------------- reCAPTCHA
def test_recaptcha_secret_is_encrypted_never_returned_and_validated(admin, google):
    bad = admin.put("/api/platform/settings/recaptcha", json={
        "enabled": True, "site_key": "k", "secret": "wrong", "min_score": 0.5, "actions": ["login"],
        "unavailable_policy": "deny"})
    assert bad.status_code == 409 and "rejected" in bad.text
    view = enable_captcha(admin)
    rc = view["recaptcha"]
    assert rc["enabled"] and rc["secret_set"] is True and "secret_enc" not in rc and rc["last_test_ok"] is True
    assert "good-secret" not in admin.get("/api/platform/settings").text
    with session_scope() as db:
        stored = db.get(PlatformSetting, "recaptcha").value
        assert stored["secret_enc"] and "good-secret" not in str(stored)
    # rotate without seeing the old one; keep it by omitting `secret`
    keep = admin.put("/api/platform/settings/recaptcha", json={
        "enabled": True, "site_key": "site-key-123", "min_score": 0.7, "actions": ["login"], "unavailable_policy": "deny"})
    assert keep.status_code == 200 and keep.json()["recaptcha"]["min_score"] == 0.7


def test_login_requires_valid_captcha(app, admin, google):
    make_user("sam", "tester")
    enable_captcha(admin)
    c = client(app, "203.0.113.7")
    cfg = c.get("/api/public/tenants/unical/auth-config").json()
    assert cfg["captcha"]["login"] == {"required": True, "site_key": "site-key-123", "action": "login"}
    assert c.post("/api/auth/login", json=creds()).status_code == 400                       # missing
    assert c.post("/api/auth/login", json=creds(captcha_token="forged")).status_code == 400   # invalid
    assert c.post("/api/auth/login", json=creds(captcha_token=google.token("low", score=0.2))).status_code == 400
    assert c.post("/api/auth/login", json=creds(captcha_token=google.token("act", action="register"))).status_code == 400
    assert c.post("/api/auth/login", json=creds(captcha_token=google.token("host", host="evil.example"))).status_code == 400
    ok = google.token("good")
    assert c.post("/api/auth/login", json=creds(captcha_token=ok)).status_code == 200
    assert google.calls[-1]["remoteip"] == "203.0.113.7" and google.calls[-1]["secret"] == "good-secret"
    # replay of an accepted token is refused
    assert client(app, "203.0.113.7").post("/api/auth/login", json=creds(captcha_token=ok)).status_code == 400


def test_trusted_ips_skip_captcha_platform_wide_and_per_organization(app, admin, google):
    make_user("sam", "tester")
    enable_captcha(admin)
    bad = admin.put("/api/platform/settings/trusted-ips", json={"cidrs": ["0.0.0.0/0"]})
    assert bad.status_code == 422
    assert admin.put("/api/platform/settings/trusted-ips", json={"cidrs": ["198.51.100.0/24", "nonsense"]}).status_code == 422
    assert admin.put("/api/platform/settings/trusted-ips", json={"cidrs": ["198.51.100.0/24"]}).status_code == 200
    office = client(app, "198.51.100.25")
    assert office.get("/api/public/tenants/unical/auth-config").json()["captcha"]["login"]["required"] is False
    assert office.post("/api/auth/login", json=creds()).status_code == 200
    assert client(app, "203.0.113.9").post("/api/auth/login", json=creds()).status_code == 400
    # registration honours the platform list too
    reg = {"org_name": "Acme", "slug": "acme-corp", "contact_name": "Asha", "contact_email": "a@acme.example",
           "accept_terms": True, "accept_privacy": True, "confirm_adult": True, "country_code": "GB"}
    assert client(app, "203.0.113.9").post("/api/public/registrations", json=reg).status_code == 400
    assert office.post("/api/public/registrations", json=reg).status_code == 202
    # an organization's own trusted list applies to its sign-in only
    make_tenant("globex")
    make_user("gina", "tenant_admin", tenant="globex", platform=False)
    g = client(app, "192.0.2.10")
    assert g.post("/api/t/globex/auth/login", json={"username": "gina", "password": "Password-12345"}).status_code == 400
    with session_scope() as db:
        from testpilot import settings_store
        from testpilot.models import Tenant
        settings_store.tenant_auth(db, db.scalars(select(Tenant).where(Tenant.slug == "globex")).one()).trusted_ips = ["192.0.2.0/28"]
    assert g.post("/api/t/globex/auth/login", json={"username": "gina", "password": "Password-12345"}).status_code == 200
    assert client(app, "192.0.2.10").post("/api/auth/login", json=creds()).status_code == 400  # not for other orgs


def test_captcha_outage_policy(app, admin, google):
    make_user("sam", "tester")
    enable_captcha(admin, policy="deny")
    google.down = True
    assert client(app, "203.0.113.7").post("/api/auth/login", json=creds(captcha_token="x")).status_code == 503
    google.down = False
    enable_captcha(admin, policy="allow_and_audit")
    google.down = True
    assert client(app, "203.0.113.7").post("/api/auth/login", json=creds(captcha_token="x")).status_code == 200
    with session_scope() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "captcha_unavailable_allowed")).first()


def test_captcha_only_on_selected_actions(app, admin, google):
    make_user("sam", "tester")
    enable_captcha(admin, actions=("register",))
    assert client(app, "203.0.113.7").post("/api/auth/login", json=creds()).status_code == 200


def test_disabling_captcha_applies_to_frontend_and_backend(app, admin, google):
    make_user("sam", "tester")
    enable_captcha(admin)
    admin.put("/api/platform/settings/recaptcha", json={"enabled": False, "site_key": "site-key-123", "min_score": 0.5,
                                                        "actions": ["login"], "unavailable_policy": "deny"})
    c = client(app, "203.0.113.7")
    assert c.get("/api/public/tenants/unical/auth-config").json()["captcha"]["login"]["required"] is False
    assert c.post("/api/auth/login", json=creds()).status_code == 200


def test_non_super_admin_cannot_read_or_change_platform_settings(app, admin):
    ops = login(app, "ops", "admin", platform=False)
    assert ops.get("/api/platform/settings").status_code == 403
    assert ops.put("/api/platform/settings/trusted-ips", json={"cidrs": []}).status_code == 403


# ------------------------------------------------------------------------------- password policy
def auth_body(**kw):
    base = {"platform_password_enabled": True, "platform_microsoft_enabled": False, "tenant_password_allowed": True,
            "tenant_microsoft_allowed": True, "login_identifier": "username_or_email", "max_failed_logins": 5,
            "lockout_minutes": 15, "session_ttl_hours": 12, "session_idle_minutes": 120,
            "password_reset_enabled": False, "password_reset_ttl_minutes": 30}
    base.update(kw)
    return base


def test_cannot_disable_every_sign_in_method_or_enable_unverified_microsoft(admin):
    r = admin.put("/api/platform/settings/auth", json=auth_body(platform_password_enabled=False))
    assert r.status_code == 409
    r = admin.put("/api/platform/settings/auth", json=auth_body(platform_microsoft_enabled=True))
    assert r.status_code == 409 and "Microsoft" in r.text


def test_password_login_disabled_for_an_organization(app, admin):
    make_tenant("acme")
    make_user("al", "tenant_admin", tenant="acme", platform=False)
    assert admin.put("/api/platform/settings/auth", json=auth_body(tenant_password_allowed=False)).status_code == 200
    r = client(app).post("/api/t/acme/auth/login", json={"username": "al", "password": "Password-12345"})
    assert r.status_code == 403
    assert client(app).get("/api/public/tenants/acme/auth-config").json()["password"] is False
    # the Unical organization is governed by its own switch
    make_user("sam", "tester")
    assert client(app).post("/api/auth/login", json=creds()).status_code == 200


def test_email_login_and_configurable_lockout(app, admin):
    make_user("sam", "tester", email="sam@unical.in")
    c = client(app)
    assert c.post("/api/auth/login", json=creds(username="SAM@unical.in")).status_code == 200
    admin.put("/api/platform/settings/auth", json=auth_body(login_identifier="username"))
    assert c.post("/api/auth/login", json=creds(username="sam@unical.in")).status_code == 401
    admin.put("/api/platform/settings/auth", json=auth_body(max_failed_logins=3, lockout_minutes=5))
    for _ in range(3):
        assert c.post("/api/auth/login", json=creds(password="wrong-password-1")).status_code == 401
    assert c.post("/api/auth/login", json=creds()).status_code == 401  # locked: same answer as a wrong password


def test_session_idle_timeout_is_configurable(app, admin):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from testpilot.models import Session_
    admin.put("/api/platform/settings/auth", json=auth_body(session_idle_minutes=5))
    c = login(app, "sam", "tester")
    with session_scope() as db:
        db.execute(update(Session_).values(last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
    assert c.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------------------------ e-mail / reset
class FakeSMTP:
    sent: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mailer, "_connect", lambda c: FakeSMTP())
    return FakeSMTP


def configure_smtp(admin):
    r = admin.put("/api/platform/settings/smtp", json={
        "enabled": True, "host": "smtp.example.com", "port": 587, "security": "starttls", "username": "mailer",
        "password": "smtp-pass-123", "from_address": "noreply@unical.in", "from_name": "TestAgent"})
    assert r.status_code == 200, r.text
    assert "smtp-pass-123" not in r.text and r.json()["smtp"]["password_set"] is True


def test_password_reset_by_email(app, admin, smtp, google):
    assert admin.put("/api/platform/settings/auth", json=auth_body(password_reset_enabled=True)).status_code == 409
    configure_smtp(admin)
    t = admin.post("/api/platform/settings/smtp/test", json={"to": "ops@unical.in"}).json()
    assert t["ok"] and smtp.sent[-1]["To"] == "ops@unical.in"
    assert admin.put("/api/platform/settings/auth", json=auth_body(password_reset_enabled=True)).status_code == 200
    make_user("sam", "tester", email="sam@unical.in")
    anon = client(app, "203.0.113.50")
    assert anon.get("/api/public/tenants/unical/auth-config").json()["password_reset"] is True
    # unknown account: identical answer, nothing sent
    n = len(smtp.sent)
    r1 = anon.post("/api/public/password-reset/request", json={"tenant": "unical", "identifier": "nobody"})
    assert r1.status_code == 200 and len(smtp.sent) == n
    r2 = anon.post("/api/public/password-reset/request", json={"tenant": "unical", "identifier": "sam@unical.in"})
    assert r2.json() == r1.json() and len(smtp.sent) == n + 1
    body = smtp.sent[-1].get_content()
    token = body.split("/unical/reset-password#", 1)[1].split()[0]
    old = login_client = client(app)
    assert login_client.post("/api/auth/login", json=creds()).status_code == 200
    assert anon.post("/api/public/password-reset/confirm", json={"token": token, "new_password": "Brand-new-pass-1"}).json()["ok"]
    assert old.get("/api/auth/me").status_code == 401  # signed out everywhere
    assert anon.post("/api/public/password-reset/confirm", json={"token": token, "new_password": "Another-pass-12"}).status_code == 404
    assert client(app).post("/api/auth/login", json=creds(password="Brand-new-pass-1")).status_code == 200
    # captcha protects the reset request when selected
    enable_captcha(admin, actions=("password_reset",))
    assert anon.post("/api/public/password-reset/request", json={"tenant": "unical", "identifier": "sam"}).status_code == 400


def test_invitation_and_approval_links_are_emailed_when_smtp_is_on(app, admin, smtp):
    configure_smtp(admin)
    r = admin.post("/api/admin/invitations", json={"email": "new@unical.in", "role": "developer"})
    assert r.json()["emailed"] is True and smtp.sent[-1]["To"] == "new@unical.in"
    assert r.json()["link"].split("#")[1] in smtp.sent[-1].get_content()


def test_recovery_cli_reenables_password_sign_in(admin, capsys):
    from testpilot import cli, settings_store
    with session_scope() as db:
        settings_store.put(db, "auth", {"platform_password_enabled": False, "platform_microsoft_enabled": True}, None)
        settings_store.put(db, "recaptcha", {"enabled": True}, None)
    cli.main(["auth-recovery", "--disable-captcha", "--disable-microsoft"])
    with session_scope() as db:
        a = settings_store.auth(db)
        assert a["platform_password_enabled"] and not a["platform_microsoft_enabled"]
        assert not settings_store.get(db, "recaptcha")["enabled"]
        assert db.scalars(select(AuditLog).where(AuditLog.action == "platform_auth_recovery")).first()


def test_username_cannot_shadow_another_members_email(app, admin):
    make_user("alice", "tester", email="alice@corp.example")
    r = admin.post("/api/admin/users", json={"username": "alice@corp.example", "role": "tester", "password": "Password-12345"})
    assert r.status_code == 409
    r = admin.post("/api/admin/users", json={"username": "bob", "email": "alice", "role": "tester", "password": "Password-12345"})
    assert r.status_code in (409, 422)
    tok = admin.post("/api/admin/invitations", json={"email": "eve@corp.example", "role": "developer"}).json()["link"].split("#")[1]
    r = client(app).post("/api/public/invitations/accept", json={"token": tok, "username": "alice@corp.example",
                                                                 "password": "Password-12345"})
    assert r.status_code == 409
    # the link is still usable with a proper username (the failed attempt did not consume it)
    assert client(app).post("/api/public/invitations/accept", json={"token": tok, "username": "eve",
                                                                    "password": "Password-12345"}).status_code == 200


def test_disabling_password_sign_in_ends_password_sessions(app, admin):
    make_tenant("acme")
    c = login(app, "al", "tenant_admin", tenant="acme", platform=False)
    assert c.get("/api/t/acme/auth/me").status_code == 200
    admin.put("/api/platform/settings/auth", json=auth_body(tenant_password_allowed=False))
    assert c.get("/api/t/acme/auth/me").status_code == 401


def test_smtp_password_must_be_reentered_when_the_server_changes(admin, smtp):
    configure_smtp(admin)
    r = admin.put("/api/platform/settings/smtp", json={
        "enabled": True, "host": "attacker.example", "port": 587, "security": "starttls", "username": "mailer",
        "from_address": "noreply@unical.in", "from_name": "TestAgent"})
    assert r.status_code == 422


def test_overly_broad_trusted_ranges_are_refused(admin):
    for bad in (["0.0.0.0/1", "128.0.0.0/1"], ["10.0.0.0/7"], ["2000::/3"]):
        assert admin.put("/api/platform/settings/trusted-ips", json={"cidrs": bad}).status_code == 422, bad
    assert admin.put("/api/platform/settings/trusted-ips", json={"cidrs": ["10.0.0.0/8", "2001:db8::/32"]}).status_code == 200
