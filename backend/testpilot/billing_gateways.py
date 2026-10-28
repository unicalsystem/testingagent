"""Payment gateways (release 1.9): Razorpay (INR, Orders API + Checkout) and PayPal (USD, Orders v2 redirect).

Only server-side calls; card / UPI / PayPal details never touch TestAgent. Every payment is confirmed on the
server (Razorpay signature + payment fetch, PayPal capture) before a licence is extended.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

# Tests point the gateways at local mocks; production never sets these.
TEST_OVERRIDE: dict[str, str] = {}
GATEWAY_CURRENCY = {"razorpay": "INR", "paypal": "USD"}


class GatewayError(Exception):
    pass


def _client(auth=None) -> httpx.Client:
    return httpx.Client(timeout=30, trust_env=False, follow_redirects=False, auth=auth,
                        headers={"User-Agent": "TestAgent-Billing/1.9"})


def _err(r: httpx.Response, what: str) -> GatewayError:
    try:
        j = r.json()
        msg = (j.get("error") or {}).get("description") if isinstance(j.get("error"), dict) else j.get("message") or j.get("error_description") or j.get("name")
    except ValueError:
        msg = r.text[:200]
    return GatewayError(f"{what} failed (HTTP {r.status_code}): {str(msg or '')[:300]}")


# --------------------------------------------------------------------------- Razorpay
class Razorpay:
    def __init__(self, key_id: str, key_secret: str):
        if not key_id or not key_secret:
            raise GatewayError("Razorpay is not configured")
        self.key_id, self.secret = key_id, key_secret
        self.base = TEST_OVERRIDE.get("razorpay", "https://api.razorpay.com")

    def _req(self, method: str, path: str, what: str, **kw) -> dict:
        try:
            with _client((self.key_id, self.secret)) as c:
                r = c.request(method, self.base + path, **kw)
        except httpx.HTTPError as e:
            raise GatewayError(f"{what}: Razorpay not reachable ({type(e).__name__})")
        if r.status_code >= 300:
            raise _err(r, what)
        return r.json()

    def create_order(self, amount: int, currency: str, receipt: str, notes: dict) -> dict:
        return self._req("POST", "/v1/orders", "Creating the Razorpay order",
                         json={"amount": amount, "currency": currency, "receipt": receipt[:40], "notes": notes})

    def fetch_order(self, order_id: str) -> dict:
        return self._req("GET", f"/v1/orders/{order_id}", "Reading the Razorpay order")

    def order_payments(self, order_id: str) -> list[dict]:
        return self._req("GET", f"/v1/orders/{order_id}/payments", "Reading the Razorpay payments").get("items", [])

    def fetch_payment(self, payment_id: str) -> dict:
        return self._req("GET", f"/v1/payments/{payment_id}", "Reading the Razorpay payment")

    def capture(self, payment_id: str, amount: int, currency: str) -> dict:
        return self._req("POST", f"/v1/payments/{payment_id}/capture", "Capturing the Razorpay payment",
                         json={"amount": amount, "currency": currency})

    def signature_ok(self, order_id: str, payment_id: str, signature: str) -> bool:
        good = hmac.new(self.secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, signature or "")

    def test(self) -> str:
        self._req("GET", "/v1/orders?count=1", "Checking the Razorpay keys")
        mode = "test mode" if self.key_id.startswith("rzp_test_") else "live mode"
        return f"Connected to Razorpay ({mode})"

    def settled_payment(self, order_id: str, amount: int, currency: str, payment_id: str = "") -> dict | None:
        """Returns the captured payment of the order (capturing an authorised one), or None if unpaid."""
        pays = [self.fetch_payment(payment_id)] if payment_id else self.order_payments(order_id)
        for p in pays:
            if p.get("order_id") != order_id or int(p.get("amount", -1)) != amount or p.get("currency") != currency:
                continue
            if p.get("status") == "authorized":
                p = self.capture(p["id"], amount, currency)
            if p.get("status") == "captured":
                return p
        return None


def razorpay_webhook_ok(body: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return False
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, signature or "")


# --------------------------------------------------------------------------- PayPal
class PayPal:
    def __init__(self, client_id: str, secret: str, mode: str = "sandbox"):
        if not client_id or not secret:
            raise GatewayError("PayPal is not configured")
        self.cid, self.secret = client_id, secret
        self.base = TEST_OVERRIDE.get("paypal") or ("https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com")
        self._token: str | None = None

    def token(self) -> str:
        if self._token:
            return self._token
        try:
            with _client((self.cid, self.secret)) as c:
                r = c.post(self.base + "/v1/oauth2/token", data={"grant_type": "client_credentials"})
        except httpx.HTTPError as e:
            raise GatewayError(f"PayPal not reachable ({type(e).__name__})")
        if r.status_code != 200:
            raise _err(r, "PayPal authentication")
        self._token = r.json()["access_token"]
        return self._token

    def _req(self, method: str, path: str, what: str, ok=(200, 201), **kw) -> tuple[int, dict]:
        try:
            with _client() as c:
                r = c.request(method, self.base + path, headers={"Authorization": f"Bearer {self.token()}",
                                                                  **kw.pop("headers", {})}, **kw)
        except httpx.HTTPError as e:
            raise GatewayError(f"{what}: PayPal not reachable ({type(e).__name__})")
        if r.status_code not in ok:
            raise _err(r, what)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def create_order(self, *, payment_id: int, amount: int, currency: str, description: str, return_url: str,
                     cancel_url: str, brand: str) -> dict:
        body = {"intent": "CAPTURE",
                "purchase_units": [{"reference_id": f"TA{payment_id}", "custom_id": str(payment_id),
                                    "description": description[:127],
                                    "amount": {"currency_code": currency, "value": f"{amount / 100:.2f}"}}],
                "payment_source": {"paypal": {"experience_context": {
                    "brand_name": brand[:127], "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                    "return_url": return_url, "cancel_url": cancel_url}}}}
        _, j = self._req("POST", "/v2/checkout/orders", "Creating the PayPal order", json=body,
                         headers={"PayPal-Request-Id": f"testagent-{payment_id}"})
        approve = next((ln["href"] for ln in j.get("links", []) if ln.get("rel") in ("payer-action", "approve")), "")
        if not j.get("id") or not approve:
            raise GatewayError("PayPal did not return an approval link")
        return {"id": j["id"], "approve_url": approve}

    def capture(self, order_id: str) -> dict:
        code, j = self._req("POST", f"/v2/checkout/orders/{order_id}/capture", "Capturing the PayPal payment",
                            ok=(200, 201, 422), json={}, headers={"PayPal-Request-Id": f"testagent-capture-{order_id}"})
        if code == 422:  # e.g. ORDER_ALREADY_CAPTURED: read the order instead
            _, j = self._req("GET", f"/v2/checkout/orders/{order_id}", "Reading the PayPal order")
        return j

    def test(self) -> str:
        self.token()
        return f"Connected to PayPal ({'live' if 'sandbox' not in self.base else 'sandbox'})"


def paypal_captured(order: dict, payment_id: int, amount: int, currency: str) -> str | None:
    """The capture id if the order is completed for exactly this payment and amount."""
    if order.get("status") != "COMPLETED":
        return None
    for pu in order.get("purchase_units", []):
        if pu.get("custom_id") not in (str(payment_id), None) and pu.get("reference_id") != f"TA{payment_id}":
            continue
        for cap in (pu.get("payments") or {}).get("captures", []):
            amt = cap.get("amount") or {}
            if cap.get("status") == "COMPLETED" and amt.get("currency_code") == currency \
                    and round(float(amt.get("value", "0")) * 100) == amount:
                return cap.get("id") or order.get("id")
    return None


def fmt(amount: int, currency: str) -> str:
    sym = {"INR": "₹", "USD": "$"}.get(currency, currency + " ")
    if currency == "INR":
        whole, frac = divmod(amount, 100)
        s = f"{whole:,}"
        # Indian digit grouping: 12,34,567
        if whole >= 1000:
            head, tail = str(whole)[:-3], str(whole)[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            s = ",".join(parts) + "," + tail
        return f"{sym}{s}" + (f".{frac:02d}" if frac else "")
    return f"{sym}{amount / 100:,.2f}"


def gateway_info(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"razorpay": {"enabled": bool(cfg["razorpay_enabled"] and cfg["razorpay_key_id"] and cfg["razorpay_key_secret_enc"]),
                         "currency": "INR"},
            "paypal": {"enabled": bool(cfg["paypal_enabled"] and cfg["paypal_client_id"] and cfg["paypal_client_secret_enc"]),
                       "currency": "USD"}}
