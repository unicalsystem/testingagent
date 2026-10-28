"""Plans & billing (release 1.9): self-service sign-up, free trial, read-only expiry, feature modules and limits,
coupons (extension and discount), Razorpay and PayPal payments against local mocks, invoices, permissions."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from .conftest import H

RZ_SECRET, RZ_HOOK = "rz-secret-123456", "rz-webhook-abcdef"
GW: dict = {"orders": {}, "captured": set()}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {"_form": raw.decode()}

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/v1/orders":
            return self._send(200 if self.headers.get("Authorization") else 401, {"items": []})
        m = re.match(r"^/v1/payments/(pay_\w+)$", p)
        if m:
            o = next((o for o in GW["orders"].values() if o.get("payment") == m.group(1)), None)
            if not o:
                return self._send(404, {"error": {"description": "no payment"}})
            return self._send(200, {"id": m.group(1), "order_id": o["id"], "amount": o["amount"], "currency": o["currency"],
                                    "status": "captured"})
        m = re.match(r"^/v1/orders/(order_\w+)/payments$", p)
        if m:
            o = GW["orders"].get(m.group(1))
            items = [{"id": o["payment"], "order_id": o["id"], "amount": o["amount"], "currency": o["currency"],
                      "status": "captured"}] if o and o.get("payment") else []
            return self._send(200, {"items": items})
        m = re.match(r"^/v2/checkout/orders/(PPORDER\w+)$", p)
        if m:
            return self._send(200, self._pp_order(m.group(1)))
        return self._send(404, {"message": p})

    def _pp_order(self, oid):
        o = GW["orders"][oid]
        done = oid in GW["captured"]
        return {"id": oid, "status": "COMPLETED" if done else "APPROVED",
                "purchase_units": [{"reference_id": o["ref"], "custom_id": o["custom"],
                                    "payments": {"captures": [{"id": "CAP" + oid, "status": "COMPLETED", "amount": o["amount"]}]} if done else {}}]}

    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._body()
        if p == "/v1/orders":
            oid = f"order_{len(GW['orders']) + 1}"
            GW["orders"][oid] = {"id": oid, "amount": body["amount"], "currency": body["currency"], "notes": body.get("notes")}
            return self._send(200, {"id": oid, "amount": body["amount"], "currency": body["currency"], "status": "created"})
        if p == "/v1/oauth2/token":
            return self._send(200, {"access_token": "pp-token", "expires_in": 3000})
        if p == "/v2/checkout/orders":
            oid = f"PPORDER{len(GW['orders']) + 1:08d}"
            pu = body["purchase_units"][0]
            GW["orders"][oid] = {"ref": pu["reference_id"], "custom": pu["custom_id"], "amount": pu["amount"]}
            return self._send(201, {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                                    "links": [{"rel": "payer-action", "href": f"https://paypal.example/checkout?token={oid}"}]})
        m = re.match(r"^/v2/checkout/orders/(PPORDER\w+)/capture$", p)
        if m:
            if m.group(1) in GW["captured"]:
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            GW["captured"].add(m.group(1))
            return self._send(201, self._pp_order(m.group(1)))
        return self._send(404, {"message": p})


@pytest.fixture
def gateways(monkeypatch):
    from testpilot import billing_gateways as bg
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setitem(bg.TEST_OVERRIDE, "razorpay", base)
    monkeypatch.setitem(bg.TEST_OVERRIDE, "paypal", base)
    GW["orders"].clear()
    GW["captured"].clear()
    yield base
    srv.shutdown()


def anon(app) -> TestClient:
    c = TestClient(app, base_url="https://testingagent.unical.in")
    c.headers.update(H)
    return c


def setup_billing(admin, **extra):
    r = admin.put("/api/platform/billing/settings", json={
        "self_signup": True, "razorpay_enabled": True, "razorpay_key_id": "rzp_test_ABC123", "razorpay_key_secret": RZ_SECRET,
        "razorpay_webhook_secret": RZ_HOOK, "paypal_enabled": True, "paypal_client_id": "pp-client", "paypal_client_secret": "pp-secret",
        "seller_tax_id": "36AABCU1234F1Z5", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def signup(app, slug="acme", username="asha"):
    c = anon(app)
    r = c.post("/api/public/registrations", json={
        "org_name": slug.title() + " Ltd", "slug": slug, "contact_name": "Asha Rao", "contact_email": f"{username}@{slug}.example",
        "country_code": "IN", "country": "India", "accept_terms": True, "accept_privacy": True, "confirm_adult": True,
        "username": username, "password": "Strong-pass-2026", "store_on_unical": True})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "created" and r.json()["redirect"] == f"/{slug}/billing?welcome=1"
    return c, f"/api/t/{slug}"


def plans(c, base):
    return {p["code"]: p for p in c.get(f"{base}/billing").json()["plans"]}


def test_signup_trial_limits_and_expiry(app, admin):
    setup_billing(admin)
    c, b = signup(app)
    me = c.get(f"{b}/auth/me").json()
    assert me["role"] == "tenant_owner" and me["licence"]["state"] == "none" and me["licence"]["read_only"]
    r = c.post(f"{b}/projects", json={"name": "P", "base_url": "https://acme.example"})
    assert r.status_code == 402 and "Choose a plan" in r.json()["detail"]
    ov = c.get(f"{b}/billing").json()
    assert ov["trial"]["available"] and ov["trial"]["days"] == 7 and ov["can_manage"]
    assert {"free-trial", "starter", "professional", "business"} <= {p["code"] for p in ov["plans"]}
    assert set(ov["currencies"]) == {"INR", "USD"}
    lic = c.post(f"{b}/billing/trial").json()
    assert lic["state"] == "trial" and lic["days_left"] == 7 and lic["plan"]["code"] == "free-trial"
    assert c.post(f"{b}/billing/trial").status_code == 409
    for i in range(2):
        assert c.post(f"{b}/projects", json={"name": f"P{i}", "base_url": "https://acme.example"}).status_code == 201
    r = c.post(f"{b}/projects", json={"name": "P3", "base_url": "https://acme.example"})
    assert r.status_code == 402 and "allows 2 projects" in r.json()["detail"]
    usage = c.get(f"{b}/billing").json()["usage"]
    assert usage["projects"] == 2 and usage["users"] == 1
    # storage was agreed at sign-up, so runs are not blocked by the storage choice
    assert c.get(f"{b}/system/status").json()["storage"]["run_block_reason"] is None
    # expire it: read-only, but reading and paying still work
    from testpilot.db import session_scope
    from testpilot.models import Tenant, TenantSubscription
    from sqlalchemy import select
    with session_scope() as db:
        tid = db.scalars(select(Tenant.id).where(Tenant.slug == "acme")).one()
        db.get(TenantSubscription, tid).current_period_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    r = c.post(f"{b}/projects", json={"name": "P4", "base_url": "https://acme.example"})
    assert r.status_code == 402 and "expired" in r.json()["detail"]
    assert c.get(f"{b}/projects").status_code == 200
    assert c.get(f"{b}/auth/me").json()["licence"]["state"] == "expired"
    assert c.post(f"{b}/billing/quote", json={"plan_id": plans(c, b)["starter"]["id"], "months": 1, "currency": "INR"}).status_code == 200


def test_unical_and_legacy_organizations_are_unlimited(app, admin):
    setup_billing(admin)
    for i in range(3):
        assert admin.post("/api/projects", json={"name": f"U{i}", "base_url": "https://unical.example"}).status_code == 201
    assert admin.get("/api/auth/me").json()["licence"]["state"] == "unlimited"


def test_feature_modules_and_platform_licence(app, admin, gateways):
    setup_billing(admin)
    c, b = signup(app, "globex", "gina")
    p = plans(c, b)
    lic = admin.get("/api/platform/billing/licences").json()
    row = next(x for x in lic if x["slug"] == "globex")
    assert row["state"] == "none"
    end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = admin.put(f"/api/platform/billing/licences/{row['tenant_id']}", json={"plan_id": p["starter"]["id"], "status": "active",
                                                                             "current_period_end": end, "note": "pilot"})
    assert r.status_code == 200, r.text
    pid = c.post(f"{b}/projects", json={"name": "G", "base_url": "https://globex.example"}).json()["id"]
    r = c.post(f"{b}/projects/{pid}/mobile/providers", json={"kind": "emulator", "name": "Phones"})
    assert r.status_code == 402 and "Mobile app testing is not included" in r.json()["detail"]
    r = c.post(f"{b}/projects/{pid}/monitors", json={"name": "m", "url": "https://globex.example"})
    assert r.status_code == 402 and "monitoring" in r.json()["detail"].lower()
    me = c.get(f"{b}/auth/me").json()
    assert me["licence"]["features"]["api_testing"] and not me["licence"]["features"]["mobile"]


def test_coupons_extend_and_discount(app, admin):
    setup_billing(admin)
    c, b = signup(app, "initech", "ian")
    p = plans(c, b)
    r = admin.post("/api/platform/billing/coupons", json={"code": "pilot30", "kind": "extend", "extend_days": 30,
                                                          "extend_plan_id": p["professional"]["id"], "description": "Pilot"})
    assert r.status_code == 201 and r.json()["code"] == "PILOT30"
    admin.post("/api/platform/billing/coupons", json={"code": "LAUNCH20", "kind": "percent", "percent_off": 20})
    admin.post("/api/platform/billing/coupons", json={"code": "ONLYGLOBEX", "kind": "extend", "extend_days": 5, "tenant_slugs": ["globex"]})
    assert c.post(f"{b}/billing/redeem", json={"code": "nope"}).status_code == 422
    assert "not valid for your organization" in c.post(f"{b}/billing/redeem", json={"code": "ONLYGLOBEX"}).json()["detail"]
    assert "discount coupon" in c.post(f"{b}/billing/redeem", json={"code": "launch20"}).json()["detail"]
    r = c.post(f"{b}/billing/redeem", json={"code": "pilot30"})
    assert r.status_code == 200, r.text
    lic = r.json()["licence"]
    assert lic["state"] == "active" and lic["plan"]["code"] == "professional" and 29 <= lic["days_left"] <= 31
    assert "already used" in c.post(f"{b}/billing/redeem", json={"code": "PILOT30"}).json()["detail"]
    q = c.post(f"{b}/billing/quote", json={"plan_id": p["business"]["id"], "months": 12, "currency": "INR", "coupon": "LAUNCH20"}).json()
    assert q["discount"] == q["subtotal"] // 5 and q["tax"] == round((q["subtotal"] - q["discount"]) * 0.18)
    assert q["total"] == q["subtotal"] - q["discount"] + q["tax"] and q["carry_over_days"] > 0
    q2 = c.post(f"{b}/billing/quote", json={"plan_id": p["starter"]["id"], "months": 1, "currency": "INR", "coupon": "PILOT30"}).json()
    assert "licence-extension coupon" in q2["coupon_problem"] and q2["discount"] == 0
    red = admin.get(f"/api/platform/billing/coupons").json()
    assert next(x for x in red if x["code"] == "PILOT30")["redemptions"] == 1


def test_razorpay_checkout_verify_invoice_and_webhook(app, admin, gateways):
    setup_billing(admin)
    c, b = signup(app, "hooli", "hank")
    p = plans(c, b)
    s = admin.get("/api/platform/billing/settings").json()
    assert s["razorpay_key_secret_set"] and "rz-secret" not in json.dumps(s) and s["razorpay_key_secret_hint"].endswith("3456")
    assert admin.post("/api/platform/billing/gateways/razorpay/test").json() == {"ok": True, "message": "Connected to Razorpay (test mode)"}
    r = c.post(f"{b}/billing/checkout", json={"plan_id": p["professional"]["id"], "months": 1, "currency": "INR", "gateway": "razorpay",
                                               "billing_name": "Hooli Pvt Ltd", "billing_tax_id": "36aabch1234f1z5"})
    assert r.status_code == 200, r.text
    co = r.json()
    assert co["key_id"] == "rzp_test_ABC123" and co["currency"] == "INR" and co["amount"] == 799900 + round(799900 * 0.18)
    GW["orders"][co["order_id"]]["payment"] = "pay_1"
    bad = c.post(f"{b}/billing/razorpay/verify", json={"payment_id": co["payment_id"], "razorpay_order_id": co["order_id"],
                                                         "razorpay_payment_id": "pay_1", "razorpay_signature": "0" * 64})
    assert bad.status_code == 422
    sig = hmac.new(RZ_SECRET.encode(), f"{co['order_id']}|pay_1".encode(), hashlib.sha256).hexdigest()
    ok = c.post(f"{b}/billing/razorpay/verify", json={"payment_id": co["payment_id"], "razorpay_order_id": co["order_id"],
                                                        "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert ok.status_code == 200, ok.text
    assert ok.json()["payment"]["status"] == "paid" and ok.json()["licence"]["plan"]["code"] == "professional"
    assert 28 <= ok.json()["licence"]["days_left"] <= 32
    inv = c.get(f"{b}/billing/payments/{co['payment_id']}/invoice")
    assert inv.status_code == 200 and "Tax invoice" in inv.text and "GST 18%" in inv.text and "36AABCH1234F1Z5" in inv.text
    assert re.search(r"TA-\d{4}-00001", inv.text)
    # verifying twice is harmless
    assert c.post(f"{b}/billing/razorpay/verify", json={"payment_id": co["payment_id"], "razorpay_order_id": co["order_id"],
                                                         "razorpay_payment_id": "pay_1", "razorpay_signature": sig}).status_code == 200
    # renewal of the same plan, confirmed only by the webhook: extends from the current end
    co2 = c.post(f"{b}/billing/checkout", json={"plan_id": p["professional"]["id"], "months": 3, "currency": "INR",
                                                 "gateway": "razorpay"}).json()
    ev = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_2", "order_id": co2["order_id"], "amount": co2["amount"], "currency": "INR", "status": "captured"}}}}).encode()
    a = anon(app)
    assert a.post("/api/public/billing/razorpay/webhook", content=ev, headers={"X-Razorpay-Signature": "bad"}).status_code == 400
    good = hmac.new(RZ_HOOK.encode(), ev, hashlib.sha256).hexdigest()
    for _ in range(2):
        assert a.post("/api/public/billing/razorpay/webhook", content=ev, headers={"X-Razorpay-Signature": good}).json()["ok"]
    lic = c.get(f"{b}/billing").json()["licence"]
    assert 115 <= lic["days_left"] <= 125
    pays = c.get(f"{b}/billing").json()["payments"]
    assert [x["status"] for x in pays[:2]] == ["paid", "paid"] and pays[0]["invoice_no"].endswith("00002")
    assert len([x for x in admin.get("/api/platform/billing/payments").json() if x["slug"] == "hooli" and x["status"] == "paid"]) == 2


def test_paypal_checkout_and_capture(app, admin, gateways):
    setup_billing(admin)
    c, b = signup(app, "umbrella", "una")
    p = plans(c, b)
    assert c.post(f"{b}/billing/checkout", json={"plan_id": p["starter"]["id"], "currency": "INR", "gateway": "paypal"}).status_code == 422
    co = c.post(f"{b}/billing/checkout", json={"plan_id": p["starter"]["id"], "months": 12, "currency": "USD", "gateway": "paypal"}).json()
    assert co["approve_url"].startswith("https://paypal.example/checkout?token=")
    assert GW["orders"][co["order_id"]]["amount"] == {"currency_code": "USD", "value": f"{p['starter']['prices']['USD']['12'] / 100:.2f}"}
    r = c.post(f"{b}/billing/paypal/capture", json={"order_id": co["order_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["payment"]["status"] == "paid" and 360 <= r.json()["licence"]["days_left"] <= 370
    # a second capture (browser refresh) is idempotent
    assert c.post(f"{b}/billing/paypal/capture", json={"order_id": co["order_id"]}).json()["payment"]["status"] == "paid"


def test_permissions_and_validation(app, admin, tester):
    setup_billing(admin)
    c, b = signup(app, "wayne", "bruce")
    assert tester.get("/api/platform/billing/plans").status_code == 403
    assert c.get("/api/platform/billing/settings").status_code in (401, 403)
    # a QA engineer of the organization can see the plan but not buy
    inv = c.post(f"{b}/admin/users", json={"username": "qa1", "role": "qa_engineer", "password": "Strong-pass-2026", "email": "qa@wayne.example"})
    assert inv.status_code == 402  # no plan yet: even adding users waits for a plan
    c.post(f"{b}/billing/trial")
    assert c.post(f"{b}/admin/users", json={"username": "qa1", "role": "qa_engineer", "password": "Strong-pass-2026",
                                            "email": "qa@wayne.example"}).status_code in (200, 201)
    qa = anon(app)
    assert qa.post(f"{b}/auth/login", json={"username": "qa1", "password": "Strong-pass-2026"}).status_code == 200
    assert qa.post(f"{b}/billing/redeem", json={"code": "X"}).status_code == 403
    assert qa.get(f"{b}/billing").json()["can_manage"] is False
    bad = admin.post("/api/platform/billing/plans", json={"code": "Bad Code", "name": "x"})
    assert bad.status_code == 422
    nop = admin.post("/api/platform/billing/plans", json={"code": "pro-max", "name": "Pro Max"})
    assert nop.status_code == 422 and "price" in nop.text
    ok = admin.post("/api/platform/billing/plans", json={"code": "pro-max", "name": "Pro Max", "prices": {"INR": {"1": 100000}},
                                                         "features": {"ai": True}, "limits": {"users": "5", "projects": ""}})
    assert ok.status_code == 201 and ok.json()["limits"]["users"] == 5 and ok.json()["limits"]["projects"] is None
    assert admin.put("/api/platform/billing/settings", json={"razorpay_key_id": "abc"}).status_code == 422
