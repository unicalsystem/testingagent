"""Microsoft Entra ID sign-in against a fake Microsoft (real RSA-signed ID tokens, real JWKS validation)."""
from __future__ import annotations

import base64
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from testpilot import oidc
from testpilot.db import session_scope
from testpilot.models import ExternalIdentity, IdentityProvider, Session_, User

from .conftest import H, login, make_tenant, make_user

HOST = "testingagent.unical.in"
ACME_TID = "11111111-1111-1111-1111-111111111111"
GLOBEX_TID = "22222222-2222-2222-2222-222222222222"
ACME_APP = "aaaaaaaa-0000-0000-0000-000000000001"
GLOBEX_APP = "bbbbbbbb-0000-0000-0000-000000000002"


def b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class FakeMicrosoft:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.secrets = {ACME_APP: "acme-secret", GLOBEX_APP: "globex-secret"}
        self.codes: dict[str, dict] = {}  # code -> {client_id, verifier_challenge, claims, tid}
        self.token_requests = []
        self.tamper = None  # callable(claims) -> claims

    # discovery / jwks
    def get(self, url):
        if url.endswith("/.well-known/openid-configuration"):
            tid = url.split("/")[3]
            base = f"https://login.microsoftonline.com/{tid}"
            return {"issuer": f"{base}/v2.0", "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{base}/oauth2/v2.0/token",
                    "jwks_uri": "https://login.microsoftonline.com/common/discovery/v2.0/keys"}
        if url.endswith("/keys"):
            pub = self.key.public_key().public_numbers()
            return {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "n": b64(pub.n), "e": b64(pub.e)}]}
        raise AssertionError(url)

    def post(self, url, data):
        self.token_requests.append((url, data))
        tid = url.split("/")[3]
        if self.secrets.get(data["client_id"]) != data["client_secret"]:
            return 401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}
        if data["grant_type"] == "client_credentials":
            return 200, {"access_token": "x", "token_type": "Bearer"}
        c = self.codes.pop(data["code"], None)
        if not c or c["client_id"] != data["client_id"] or c["tid"] != tid:
            return 400, {"error": "invalid_grant"}
        import hashlib
        chal = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if chal != c["challenge"]:
            return 400, {"error": "invalid_grant", "error_description": "PKCE"}
        claims = c["claims"]
        if self.tamper:
            claims = self.tamper(dict(claims))
        tok = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})
        return 200, {"id_token": tok, "access_token": "at"}

    def authorize(self, url, *, oid, email, tid, name="Test User", groups=None):
        """What Microsoft does after the user signs in: issue a code bound to the request."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        now = int(time.time())
        claims = {"iss": f"https://login.microsoftonline.com/{tid}/v2.0", "aud": q["client_id"], "sub": "pairwise-" + oid,
                  "oid": oid, "tid": tid, "email": email, "preferred_username": email, "name": name,
                  "nonce": q["nonce"], "iat": now, "nbf": now, "exp": now + 600}
        if groups is not None:
            claims["groups"] = groups
        code = uuid.uuid4().hex
        self.codes[code] = {"client_id": q["client_id"], "challenge": q["code_challenge"], "claims": claims,
                            "tid": urlsplit(url).path.split("/")[1]}
        assert q["code_challenge_method"] == "S256" and q["redirect_uri"].endswith("/api/public/oidc/microsoft/callback")
        return code, q["state"]


@pytest.fixture
def ms(monkeypatch):
    f = FakeMicrosoft()
    monkeypatch.setattr(oidc, "http_get_json", f.get)
    monkeypatch.setattr(oidc, "http_post_form", f.post)
    oidc._cache.clear()
    return f


def client(app) -> TestClient:
    c = TestClient(app, base_url=f"https://{HOST}", follow_redirects=False)
    c.headers.update(H)
    return c


def setup_org(app, slug, tid, app_id, secret, **extra):
    make_tenant(slug)
    adm = login(app, f"{slug}-admin", "tenant_admin", tenant=slug, platform=False)
    base = f"/api/t/{slug}"
    r = adm.put(f"{base}/admin/sso/microsoft", json={"entra_tenant_id": tid, "client_id": app_id, "client_secret": secret, **extra})
    assert r.status_code == 200, r.text
    assert "secret" not in json.dumps(r.json()["microsoft"]).replace("secret_set", "").replace("secret_hint", "").replace("secret_updated_at", "")
    t = adm.post(f"{base}/admin/sso/microsoft/test")
    assert t.json()["ok"], t.text
    r = adm.put(f"{base}/admin/sso/policy", json={"password_enabled": True, "microsoft_enabled": True})
    assert r.status_code == 200, r.text
    return adm


def sign_in(app, ms, slug, *, oid, email, tid, purpose="login", invite_token=None, c=None, path=None, **kw):
    c = c or client(app)
    if path:
        r = c.post(path)
    else:
        r = c.post("/api/public/oidc/microsoft/start", json={"tenant": slug, "purpose": purpose, "invite_token": invite_token})
    assert r.status_code == 200, r.text
    code, state = ms.authorize(r.json()["url"], oid=oid, email=email, tid=tid, **kw)
    cb = c.get(f"/api/public/oidc/microsoft/callback?code={code}&state={state}")
    return c, cb


def landed(cb) -> str:
    return cb.text.split("url=", 1)[1].split('"', 1)[0]


# ----------------------------------------------------------------------------------------------
def test_secret_is_encrypted_and_wrong_secret_fails_the_connection_test(app, ms):
    make_tenant("acme")
    adm = login(app, "adm", "tenant_admin", tenant="acme", platform=False)
    adm.put("/api/t/acme/admin/sso/microsoft", json={"entra_tenant_id": ACME_TID, "client_id": ACME_APP, "client_secret": "wrong"})
    t = adm.post("/api/t/acme/admin/sso/microsoft/test").json()
    assert t["ok"] is False and "AADSTS7000215" in t["message"] and t["microsoft"]["status"] == "error"
    assert adm.put("/api/t/acme/admin/sso/policy", json={"password_enabled": True, "microsoft_enabled": True}).status_code == 409
    with session_scope() as db:
        idp = db.scalars(select(IdentityProvider)).one()
        assert idp.client_secret_enc and "wrong" not in idp.client_secret_enc
    for bad in ({"entra_tenant_id": "common", "client_id": ACME_APP}, {"entra_tenant_id": ACME_TID, "client_id": "x"},
                {"entra_tenant_id": ACME_TID, "client_id": ACME_APP, "cloud_host": "evil.example.com"}):
        assert adm.put("/api/t/acme/admin/sso/microsoft", json=bad).status_code == 422


def test_unlinked_microsoft_account_is_refused_even_with_matching_email(app, ms):
    setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    make_user("asha", "tester", tenant="acme", platform=False, email="asha@acme.example")
    c, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="asha@acme.example", tid=ACME_TID)
    assert "sso_error=not_linked" in landed(cb)
    assert c.get("/api/t/acme/auth/me").status_code == 401


def test_link_then_sign_in_with_microsoft(app, ms):
    setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    asha = login(app, "asha", "tester", tenant="acme", platform=False)
    oid = str(uuid.uuid4())
    _, cb = sign_in(app, ms, "acme", oid=oid, email="asha@acme.example", tid=ACME_TID, c=asha,
                    path="/api/t/acme/auth/microsoft/link-start")
    assert "notice=microsoft_linked" in landed(cb)
    assert asha.get("/api/t/acme/auth/me").json()["microsoft_linked"] is True
    c, cb = sign_in(app, ms, "acme", oid=oid, email="asha@acme.example", tid=ACME_TID)
    assert landed(cb) == "/acme/projects"
    me = c.get("/api/t/acme/auth/me").json()
    assert me["username"] == "asha"
    with session_scope() as db:
        assert db.scalars(select(Session_.auth_method).where(Session_.user_id == me["id"]).order_by(Session_.id.desc())).first() == "microsoft"


def test_state_nonce_binding_and_token_checks(app, ms):
    setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    asha = login(app, "asha", "tester", tenant="acme", platform=False)
    oid = str(uuid.uuid4())
    sign_in(app, ms, "acme", oid=oid, email="asha@acme.example", tid=ACME_TID, c=asha, path="/api/t/acme/auth/microsoft/link-start")

    # 1. replayed callback (state already used)
    c = client(app)
    r = c.post("/api/public/oidc/microsoft/start", json={"tenant": "acme", "purpose": "login"})
    code, state = ms.authorize(r.json()["url"], oid=oid, email="x@acme.example", tid=ACME_TID)
    assert landed(c.get(f"/api/public/oidc/microsoft/callback?code={code}&state={state}")) == "/acme/projects"
    assert "sso_error=" in landed(c.get(f"/api/public/oidc/microsoft/callback?code={code}&state={state}"))

    # 2. callback in another browser (no binding cookie) -> login CSRF refused
    c1, c2 = client(app), client(app)
    r = c1.post("/api/public/oidc/microsoft/start", json={"tenant": "acme", "purpose": "login"})
    code, state = ms.authorize(r.json()["url"], oid=oid, email="x@acme.example", tid=ACME_TID)
    assert "sso_error=invalid_state" in landed(c2.get(f"/api/public/oidc/microsoft/callback?code={code}&state={state}"))
    assert c2.get("/api/t/acme/auth/me").status_code == 401

    # 3. tampered tokens
    for tamper, why in ((lambda cl: {**cl, "nonce": "other"}, "nonce"),
                        (lambda cl: {**cl, "aud": GLOBEX_APP}, "audience"),
                        (lambda cl: {**cl, "tid": GLOBEX_TID}, "tid"),
                        (lambda cl: {**cl, "iss": f"https://login.microsoftonline.com/{GLOBEX_TID}/v2.0"}, "issuer"),
                        (lambda cl: {**cl, "exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "expired")):
        ms.tamper = tamper
        c, cb = sign_in(app, ms, "acme", oid=oid, email="x@acme.example", tid=ACME_TID)
        assert "sso_error=invalid_token" in landed(cb), why
        assert c.get("/api/t/acme/auth/me").status_code == 401, why
    ms.tamper = None

    # 4. token signed by someone else
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    real = ms.key
    c = client(app)
    r = c.post("/api/public/oidc/microsoft/start", json={"tenant": "acme", "purpose": "login"})
    code, state = ms.authorize(r.json()["url"], oid=oid, email="x@acme.example", tid=ACME_TID)
    ms.key = other  # signs with a key not in the JWKS served below...
    ms_get = ms.get
    oidc_get = oidc.http_get_json
    ms.key_pub_override = real
    try:
        oidc.http_get_json = lambda url: (ms_get(url) if not url.endswith("/keys") else
                                          {"keys": [{"kty": "RSA", "kid": "k1", "n": b64(real.public_key().public_numbers().n),
                                                     "e": b64(real.public_key().public_numbers().e)}]})
        oidc._cache.clear()
        assert "sso_error=invalid_token" in landed(c.get(f"/api/public/oidc/microsoft/callback?code={code}&state={state}"))
    finally:
        oidc.http_get_json = oidc_get
        ms.key = real


def test_tenant_configurations_cannot_be_mixed(app, ms):
    setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    setup_org(app, "globex", GLOBEX_TID, GLOBEX_APP, "globex-secret")
    gina = login(app, "gina", "tester", tenant="globex", platform=False)
    oid = str(uuid.uuid4())
    sign_in(app, ms, "globex", oid=oid, email="gina@globex.example", tid=GLOBEX_TID, c=gina,
            path="/api/t/globex/auth/microsoft/link-start")
    # Globex's Microsoft user starting Acme's sign-in is sent to ACME's Entra tenant and app ...
    c = client(app)
    r = c.post("/api/public/oidc/microsoft/start", json={"tenant": "acme", "purpose": "login"})
    u = r.json()["url"]
    assert f"/{ACME_TID}/" in u and f"client_id={ACME_APP}" in u
    # ... and a token from the Globex directory is not accepted by Acme's configuration
    ms.tamper = lambda cl: {**cl, "tid": GLOBEX_TID, "iss": f"https://login.microsoftonline.com/{GLOBEX_TID}/v2.0"}
    code, state = ms.authorize(u, oid=oid, email="gina@globex.example", tid=ACME_TID)
    assert "sso_error=" in landed(c.get(f"/api/public/oidc/microsoft/callback?code={code}&state={state}"))
    ms.tamper = None
    # even a valid Acme-directory login with the same oid is not linked in Acme
    c, cb = sign_in(app, ms, "acme", oid=oid, email="gina@globex.example", tid=ACME_TID)
    assert "sso_error=not_linked" in landed(cb)
    # the code exchange used Acme's own client credentials
    assert all(d["client_id"] == ACME_APP for url, d in ms.token_requests if ACME_TID in url)


def test_disabled_microsoft_cannot_be_used_and_sessions_are_revoked(app, ms):
    adm = setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    asha = login(app, "asha", "tester", tenant="acme", platform=False)
    oid = str(uuid.uuid4())
    sign_in(app, ms, "acme", oid=oid, email="asha@acme.example", tid=ACME_TID, c=asha, path="/api/t/acme/auth/microsoft/link-start")
    ms_session, _ = sign_in(app, ms, "acme", oid=oid, email="asha@acme.example", tid=ACME_TID)
    assert ms_session.get("/api/t/acme/auth/me").status_code == 200
    assert adm.put("/api/t/acme/admin/sso/policy", json={"password_enabled": True, "microsoft_enabled": False}).status_code == 200
    assert ms_session.get("/api/t/acme/auth/me").status_code == 401          # Microsoft sessions revoked
    assert asha.get("/api/t/acme/auth/me").status_code == 200                # password session kept
    assert client(app).post("/api/public/oidc/microsoft/start", json={"tenant": "acme", "purpose": "login"}).status_code == 403
    assert client(app).get("/api/public/tenants/acme/auth-config").json()["microsoft"] is False


def test_microsoft_only_policy_requires_confirmation(app, ms):
    adm = setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    assert adm.put("/api/t/acme/admin/sso/policy", json={"password_enabled": False, "microsoft_enabled": False,
                                                         "confirm": True}).status_code == 409   # never both off
    r = adm.put("/api/t/acme/admin/sso/policy", json={"password_enabled": False, "microsoft_enabled": True})
    assert r.status_code == 409                                                               # needs confirmation
    r = adm.put("/api/t/acme/admin/sso/policy", json={"password_enabled": False, "microsoft_enabled": True, "confirm": True})
    assert r.status_code == 200
    assert client(app).post("/api/t/acme/auth/login", json={"username": "acme-admin", "password": "Password-12345"}).status_code == 403
    # password sessions (including the admin's own) end with the switch
    assert adm.get("/api/t/acme/auth/me").status_code == 401


def test_invitation_redeemed_with_microsoft_requires_matching_email(app, ms):
    adm = setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    tok = adm.post("/api/t/acme/admin/invitations", json={"email": "dev@acme.example", "role": "developer"}).json()["link"].split("#")[1]
    c, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="someone-else@acme.example", tid=ACME_TID,
                    purpose="invite", invite_token=tok)
    assert "sso_error=email_mismatch" in landed(cb)
    oid = str(uuid.uuid4())
    c, cb = sign_in(app, ms, "acme", oid=oid, email="dev@acme.example", tid=ACME_TID, purpose="invite", invite_token=tok)
    assert landed(cb) == "/acme/projects"
    me = c.get("/api/t/acme/auth/me").json()
    assert me["role"] == "developer" and me["microsoft_linked"]
    # no password: password sign-in impossible for this account
    assert client(app).post("/api/t/acme/auth/login", json={"username": me["username"], "password": "Any-password-123"}).status_code == 401
    # and Microsoft authentication alone gives no project access
    adm.post("/api/t/acme/admin/targets", json={"host": "app.example.com"})
    adm.post("/api/t/acme/projects", json={"name": "P", "base_url": "https://app.example.com/"})
    assert c.get("/api/t/acme/projects").json() == []


def test_domain_and_group_restrictions_and_provisioning(app, ms):
    group = "33333333-3333-3333-3333-333333333333"
    setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret", allowed_domains=["acme.example"],
              allowed_group_ids=[group], provisioning="auto_domain", auto_role="developer")
    _, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="x@gmail.com", tid=ACME_TID, groups=[group])
    assert "sso_error=domain_not_allowed" in landed(cb)
    _, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="x@acme.example", tid=ACME_TID, groups=[])
    assert "sso_error=group_not_allowed" in landed(cb)
    # provisioning: new account, configured role, no project access
    c, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="newbie@acme.example", tid=ACME_TID, groups=[group])
    assert landed(cb) == "/acme/projects"
    me = c.get("/api/t/acme/auth/me").json()
    assert me["role"] == "developer" and me["username"] == "newbie"
    # provisioning never takes over an existing account with the same e-mail
    make_user("pat", "tester", tenant="acme", platform=False, email="pat@acme.example")
    _, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="pat@acme.example", tid=ACME_TID, groups=[group])
    assert "sso_error=account_exists" in landed(cb)
    with session_scope() as db:
        assert db.scalars(select(ExternalIdentity).join(User, User.id == ExternalIdentity.user_id)
                          .where(User.username == "pat")).first() is None


def test_unical_microsoft_settings_are_super_admin_only(app, admin, ms):
    ops = login(app, "ops", "admin", platform=False)
    assert ops.get("/api/admin/sso").status_code == 403
    assert admin.get("/api/admin/sso").json()["platform_organization"] is True


def test_unverified_email_claim_cannot_redeem_an_invitation(app, ms):
    adm = setup_org(app, "acme", ACME_TID, ACME_APP, "acme-secret")
    tok = adm.post("/api/t/acme/admin/invitations", json={"email": "boss@acme.example", "role": "developer"}).json()["link"].split("#")[1]
    # attacker's directory account has UPN eve@..., with a self-set `email` claim equal to the invitee
    ms.tamper = lambda cl: {**cl, "preferred_username": "eve@acme.example", "email": "boss@acme.example"}
    _, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="boss@acme.example", tid=ACME_TID, purpose="invite", invite_token=tok)
    assert "sso_error=email_mismatch" in landed(cb)
    # marked verified by Microsoft (xms_edov) -> accepted
    ms.tamper = lambda cl: {**cl, "preferred_username": "b.oss@acme.example", "email": "boss@acme.example", "xms_edov": True}
    _, cb = sign_in(app, ms, "acme", oid=str(uuid.uuid4()), email="boss@acme.example", tid=ACME_TID, purpose="invite", invite_token=tok)
    assert landed(cb) == "/acme/projects"
    ms.tamper = None
