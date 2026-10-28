"""Plans & billing (release 1.9).

  /api/t/<org>/billing/...        the organization's licence, plans, checkout, coupons, invoices
  /api/platform/billing/...       Unical Super Admin: plans, coupons, gateways, licences, payments
  /api/public/plans               public price list (landing page)
  /api/public/billing/razorpay/webhook   Razorpay server-to-server confirmation
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import billing, permissions, settings_store
from ..billing_gateways import GATEWAY_CURRENCY, GatewayError, PayPal, Razorpay, fmt, gateway_info, paypal_captured, razorpay_webhook_ok
from ..config import get_settings
from ..db import get_db
from ..models import (BillingCoupon, BillingCouponRedemption, BillingPayment, BillingPlan, Tenant, TenantSubscription,
                      User, utcnow)
from ..security import SecretsUnavailable
from .deps import audit, current_tenant, member, require_platform_admin

router = APIRouter(prefix="/billing", tags=["billing"])
platform = APIRouter(prefix="/platform/billing", tags=["billing"])
public = APIRouter(prefix="/public", tags=["billing"])
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,39}$")
PLAN_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


# --------------------------------------------------------------------------- helpers
def _manage(me: User) -> None:
    if not permissions.has(me.role, "tenant.settings"):
        raise HTTPException(403, "Only the organization's owner or administrators can manage the plan and payments")


def _razorpay(c: dict) -> Razorpay:
    return Razorpay(c["razorpay_key_id"], settings_store.unseal(c["razorpay_key_secret_enc"]))


def _paypal(c: dict) -> PayPal:
    return PayPal(c["paypal_client_id"], settings_store.unseal(c["paypal_client_secret_enc"]), c["paypal_mode"])


def payment_out(p: BillingPayment) -> dict:
    return {"id": p.id, "plan_name": p.plan_name, "months": p.months, "period": billing.PERIODS.get(p.months, f"{p.months} months"),
            "currency": p.currency, "amount": p.amount, "amount_display": fmt(p.amount, p.currency), "discount": p.discount,
            "coupon_code": p.coupon_code, "gateway": p.gateway, "status": p.status, "error": p.error, "invoice_no": p.invoice_no,
            "period_start": p.period_start, "period_end": p.period_end, "created_at": p.created_at, "paid_at": p.paid_at}


def ent_out(db: Session, t: Tenant) -> dict:
    e = billing.entitlements(db, t)
    return {**e, "ends_at": e["ends_at"].isoformat() if e["ends_at"] else None}


def catalog(db: Session) -> dict:
    c = billing.cfg(db)
    g = gateway_info(c)
    return {"plans": [billing.plan_out(p) for p in billing.public_plans(db)], "features": billing.FEATURES,
            "limits": billing.LIMITS, "periods": {str(k): v for k, v in billing.PERIODS.items()},
            "currencies": [cur for gw, cur in GATEWAY_CURRENCY.items() if g[gw]["enabled"]] or ["INR"],
            "gateways": g, "tax": {"label": c["tax_label"], "INR": c["tax_percent_inr"], "USD": c["tax_percent_usd"]},
            "self_signup": bool(c["self_signup"])}


# --------------------------------------------------------------------------- public
@public.get("/plans")
def public_plans(db: Session = Depends(get_db)):
    return catalog(db)


@public.post("/billing/razorpay/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    c = billing.cfg(db)
    secret = settings_store.unseal(c["razorpay_webhook_secret_enc"])
    if not razorpay_webhook_ok(body, request.headers.get("x-razorpay-signature", ""), secret):
        raise HTTPException(400, "Invalid signature")
    try:
        ev = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if ev.get("event") not in ("payment.captured", "order.paid"):
        return {"ok": True, "ignored": ev.get("event")}
    pay = ((ev.get("payload") or {}).get("payment") or {}).get("entity") or {}
    order_id = pay.get("order_id") or (((ev.get("payload") or {}).get("order") or {}).get("entity") or {}).get("id")
    p = db.scalars(select(BillingPayment).where(BillingPayment.gateway == "razorpay",
                                                BillingPayment.gateway_order_id == (order_id or ""))).first()
    if not p:
        return {"ok": True, "ignored": "unknown order"}
    if pay and (int(pay.get("amount", -1)) != p.amount or pay.get("currency") != p.currency or pay.get("status") != "captured"):
        return {"ok": True, "ignored": "amount, currency or status mismatch"}
    billing.fulfil(db, p.id, pay.get("id") or order_id)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- organization
class CheckoutIn(BaseModel):
    plan_id: int
    months: int = 1
    currency: str = Field(default="INR", pattern="^(INR|USD)$")
    coupon: str = Field(default="", max_length=40)
    gateway: str = Field(default="razorpay", pattern="^(razorpay|paypal)$")
    billing_name: str = Field(default="", max_length=200)
    billing_tax_id: str = Field(default="", max_length=40)


class QuoteIn(BaseModel):
    plan_id: int
    months: int = 1
    currency: str = Field(default="INR", pattern="^(INR|USD)$")
    coupon: str = Field(default="", max_length=40)


class RazorpayVerifyIn(BaseModel):
    payment_id: int
    razorpay_order_id: str = Field(max_length=120)
    razorpay_payment_id: str = Field(max_length=120)
    razorpay_signature: str = Field(max_length=200)


class PayPalCaptureIn(BaseModel):
    order_id: str = Field(min_length=5, max_length=120)


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=40)


@router.get("")
def overview(request: Request, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    sub = billing.subscription(db, t.id)
    pays = db.scalars(select(BillingPayment).where(BillingPayment.tenant_id == t.id, BillingPayment.status != "created")
                      .order_by(BillingPayment.id.desc()).limit(50)).all()
    tp = billing.trial_plan(db)
    return {"licence": ent_out(db, t), "usage": billing.usage(db, t.id), **catalog(db),
            "trial": {"available": bool(tp and not sub.trial_used and sub.status not in ("legacy",)), "days": tp.trial_days if tp else 0,
                      "plan_id": tp.id if tp else None},
            "razorpay_key_id": billing.cfg(db)["razorpay_key_id"] if gateway_info(billing.cfg(db))["razorpay"]["enabled"] else "",
            "payments": [payment_out(p) for p in pays], "can_manage": permissions.has(me.role, "tenant.settings"),
            "billing_name": sub.billing_name or t.name, "billing_tax_id": sub.billing_tax_id, "tenant_name": t.name}


@router.post("/trial")
def trial(request: Request, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    _manage(me)
    end = billing.start_trial(db, t)
    audit(db, request, me, "billing_trial_started", t.slug, ends_at=end.isoformat())
    db.commit()
    return ent_out(db, t)


@router.post("/quote")
def get_quote(body: QuoteIn, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    plan = db.get(BillingPlan, body.plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return billing.quote(db, t, plan, body.months, body.currency, body.coupon)


@router.post("/checkout")
def checkout(body: CheckoutIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member),
             t: Tenant = Depends(current_tenant)):
    _manage(me)
    c = billing.cfg(db)
    gws = gateway_info(c)
    if not gws[body.gateway]["enabled"]:
        raise HTTPException(409, f"{body.gateway.title()} payments are not available at the moment")
    if GATEWAY_CURRENCY[body.gateway] != body.currency:
        raise HTTPException(422, f"{body.gateway.title()} payments are in {GATEWAY_CURRENCY[body.gateway]}")
    plan = db.get(BillingPlan, body.plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    q = billing.quote(db, t, plan, body.months, body.currency, body.coupon)
    if body.coupon.strip() and q["coupon_problem"]:
        raise HTTPException(422, q["coupon_problem"])
    if q["total"] <= 0:
        raise HTTPException(422, "The total is zero; ask Unical for a licence-extension coupon instead")
    coupon = billing.find_coupon(db, q["coupon"]) if q["coupon"] else None
    p = BillingPayment(tenant_id=t.id, plan_id=plan.id, plan_name=plan.name, months=body.months, currency=body.currency,
                       subtotal=q["subtotal"], discount=q["discount"], tax=q["tax"], tax_label=q["tax_label"], amount=q["total"],
                       coupon_id=coupon.id if coupon else None, coupon_code=coupon.code if coupon else "", gateway=body.gateway,
                       billing_name=body.billing_name.strip() or t.name, billing_tax_id=body.billing_tax_id.strip().upper(),
                       created_by=me.id)
    db.add(p)
    db.flush()
    desc = f"TestAgent {plan.name} — {billing.PERIODS[body.months]} — {t.name}"
    try:
        if body.gateway == "razorpay":
            o = _razorpay(c).create_order(p.amount, p.currency, f"TA-{p.id}", {"tenant": t.slug, "payment_id": str(p.id), "plan": plan.code})
            p.gateway_order_id = o["id"]
            out = {"gateway": "razorpay", "payment_id": p.id, "key_id": c["razorpay_key_id"], "order_id": o["id"],
                   "amount": p.amount, "currency": p.currency, "name": c["seller_name"] or "TestAgent", "description": desc,
                   "prefill": {"name": me.display_name or me.username, "email": me.email}}
        else:
            base = f"{get_settings().public_url}/{t.slug}/billing"
            o = _paypal(c).create_order(payment_id=p.id, amount=p.amount, currency=p.currency, description=desc,
                                        return_url=f"{base}?paypal=return", cancel_url=f"{base}?paypal=cancel",
                                        brand=c["seller_name"] or "TestAgent")
            p.gateway_order_id = o["id"]
            out = {"gateway": "paypal", "payment_id": p.id, "approve_url": o["approve_url"], "order_id": o["id"]}
    except (GatewayError, SecretsUnavailable) as e:
        p.status, p.error = "failed", str(e)[:1000]
        db.commit()
        raise HTTPException(502, str(e))
    audit(db, request, me, "billing_checkout_started", plan.name, payment_id=p.id, gateway=body.gateway, amount=p.amount,
          currency=p.currency)
    db.commit()
    return out


def _own_payment(db: Session, pid: int, t: Tenant, *, lock: bool = False) -> BillingPayment:
    q = select(BillingPayment).where(BillingPayment.id == pid)
    p = db.scalars(q.with_for_update() if lock else q).first()
    if not p or p.tenant_id != t.id:
        raise HTTPException(404, "Payment not found")
    return p


@router.post("/razorpay/verify")
def razorpay_verify(body: RazorpayVerifyIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member),
                    t: Tenant = Depends(current_tenant)):
    p = _own_payment(db, body.payment_id, t)
    if p.gateway != "razorpay" or p.gateway_order_id != body.razorpay_order_id:
        raise HTTPException(422, "This payment does not belong to that order")
    if p.status == "paid":
        return {"payment": payment_out(p), "licence": ent_out(db, t)}
    c = billing.cfg(db)
    rz = _razorpay(c)
    if not rz.signature_ok(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(422, "The payment signature is not valid")
    try:
        paid = rz.settled_payment(p.gateway_order_id, p.amount, p.currency, body.razorpay_payment_id)
    except GatewayError as e:
        raise HTTPException(502, str(e))
    if not paid:
        raise HTTPException(409, "Razorpay has not confirmed the payment yet. It will be applied automatically when it is.")
    billing.fulfil(db, p.id, paid["id"])
    audit(db, request, me, "billing_payment_received", p.plan_name, payment_id=p.id, gateway="razorpay", amount=p.amount)
    db.commit()
    db.refresh(p)
    return {"payment": payment_out(p), "licence": ent_out(db, t)}


@router.post("/paypal/capture")
def paypal_capture(body: PayPalCaptureIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member),
                   t: Tenant = Depends(current_tenant)):
    p = db.scalars(select(BillingPayment).where(BillingPayment.gateway == "paypal", BillingPayment.gateway_order_id == body.order_id,
                                                BillingPayment.tenant_id == t.id)).first()
    if not p:
        raise HTTPException(404, "Payment not found")
    if p.status != "paid":
        try:
            order = _paypal(billing.cfg(db)).capture(body.order_id)
        except GatewayError as e:
            raise HTTPException(502, str(e))
        cap = paypal_captured(order, p.id, p.amount, p.currency)
        if not cap:
            p.error = f"PayPal order status: {order.get('status', 'unknown')}"
            db.commit()
            raise HTTPException(409, "PayPal has not completed the payment. If you were charged, contact support with your PayPal receipt.")
        billing.fulfil(db, p.id, cap)
        audit(db, request, me, "billing_payment_received", p.plan_name, payment_id=p.id, gateway="paypal", amount=p.amount)
        db.commit()
        db.refresh(p)
    return {"payment": payment_out(p), "licence": ent_out(db, t)}


@router.post("/payments/{pid}/check")
def check_payment(pid: int, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    """For a payment whose browser confirmation was lost: asks the gateway again."""
    p = _own_payment(db, pid, t)
    if p.status == "created" and p.gateway == "razorpay" and p.gateway_order_id:
        try:
            paid = _razorpay(billing.cfg(db)).settled_payment(p.gateway_order_id, p.amount, p.currency)
        except GatewayError as e:
            raise HTTPException(502, str(e))
        if paid:
            billing.fulfil(db, p.id, paid["id"])
            db.commit()
            db.refresh(p)
    return payment_out(p)


@router.post("/payments/{pid}/cancel")
def cancel_payment(pid: int, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    p = _own_payment(db, pid, t, lock=True)
    if p.status == "created":
        p.status = "cancelled"
        db.commit()
    return payment_out(p)


@router.post("/redeem")
def redeem(body: CouponIn, request: Request, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    _manage(me)
    c, end = billing.redeem_extension(db, t, body.code, me)
    audit(db, request, me, "billing_coupon_redeemed", c.code, ends_at=end.isoformat())
    db.commit()
    return {"message": f"Coupon applied. Your licence is valid until {end:%d %b %Y}.", "licence": ent_out(db, t)}


@router.get("/payments/{pid}/invoice")
def invoice(pid: int, db: Session = Depends(get_db), me: User = Depends(member), t: Tenant = Depends(current_tenant)):
    p = _own_payment(db, pid, t)
    if p.status != "paid":
        raise HTTPException(404, "No invoice for this payment")
    return HTMLResponse(billing.receipt_html(db, p), headers={
        "Content-Disposition": f'inline; filename="{p.invoice_no}.html"',
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


# --------------------------------------------------------------------------- platform (Unical Super Admin)
class PlanIn(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(default="", max_length=200)
    badge: str = Field(default="", max_length=40)
    is_trial: bool = False
    trial_days: int = Field(default=7, ge=1, le=365)
    is_public: bool = True
    is_active: bool = True
    sort_order: int = Field(default=0, ge=-1000, le=1000)
    prices: dict = Field(default_factory=dict)
    features: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    highlights: list[str] = Field(default_factory=list, max_length=20)


class CouponAdminIn(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    description: str = Field(default="", max_length=300)
    kind: str = Field(pattern="^(extend|percent|amount)$")
    extend_days: int = Field(default=0, ge=0, le=3650)
    extend_plan_id: int | None = None
    percent_off: int = Field(default=0, ge=0, le=100)
    amount_off: int = Field(default=0, ge=0)
    currency: str = Field(default="", pattern="^(|INR|USD)$")
    plan_ids: list[int] = Field(default_factory=list, max_length=50)
    tenant_slugs: list[str] = Field(default_factory=list, max_length=200)
    max_redemptions: int | None = Field(default=None, ge=1)
    valid_until: datetime | None = None
    is_active: bool = True


class SettingsIn(BaseModel):
    enforce: bool | None = None
    self_signup: bool | None = None
    razorpay_enabled: bool | None = None
    razorpay_key_id: str | None = Field(default=None, max_length=80)
    razorpay_key_secret: str | None = Field(default=None, max_length=200)
    razorpay_webhook_secret: str | None = Field(default=None, max_length=200)
    paypal_enabled: bool | None = None
    paypal_mode: str | None = Field(default=None, pattern="^(sandbox|live)$")
    paypal_client_id: str | None = Field(default=None, max_length=200)
    paypal_client_secret: str | None = Field(default=None, max_length=300)
    seller_name: str | None = Field(default=None, max_length=200)
    seller_address: str | None = Field(default=None, max_length=500)
    seller_tax_id: str | None = Field(default=None, max_length=40)
    seller_email: str | None = Field(default=None, max_length=254)
    tax_label: str | None = Field(default=None, max_length=20)
    tax_percent_inr: float | None = Field(default=None, ge=0, le=50)
    tax_percent_usd: float | None = Field(default=None, ge=0, le=50)
    invoice_prefix: str | None = Field(default=None, pattern=r"^[A-Z0-9-]{1,10}$")
    reminder_days: list[int] | None = Field(default=None, max_length=5)


class LicenceIn(BaseModel):
    plan_id: int | None = None
    status: str = Field(pattern="^(none|trialing|active|legacy)$")
    current_period_end: datetime | None = None
    note: str = Field(default="", max_length=2000)


def _clean_plan(body: PlanIn) -> dict:
    code = body.code.strip().lower()
    if not PLAN_CODE_RE.match(code):
        raise HTTPException(422, "Plan code: lower-case letters, digits and dashes")
    prices: dict = {}
    for cur, per in (body.prices or {}).items():
        if cur not in billing.CURRENCIES or not isinstance(per, dict):
            raise HTTPException(422, f"Unsupported currency {cur}")
        clean = {}
        for m, v in per.items():
            if str(m) not in {str(k) for k in billing.PERIODS}:
                raise HTTPException(422, f"Unsupported period {m}")
            if v in (None, ""):
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(422, f"Price {cur} {m}: whole minor units (paise / cents) expected")
            if iv < 0 or iv > 10**11:
                raise HTTPException(422, f"Price {cur} {m} out of range")
            clean[str(m)] = iv
        if clean:
            prices[cur] = clean
    if not body.is_trial and body.is_active and not prices:
        raise HTTPException(422, "A paid plan needs at least one price")
    limits = {}
    for k in billing.LIMITS:
        v = (body.limits or {}).get(k)
        if v in (None, ""):
            limits[k] = None
        else:
            try:
                limits[k] = max(0, int(v))
            except (TypeError, ValueError):
                raise HTTPException(422, f"Limit {k} must be a whole number or empty (unlimited)")
    return dict(code=code, name=body.name.strip(), tagline=body.tagline.strip(), badge=body.badge.strip(),
                is_trial=body.is_trial, trial_days=body.trial_days, is_public=body.is_public, is_active=body.is_active,
                sort_order=body.sort_order, prices=prices, features={k: bool((body.features or {}).get(k)) for k in billing.FEATURES},
                limits=limits, highlights=[h.strip()[:120] for h in body.highlights if h.strip()])


@platform.get("/plans")
def admin_plans(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    plans = db.scalars(select(BillingPlan).order_by(BillingPlan.sort_order, BillingPlan.id)).all()
    counts = dict(db.execute(select(TenantSubscription.plan_id, func.count()).group_by(TenantSubscription.plan_id)).all())
    return {"plans": [{**billing.plan_out(p, admin=True), "organizations": counts.get(p.id, 0)} for p in plans],
            "features": billing.FEATURES, "limits": billing.LIMITS, "periods": {str(k): v for k, v in billing.PERIODS.items()},
            "currencies": list(billing.CURRENCIES)}


@platform.post("/plans", status_code=201)
def admin_create_plan(body: PlanIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    data = _clean_plan(body)
    if db.scalars(select(BillingPlan.id).where(BillingPlan.code == data["code"])).first():
        raise HTTPException(409, "A plan with this code exists")
    p = BillingPlan(**data)
    db.add(p)
    db.flush()
    audit(db, request, me, "billing_plan_created", p.name, tenant_id=None, scope="platform", plan_id=p.id)
    db.commit()
    return billing.plan_out(p, admin=True)


@platform.put("/plans/{pid}")
def admin_update_plan(pid: int, body: PlanIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    p = db.get(BillingPlan, pid)
    if not p:
        raise HTTPException(404, "Plan not found")
    data = _clean_plan(body)
    if data["code"] != p.code and db.scalars(select(BillingPlan.id).where(BillingPlan.code == data["code"])).first():
        raise HTTPException(409, "A plan with this code exists")
    for k, v in data.items():
        setattr(p, k, v)
    audit(db, request, me, "billing_plan_updated", p.name, tenant_id=None, scope="platform", plan_id=p.id)
    db.commit()
    return billing.plan_out(p, admin=True)


@platform.delete("/plans/{pid}")
def admin_delete_plan(pid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    p = db.get(BillingPlan, pid)
    if not p:
        raise HTTPException(404, "Plan not found")
    used = db.scalar(select(func.count()).select_from(TenantSubscription).where(TenantSubscription.plan_id == p.id)) or 0
    if used:
        p.is_active, p.is_public = False, False  # keep it for the organizations on it
        msg = f"{used} organization(s) use this plan: it was retired (hidden, no new purchases) instead of deleted"
    else:
        db.delete(p)
        msg = "Plan deleted"
    audit(db, request, me, "billing_plan_deleted", p.name, tenant_id=None, scope="platform", retired=bool(used))
    db.commit()
    return {"message": msg}


def coupon_out(c: BillingCoupon) -> dict:
    return {"id": c.id, "code": c.code, "description": c.description, "kind": c.kind, "extend_days": c.extend_days,
            "extend_plan_id": c.extend_plan_id, "percent_off": c.percent_off, "amount_off": c.amount_off, "currency": c.currency,
            "plan_ids": c.plan_ids or [], "tenant_slugs": c.tenant_slugs or [], "max_redemptions": c.max_redemptions,
            "redemptions": c.redemptions, "valid_until": c.valid_until, "is_active": c.is_active, "created_at": c.created_at}


def _apply_coupon(c: BillingCoupon, body: CouponAdminIn) -> None:
    code = body.code.strip().upper()
    if not CODE_RE.match(code):
        raise HTTPException(422, "Coupon code: 3–40 letters, digits, - or _")
    if body.kind == "extend" and body.extend_days < 1:
        raise HTTPException(422, "Give the number of days the coupon adds")
    if body.kind == "percent" and not 1 <= body.percent_off <= 100:
        raise HTTPException(422, "Give a discount between 1 and 100 %")
    if body.kind == "amount" and (body.amount_off < 1 or not body.currency):
        raise HTTPException(422, "Give the discount amount (minor units) and its currency")
    c.code, c.description, c.kind = code, body.description.strip(), body.kind
    c.extend_days, c.extend_plan_id = body.extend_days, body.extend_plan_id
    c.percent_off, c.amount_off, c.currency = body.percent_off, body.amount_off, body.currency
    c.plan_ids, c.tenant_slugs = body.plan_ids, [s.strip().lower() for s in body.tenant_slugs if s.strip()]
    c.max_redemptions, c.valid_until, c.is_active = body.max_redemptions, body.valid_until, body.is_active


@platform.get("/coupons")
def admin_coupons(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    return [coupon_out(c) for c in db.scalars(select(BillingCoupon).order_by(BillingCoupon.id.desc())).all()]


@platform.post("/coupons", status_code=201)
def admin_create_coupon(body: CouponAdminIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    c = BillingCoupon(created_by=me.id)
    _apply_coupon(c, body)
    if billing.find_coupon(db, c.code):
        raise HTTPException(409, "A coupon with this code exists")
    db.add(c)
    db.flush()
    audit(db, request, me, "billing_coupon_created", c.code, tenant_id=None, scope="platform", kind=c.kind)
    db.commit()
    return coupon_out(c)


@platform.put("/coupons/{cid}")
def admin_update_coupon(cid: int, body: CouponAdminIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    c = db.get(BillingCoupon, cid)
    if not c:
        raise HTTPException(404, "Coupon not found")
    old = c.code
    _apply_coupon(c, body)
    other = billing.find_coupon(db, c.code)
    if c.code != old and other and other.id != c.id:
        raise HTTPException(409, "A coupon with this code exists")
    audit(db, request, me, "billing_coupon_updated", c.code, tenant_id=None, scope="platform")
    db.commit()
    return coupon_out(c)


@platform.delete("/coupons/{cid}", status_code=204)
def admin_delete_coupon(cid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    c = db.get(BillingCoupon, cid)
    if c:
        if c.redemptions:
            c.is_active = False  # keep the history of who used it
        else:
            db.delete(c)
        audit(db, request, me, "billing_coupon_deleted", c.code, tenant_id=None, scope="platform")
        db.commit()


@platform.get("/coupons/{cid}/redemptions")
def admin_coupon_redemptions(cid: int, db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    rows = db.execute(select(BillingCouponRedemption, Tenant).join(Tenant, Tenant.id == BillingCouponRedemption.tenant_id)
                      .where(BillingCouponRedemption.coupon_id == cid).order_by(BillingCouponRedemption.id.desc())).all()
    return [{"tenant": t.name, "slug": t.slug, "at": r.created_at, "payment_id": r.payment_id} for r, t in rows]


@platform.get("/settings")
def admin_settings(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    c = billing.cfg(db)
    return {**settings_store.public_view(c), "webhook_url": f"{get_settings().public_url}/api/public/billing/razorpay/webhook",
            "gateways": gateway_info(c)}


@platform.put("/settings")
def admin_put_settings(body: SettingsIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    upd = {k: v for k, v in body.model_dump(exclude_none=True).items()
           if k not in ("razorpay_key_secret", "razorpay_webhook_secret", "paypal_client_secret")}
    for field, key in (("razorpay_key_secret", "razorpay_key_secret"), ("razorpay_webhook_secret", "razorpay_webhook_secret"),
                       ("paypal_client_secret", "paypal_client_secret")):
        v = getattr(body, field)
        if v:
            try:
                upd[key + "_enc"], upd[key + "_hint"] = settings_store.seal(v.strip())
            except SecretsUnavailable as e:
                raise HTTPException(503, str(e))
    if "razorpay_key_id" in upd:
        upd["razorpay_key_id"] = upd["razorpay_key_id"].strip()
        if upd["razorpay_key_id"] and not re.match(r"^rzp_(test|live)_[A-Za-z0-9]+$", upd["razorpay_key_id"]):
            raise HTTPException(422, "A Razorpay key id looks like rzp_test_… or rzp_live_…")
    if "reminder_days" in upd:
        upd["reminder_days"] = sorted({max(0, min(60, int(d))) for d in upd["reminder_days"]}, reverse=True)
    settings_store.put(db, "billing", upd, me.id)
    audit(db, request, me, "billing_settings_updated", "billing", tenant_id=None, scope="platform",
          fields=sorted(k for k in body.model_dump(exclude_none=True)))
    db.commit()
    return admin_settings(db, me)


@platform.post("/gateways/{gw}/test")
def admin_test_gateway(gw: str, db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    c = billing.cfg(db)
    try:
        msg = (_razorpay(c) if gw == "razorpay" else _paypal(c) if gw == "paypal" else None)
        if msg is None:
            raise HTTPException(404, "Unknown gateway")
        return {"ok": True, "message": msg.test()}
    except (GatewayError, SecretsUnavailable) as e:
        return {"ok": False, "message": str(e)}


@platform.get("/licences")
def admin_licences(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    out = []
    for t in db.scalars(select(Tenant).order_by(Tenant.name)).all():
        sub = billing.subscription(db, t.id)
        e = billing.entitlements(db, t, sub=sub)
        plan = db.get(BillingPlan, sub.plan_id) if sub.plan_id else None
        paid = db.scalar(select(func.coalesce(func.sum(BillingPayment.amount), 0)).where(
            BillingPayment.tenant_id == t.id, BillingPayment.status == "paid", BillingPayment.currency == "INR")) or 0
        paid_usd = db.scalar(select(func.coalesce(func.sum(BillingPayment.amount), 0)).where(
            BillingPayment.tenant_id == t.id, BillingPayment.status == "paid", BillingPayment.currency == "USD")) or 0
        out.append({"tenant_id": t.id, "slug": t.slug, "name": t.name, "tenant_status": t.status, "status": sub.status,
                    "state": e["state"], "plan_id": sub.plan_id, "plan_name": plan.name if plan else "",
                    "current_period_end": sub.current_period_end, "trial_used": sub.trial_used, "note": sub.note,
                    "paid": {"INR": fmt(paid, "INR"), "USD": fmt(paid_usd, "USD")}})
    db.commit()
    return out


@platform.put("/licences/{tid}")
def admin_set_licence(tid: int, body: LicenceIn, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "Organization not found")
    sub = billing.subscription(db, t.id, lock=True)
    if body.status in ("trialing", "active"):
        if not body.plan_id or not db.get(BillingPlan, body.plan_id):
            raise HTTPException(422, "Choose the plan")
        if not body.current_period_end:
            raise HTTPException(422, "Give the licence end date")
    sub.plan_id = body.plan_id if body.status != "legacy" else None
    sub.status = body.status
    sub.current_period_end = body.current_period_end if body.status in ("trialing", "active") else None
    if body.status in ("trialing", "active"):
        sub.period_start = sub.period_start or utcnow()
    sub.note, sub.reminders = body.note, {}
    audit(db, request, me, "billing_licence_set", t.slug, tenant_id=t.id, scope="platform", status=body.status,
          plan_id=body.plan_id, ends_at=body.current_period_end.isoformat() if body.current_period_end else None)
    db.commit()
    return {"ok": True}


@platform.get("/payments")
def admin_payments(db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    rows = db.execute(select(BillingPayment, Tenant).join(Tenant, Tenant.id == BillingPayment.tenant_id)
                      .order_by(BillingPayment.id.desc()).limit(300)).all()
    return [{**payment_out(p), "tenant": t.name, "slug": t.slug, "gateway_order_id": p.gateway_order_id,
             "gateway_payment_id": p.gateway_payment_id, "billing_name": p.billing_name, "billing_tax_id": p.billing_tax_id}
            for p, t in rows]


@platform.get("/payments/{pid}/invoice")
def admin_invoice(pid: int, db: Session = Depends(get_db), _: User = Depends(require_platform_admin)):
    p = db.get(BillingPayment, pid)
    if not p or p.status != "paid":
        raise HTTPException(404, "No invoice for this payment")
    return HTMLResponse(billing.receipt_html(db, p), headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@platform.post("/payments/{pid}/mark-paid")
def admin_mark_paid(pid: int, request: Request, db: Session = Depends(get_db), me: User = Depends(require_platform_admin)):
    """For a payment the gateway confirmed outside TestAgent (e.g. lost confirmation) after checking the gateway dashboard."""
    p = db.get(BillingPayment, pid)
    if not p or p.status not in ("created", "failed"):
        raise HTTPException(409, "Only open or failed payments can be marked paid")
    billing.fulfil(db, p.id, p.gateway_payment_id or f"manual-{me.id}-{int(datetime.now(timezone.utc).timestamp())}")
    audit(db, request, me, "billing_payment_marked_paid", p.plan_name, tenant_id=p.tenant_id, scope="platform", payment_id=p.id)
    db.commit()
    return payment_out(p)
