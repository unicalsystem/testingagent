"""Plans, licences, quotas and payments (release 1.9).

An organization's licence (TenantSubscription) is one of:
  legacy    organizations that existed before 1.9, and the Unical organization: unlimited, no expiry
  none      registered but no plan chosen yet          -> read-only until a trial or plan starts
  trialing  the free trial plan until current_period_end
  active    a paid (or coupon / manually granted) plan until current_period_end
After current_period_end the organization is read-only: data stays visible and exportable; anything that
creates or runs is refused (HTTP 402) until it pays or redeems a coupon.

Enforcement happens in one place for most routes (guard_request, called by the `member` dependency for
unsafe methods): read-only state, feature modules mapped from URL paths, and simple +1 creation limits.
Bulk operations (mobile run groups) check their exact quantity in the route.
"""
from __future__ import annotations

import calendar
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import settings_store
from .billing_gateways import fmt
from .config import get_settings
from .models import (ApiMonitor, ApiRun, BillingCoupon, BillingCouponRedemption, BillingPayment, BillingPlan,
                     BillingUsage, Invitation, MobileRun, Project, Run, Tenant, TenantSubscription, User, utcnow)

log = logging.getLogger(__name__)

FEATURES: dict[str, str] = {
    "browser_automation": "Browser test automation",
    "ai": "AI features (test generation, failure analysis, automation, mobile AI agent)",
    "api_testing": "API testing",
    "monitoring": "API uptime monitoring",
    "mobile": "Mobile app testing",
    "connectors": "Connectors (Azure DevOps, Jira, GitHub, GitLab, webhooks)",
    "sso": "Microsoft 365 single sign-on",
    "custom_storage": "Own storage (SharePoint / Google Drive)",
}
LIMITS: dict[str, str] = {
    "users": "Team members",
    "projects": "Projects",
    "runs_per_month": "Automated browser runs / month",
    "api_runs_per_month": "API test runs / month",
    "monitors": "API monitors",
    "mobile_runs_per_month": "Mobile test runs / month",
    "ai_actions_per_month": "AI requests / month",
}
PERIODS = {1: "Monthly", 3: "Quarterly", 6: "Half-yearly", 12: "Annual"}
CURRENCIES = ("INR", "USD")

# Unsafe requests (relative to /api or /api/t/<org>) that need a feature module.
FEATURE_PATHS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/ai/|^/projects/\d+/automation-jobs|^/automation-(jobs|items)/"), "ai"),
    (re.compile(r"^/cases/\d+/runs$|^/runs/\d+/rerun$|^/test-runs/\d+/automate$"), "browser_automation"),
    (re.compile(r"^/(projects/\d+/)?api-(suites|cases|runs)"), "api_testing"),
    (re.compile(r"^/(projects/\d+/)?monitors"), "monitoring"),
    (re.compile(r"^/(projects/\d+/)?mobile"), "mobile"),
    (re.compile(r"^/(projects/\d+/)?integrations"), "connectors"),
    (re.compile(r"^/admin/sso"), "sso"),
    (re.compile(r"^/admin/storage/(pending|activate)"), "custom_storage"),
]
# AI requests that call Claude (approving or rejecting drafts does not).
AI_METERED = re.compile(r"/ai/|^/projects/\d+/automation-jobs$|^/automation-items/\d+/retry$")
# Requests that consume one unit of a limit when they succeed in creating something.
LIMIT_PATHS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/projects$"), "projects"),
    (re.compile(r"^/admin/(users|invitations)$"), "users"),
    (re.compile(r"^/projects/\d+/monitors$"), "monitors"),
    (re.compile(r"^/cases/\d+/runs$|^/runs/\d+/rerun$|^/test-runs/\d+/automate$"), "runs_per_month"),
    (re.compile(r"^/api-suites/\d+/runs$"), "api_runs_per_month"),
    (re.compile(r"^/projects/\d+/mobile/run-groups$|^/mobile/run-groups/\d+/rerun$"), "mobile_runs_per_month"),
]
# Always allowed, even when read-only: signing in/out, paying, privacy rights.
OPEN_PATHS = re.compile(r"^/(auth|billing|privacy)(/|$)|^/admin/privacy-requests")
TENANT_PREFIX = re.compile(r"^/api(/t/[a-z0-9-]+)?")


class PaymentRequired(HTTPException):
    def __init__(self, detail: str):
        super().__init__(402, detail)


def cfg(db: Session) -> dict[str, Any]:
    return settings_store.get(db, "billing")


# --------------------------------------------------------------------------- time helpers
def add_months(d: datetime, months: int) -> datetime:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_key(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y-%m")


def _aware(d: datetime | None) -> datetime | None:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d


# --------------------------------------------------------------------------- plans
def plan_out(p: BillingPlan, *, admin: bool = False) -> dict:
    out = {"id": p.id, "code": p.code, "name": p.name, "tagline": p.tagline, "badge": p.badge, "is_trial": p.is_trial,
           "trial_days": p.trial_days, "prices": p.prices or {}, "features": {k: bool((p.features or {}).get(k)) for k in FEATURES},
           "limits": {k: (p.limits or {}).get(k) for k in LIMITS}, "highlights": p.highlights or [], "sort_order": p.sort_order}
    if admin:
        out.update(is_public=p.is_public, is_active=p.is_active, created_at=p.created_at, updated_at=p.updated_at)
    return out


def public_plans(db: Session) -> list[BillingPlan]:
    return db.scalars(select(BillingPlan).where(BillingPlan.is_public.is_(True), BillingPlan.is_active.is_(True))
                      .order_by(BillingPlan.sort_order, BillingPlan.id)).all()


def trial_plan(db: Session) -> BillingPlan | None:
    return db.scalars(select(BillingPlan).where(BillingPlan.is_trial.is_(True), BillingPlan.is_active.is_(True))
                      .order_by(BillingPlan.sort_order, BillingPlan.id)).first()


def price_of(p: BillingPlan, currency: str, months: int) -> int | None:
    v = ((p.prices or {}).get(currency) or {}).get(str(months))
    return int(v) if v not in (None, "") else None


# --------------------------------------------------------------------------- licence state
def subscription(db: Session, tenant_id: int, *, lock: bool = False) -> TenantSubscription:
    q = select(TenantSubscription).where(TenantSubscription.tenant_id == tenant_id)
    sub = db.scalars(q.with_for_update() if lock else q).first()
    if not sub:
        from sqlalchemy.dialects.postgresql import insert
        # Organizations created before 1.9 without a row (should not happen after the migration) stay unlimited.
        db.execute(insert(TenantSubscription).values(tenant_id=tenant_id, status="legacy", reminders={},
                                                     updated_at=utcnow()).on_conflict_do_nothing(index_elements=["tenant_id"]))
        sub = db.scalars(q.with_for_update() if lock else q).first()
    return sub


def on_tenant_created(db: Session, tenant_id: int) -> TenantSubscription:
    """New organizations (self-service or approved registrations) start without a plan."""
    sub = db.get(TenantSubscription, tenant_id)
    if not sub:
        sub = TenantSubscription(tenant_id=tenant_id, status="none", reminders={})
        db.add(sub)
        db.flush()
    return sub


def entitlements(db: Session, tenant: Tenant, *, sub: TenantSubscription | None = None) -> dict[str, Any]:
    now = utcnow()
    unlimited = {"state": "unlimited", "plan": None, "features": {k: True for k in FEATURES},
                 "limits": {k: None for k in LIMITS}, "ends_at": None, "days_left": None, "read_only": False,
                 "exempt": True}
    if tenant.slug == get_settings().default_tenant_slug or not cfg(db)["enforce"]:
        return unlimited
    sub = sub or subscription(db, tenant.id)
    if sub.status == "legacy":
        return unlimited
    plan = db.get(BillingPlan, sub.plan_id) if sub.plan_id else None
    end = _aware(sub.current_period_end)
    if sub.status == "none" or not plan or not end:
        state = "none"
    elif end <= now:
        state = "expired"
    else:
        state = "trial" if sub.status == "trialing" else "active"
    days_left = max(0, (end - now).days + (1 if (end - now).seconds else 0)) if end and end > now else 0
    return {"state": state, "plan": {"id": plan.id, "code": plan.code, "name": plan.name, "is_trial": plan.is_trial} if plan else None,
            "features": {k: bool((plan.features or {}).get(k)) for k in FEATURES} if plan else {k: False for k in FEATURES},
            "limits": {k: (plan.limits or {}).get(k) for k in LIMITS} if plan else {k: 0 for k in LIMITS},
            "ends_at": end, "days_left": days_left if state in ("trial", "active") else 0,
            "read_only": state in ("none", "expired"), "exempt": False, "trial_used": sub.trial_used}


def usage(db: Session, tenant_id: int) -> dict[str, int]:
    since = month_start()
    count = lambda q: db.scalar(q) or 0  # noqa: E731
    return {
        "users": count(select(func.count()).select_from(User).where(User.tenant_id == tenant_id, User.is_active.is_(True)))
        + count(select(func.count()).select_from(Invitation).where(Invitation.tenant_id == tenant_id, Invitation.used_at.is_(None),
                                                                     Invitation.revoked.is_(False), Invitation.expires_at > utcnow())),
        "projects": count(select(func.count()).select_from(Project).where(Project.tenant_id == tenant_id, Project.archived.is_(False))),
        "runs_per_month": count(select(func.count()).select_from(Run).where(Run.tenant_id == tenant_id, Run.created_at >= since)),
        "api_runs_per_month": count(select(func.count()).select_from(ApiRun).where(ApiRun.tenant_id == tenant_id, ApiRun.created_at >= since)),
        "monitors": count(select(func.count()).select_from(ApiMonitor).where(ApiMonitor.tenant_id == tenant_id)),
        "mobile_runs_per_month": count(select(func.count()).select_from(MobileRun).where(MobileRun.tenant_id == tenant_id,
                                                                                          MobileRun.created_at >= since)),
        "ai_actions_per_month": count(select(BillingUsage.count).where(BillingUsage.tenant_id == tenant_id, BillingUsage.month == month_key(),
                                                                        BillingUsage.metric == "ai_actions_per_month")),
    }


def _ent(db: Session, request: Request | None, tenant: Tenant) -> dict:
    if request is not None and getattr(request.state, "billing_ent", None) is not None:
        return request.state.billing_ent
    e = entitlements(db, tenant)
    if request is not None:
        request.state.billing_ent = e
    return e


def require_feature(db: Session, tenant: Tenant, feature: str, request: Request | None = None) -> None:
    e = _ent(db, request, tenant)
    if e["exempt"]:
        return
    if e["read_only"]:
        raise PaymentRequired(_read_only_msg(e))
    if not e["features"].get(feature):
        raise PaymentRequired(f"{FEATURES[feature]} is not included in your plan ({e['plan']['name'] if e['plan'] else 'none'}). "
                              "Upgrade on the Plans & billing page.")


def check_quota(db: Session, tenant: Tenant, metric: str, add: int = 1, request: Request | None = None) -> None:
    e = _ent(db, request, tenant)
    if e["exempt"]:
        return
    if e["read_only"]:
        raise PaymentRequired(_read_only_msg(e))
    limit = e["limits"].get(metric)
    if limit is None:
        return
    used = usage(db, tenant.id)[metric]
    if used + add > int(limit):
        raise PaymentRequired(f"Your plan allows {limit} {LIMITS[metric].lower()} and {used} are used"
                              + (f"; this needs {add} more" if add > 1 else "") + ". Upgrade on the Plans & billing page.")


def consume_ai(db: Session, tenant: Tenant, n: int = 1, request: Request | None = None) -> None:
    """Checks and records AI requests (the only metered usage without its own table)."""
    check_quota(db, tenant, "ai_actions_per_month", n, request)
    if _ent(db, request, tenant)["exempt"]:
        return
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(BillingUsage).values(tenant_id=tenant.id, month=month_key(), metric="ai_actions_per_month", count=n)
    db.execute(stmt.on_conflict_do_update(index_elements=["tenant_id", "month", "metric"],
                                          set_={"count": BillingUsage.count + n}))


def _read_only_msg(e: dict) -> str:
    if e["state"] == "none":
        return "Choose a plan or start the free trial to use TestAgent (Plans & billing)."
    return (f"Your {'free trial' if e['plan'] and e['plan']['is_trial'] else 'plan'} has expired, so this organization is read-only. "
            "Renew on the Plans & billing page or apply a coupon.")


def guard_request(db: Session, request: Request, tenant: Tenant) -> None:
    """Called for every unsafe request of a signed-in member (see deps.member)."""
    if getattr(request.state, "billing_guarded", False):
        return
    request.state.billing_guarded = True
    path = TENANT_PREFIX.sub("", request.url.path) or "/"
    if OPEN_PATHS.search(path):
        return
    e = _ent(db, request, tenant)
    if e["exempt"]:
        return
    if e["read_only"]:
        raise PaymentRequired(_read_only_msg(e))
    for rx, feature in FEATURE_PATHS:
        if rx.search(path):
            require_feature(db, tenant, feature, request)
            if feature == "ai" and AI_METERED.search(path):
                consume_ai(db, tenant, 1, request)
            break
    if request.method == "POST":
        for rx, metric in LIMIT_PATHS:
            if rx.search(path):
                check_quota(db, tenant, metric, 1, request)
                break


def monitor_block(db: Session, tenant_id: int) -> str | None:
    t = db.get(Tenant, tenant_id)
    if not t:
        return "organization not found"
    e = entitlements(db, t)
    if e["exempt"]:
        return None
    if e["read_only"]:
        return "the organization's plan has expired"
    if not e["features"]["monitoring"]:
        return "API monitoring is not included in the organization's plan"
    return None


# --------------------------------------------------------------------------- quotes, coupons, fulfilment
def tax_percent(c: dict, currency: str) -> float:
    return float(c["tax_percent_inr"] if currency == "INR" else c["tax_percent_usd"]) or 0.0


def find_coupon(db: Session, code: str, *, lock: bool = False) -> BillingCoupon | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    q = select(BillingCoupon).where(func.upper(BillingCoupon.code) == code)
    return db.scalars(q.with_for_update() if lock else q).first()


def coupon_problem(db: Session, c: BillingCoupon | None, tenant: Tenant, *, plan: BillingPlan | None = None,
                   currency: str | None = None) -> str | None:
    if not c or not c.is_active:
        return "This coupon code is not valid"
    if c.valid_until and _aware(c.valid_until) < utcnow():
        return "This coupon has expired"
    if c.max_redemptions is not None and c.redemptions >= c.max_redemptions:
        return "This coupon has been fully used"
    if c.tenant_slugs and tenant.slug not in c.tenant_slugs:
        return "This coupon is not valid for your organization"
    if plan is not None:
        if c.kind == "extend":
            return "This is a licence-extension coupon: apply it with “Redeem coupon”, not at checkout"
        if c.plan_ids and plan.id not in c.plan_ids:
            return "This coupon does not apply to the selected plan"
        if c.kind == "amount" and c.currency and currency and c.currency != currency:
            return f"This coupon is for payments in {c.currency}"
    if db.scalars(select(BillingCouponRedemption.id).where(BillingCouponRedemption.coupon_id == c.id,
                                                           BillingCouponRedemption.tenant_id == tenant.id)).first():
        return "Your organization has already used this coupon"
    return None


def quote(db: Session, tenant: Tenant, plan: BillingPlan, months: int, currency: str, coupon_code: str = "") -> dict:
    if months not in PERIODS:
        raise HTTPException(422, "Choose a billing period of 1, 3, 6 or 12 months")
    if currency not in CURRENCIES:
        raise HTTPException(422, "Unsupported currency")
    if plan.is_trial or not plan.is_active:
        raise HTTPException(422, "This plan cannot be purchased")
    base = price_of(plan, currency, months)
    if base is None:
        raise HTTPException(422, f"{plan.name} is not available for {months} month(s) in {currency}")
    c = cfg(db)
    coupon, discount, problem = None, 0, None
    if coupon_code.strip():
        coupon = find_coupon(db, coupon_code)
        problem = coupon_problem(db, coupon, tenant, plan=plan, currency=currency)
        if problem:
            coupon = None
        elif coupon.kind == "percent":
            discount = base * max(0, min(100, coupon.percent_off)) // 100
        elif coupon.kind == "amount":
            discount = min(base, coupon.amount_off)
    taxable = base - discount
    pct = tax_percent(c, currency)
    tax = int(round(taxable * pct / 100))
    total = taxable + tax
    sub = subscription(db, tenant.id)
    carry_days = carry_over_days(db, sub, plan, currency)
    return {"plan_id": plan.id, "plan_name": plan.name, "months": months, "currency": currency, "subtotal": base,
            "discount": discount, "coupon": coupon.code if coupon else "", "coupon_problem": problem,
            "tax_label": f"{c['tax_label']} {pct:g}%" if pct else "", "tax": tax, "total": total,
            "display": {"subtotal": fmt(base, currency), "discount": fmt(discount, currency), "tax": fmt(tax, currency),
                        "total": fmt(total, currency)},
            "carry_over_days": carry_days,
            "renews_same_plan": bool(sub.plan_id == plan.id and sub.status == "active" and _aware(sub.current_period_end)
                                     and _aware(sub.current_period_end) > utcnow())}


def carry_over_days(db: Session, sub: TenantSubscription, new_plan: BillingPlan, currency: str) -> int:
    """Switching from a paid plan with time left converts the remaining time by monthly list-price value."""
    end = _aware(sub.current_period_end)
    if sub.status != "active" or not sub.plan_id or sub.plan_id == new_plan.id or not end or end <= utcnow():
        return 0
    old = db.get(BillingPlan, sub.plan_id)
    remaining = (end - utcnow()).total_seconds() / 86400
    if not old or old.is_trial:
        return 0
    po, pn = price_of(old, currency, 1), price_of(new_plan, currency, 1)
    if not po or not pn:
        return int(remaining)
    return int(remaining * po / pn)


def _next_invoice(db: Session) -> str:
    row = settings_store.get(db, "billing")
    n = int(row.get("invoice_counter") or 0) + 1
    settings_store.put(db, "billing", {"invoice_counter": n}, None)
    return f"{row.get('invoice_prefix') or 'TA'}-{utcnow():%Y}-{n:05d}"


def apply_licence(db: Session, sub: TenantSubscription, plan: BillingPlan, *, months: int = 0, days: int = 0,
                  status: str = "active", carry_days: int = 0) -> tuple[datetime, datetime]:
    """Extends the same plan from its current end, or starts a (different / renewed after expiry) plan now."""
    now = utcnow()
    end = _aware(sub.current_period_end)
    same_running = sub.plan_id == plan.id and end and end > now and sub.status in ("active", "trialing")
    start = end if same_running else now
    new_end = add_months(start, months) if months else start
    new_end += timedelta(days=days + carry_days)
    sub.plan_id, sub.status = plan.id, status
    if not same_running:
        sub.period_start = now
    sub.current_period_end = new_end
    sub.reminders = {}
    return start, new_end


def fulfil(db: Session, payment_id: int, gateway_payment_id: str) -> BillingPayment:
    """Marks a payment paid and extends the licence. Idempotent (row-locked)."""
    p = db.scalars(select(BillingPayment).where(BillingPayment.id == payment_id).with_for_update()).first()
    if not p or p.status == "paid":
        return p
    sub = subscription(db, p.tenant_id, lock=True)
    plan = db.get(BillingPlan, p.plan_id) if p.plan_id else None
    if not plan:
        p.status, p.error = "failed", "The plan no longer exists; contact support for a refund"
        return p
    carry = carry_over_days(db, sub, plan, p.currency)
    start, end = apply_licence(db, sub, plan, months=p.months, carry_days=carry)
    p.status, p.paid_at, p.gateway_payment_id = "paid", utcnow(), gateway_payment_id[:120]
    p.period_start, p.period_end, p.invoice_no = start, end, _next_invoice(db)
    if p.billing_name:
        sub.billing_name, sub.billing_tax_id = p.billing_name, p.billing_tax_id
    if p.coupon_id:
        c = db.scalars(select(BillingCoupon).where(BillingCoupon.id == p.coupon_id).with_for_update()).first()
        if c and not db.scalars(select(BillingCouponRedemption.id).where(BillingCouponRedemption.coupon_id == c.id,
                                                                         BillingCouponRedemption.tenant_id == p.tenant_id)).first():
            c.redemptions += 1
            db.add(BillingCouponRedemption(coupon_id=c.id, tenant_id=p.tenant_id, payment_id=p.id, user_id=p.created_by))
    db.flush()
    _notify_paid(db, p)
    return p


def _notify_paid(db: Session, p: BillingPayment) -> None:
    from . import mailer
    t = db.get(Tenant, p.tenant_id)
    for u in _billing_contacts(db, p.tenant_id):
        mailer.try_send(db, u.email, f"Payment received — TestAgent {p.plan_name}",
                        f"Hello {u.display_name or u.username},\n\nThank you. We received {fmt(p.amount, p.currency)} for the "
                        f"{p.plan_name} plan ({PERIODS.get(p.months, f'{p.months} months')}) for {t.name}.\n"
                        f"Your licence is valid until {p.period_end:%d %b %Y}.\nInvoice: {p.invoice_no} — download it from "
                        f"Plans & billing: {get_settings().public_url}/{t.slug}/billing\n\nTestAgent · Unical Systems")


def _billing_contacts(db: Session, tenant_id: int) -> list[User]:
    return db.scalars(select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True), User.email != "",
                                         User.role.in_(("tenant_owner", "tenant_admin")))).all()


def redeem_extension(db: Session, tenant: Tenant, code: str, user: User | None) -> tuple[BillingCoupon, datetime]:
    c = find_coupon(db, code, lock=True)
    problem = coupon_problem(db, c, tenant)
    if problem:
        raise HTTPException(422, problem)
    if c.kind != "extend":
        raise HTTPException(422, "This is a discount coupon: enter it at checkout when you choose a paid plan")
    sub = subscription(db, tenant.id, lock=True)
    plan = db.get(BillingPlan, c.extend_plan_id) if c.extend_plan_id else (db.get(BillingPlan, sub.plan_id) if sub.plan_id else trial_plan(db))
    if not plan:
        raise HTTPException(422, "This coupon cannot be applied: no plan is available")
    if sub.status == "legacy":
        raise HTTPException(422, "Your organization has an unlimited licence; no coupon is needed")
    _, end = apply_licence(db, sub, plan, days=max(1, c.extend_days), status="trialing" if plan.is_trial else "active")
    c.redemptions += 1
    db.add(BillingCouponRedemption(coupon_id=c.id, tenant_id=tenant.id, user_id=user.id if user else None))
    db.flush()
    return c, end


def start_trial(db: Session, tenant: Tenant) -> datetime:
    sub = subscription(db, tenant.id, lock=True)
    if sub.status == "legacy":
        raise HTTPException(422, "Your organization has an unlimited licence")
    if sub.trial_used:
        raise HTTPException(409, "The free trial has already been used. Choose a plan or apply a coupon.")
    plan = trial_plan(db)
    if not plan:
        raise HTTPException(409, "No free trial is offered at the moment")
    sub.trial_used = True
    _, end = apply_licence(db, sub, plan, days=max(1, plan.trial_days), status="trialing")
    return end


# --------------------------------------------------------------------------- reminders (worker, daily)
def send_reminders() -> int:
    from . import mailer
    from .db import session_scope

    sent = 0
    with session_scope() as db:
        c = cfg(db)
        if not c["enforce"]:
            return 0
        days = sorted({int(d) for d in c.get("reminder_days") or [] if int(d) >= 0}, reverse=True)
        now = utcnow()
        subs = db.scalars(select(TenantSubscription).where(TenantSubscription.status.in_(("trialing", "active")),
                                                           TenantSubscription.current_period_end.is_not(None))).all()
        for s in subs:
            end = _aware(s.current_period_end)
            left = (end - now).total_seconds() / 86400
            t = db.get(Tenant, s.tenant_id)
            if not t or t.status != "active" or t.slug == get_settings().default_tenant_slug:
                continue
            key = None
            if left <= 0:
                key = "expired"
            else:
                for d in days:
                    if left <= d:
                        key = f"d{d}"
            if not key or (s.reminders or {}).get(key) == end.isoformat():
                continue
            plan = db.get(BillingPlan, s.plan_id) if s.plan_id else None
            what = "free trial" if plan and plan.is_trial else f"{plan.name if plan else ''} plan"
            subject = (f"Your TestAgent {what} has expired" if key == "expired"
                       else f"Your TestAgent {what} ends on {end:%d %b %Y}")
            for u in _billing_contacts(db, t.id):
                body = (f"Hello {u.display_name or u.username},\n\n"
                        + (f"The {what} of {t.name} has expired. Your data is safe and visible, but tests cannot run until "
                           "you renew." if key == "expired" else
                           f"The {what} of {t.name} ends on {end:%d %b %Y}.")
                        + f"\n\nChoose or renew a plan here: {get_settings().public_url}/{t.slug}/billing\n\nTestAgent · Unical Systems")
                if mailer.try_send(db, u.email, subject, body):
                    sent += 1
            s.reminders = {**(s.reminders or {}), key: end.isoformat()}
    return sent


# --------------------------------------------------------------------------- receipts
def receipt_html(db: Session, p: BillingPayment) -> str:
    c = cfg(db)
    t = db.get(Tenant, p.tenant_id)
    e = html.escape
    rows = [(f"TestAgent {p.plan_name} — {PERIODS.get(p.months, f'{p.months} months')}"
             + (f" ({p.period_start:%d %b %Y} – {p.period_end:%d %b %Y})" if p.period_start and p.period_end else ""), fmt(p.subtotal, p.currency))]
    if p.discount:
        rows.append((f"Discount{f' ({p.coupon_code})' if p.coupon_code else ''}", "−" + fmt(p.discount, p.currency)))
    if p.tax:
        rows.append((p.tax_label or "Tax", fmt(p.tax, p.currency)))
    body = "".join(f"<tr><td>{e(a)}</td><td style='text-align:right'>{e(b)}</td></tr>" for a, b in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Invoice {e(p.invoice_no)}</title>
<style>body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#111827;max-width:760px;margin:32px auto;padding:0 16px}}
h1{{color:#14305c;margin:0}}table{{width:100%;border-collapse:collapse;margin-top:16px}}td,th{{border-bottom:1px solid #e5e7eb;padding:8px}}
.m{{color:#6b7280}}.tot td{{font-weight:bold;border-top:2px solid #14305c}}</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:flex-start"><div><h1>{'Tax invoice' if p.tax and p.currency == 'INR' else 'Invoice'}</h1>
<div class="m">{e(p.invoice_no)} · {p.paid_at:%d %b %Y}</div></div>
<div style="text-align:right"><b>{e(c['seller_name'])}</b><div class="m">{e(c['seller_address'])}</div>
{f'<div class="m">GSTIN: {e(c["seller_tax_id"])}</div>' if c['seller_tax_id'] else ''}</div></div>
<p><b>Billed to:</b> {e(p.billing_name or t.name)}{f'<br>GSTIN / Tax ID: {e(p.billing_tax_id)}' if p.billing_tax_id else ''}<br>
<span class="m">Organization: {e(t.name)} ({e(t.slug)})</span></p>
<table><tr><th style="text-align:left">Description</th><th style="text-align:right">Amount</th></tr>{body}
<tr class="tot"><td>Total paid ({e(p.currency)})</td><td style="text-align:right">{e(fmt(p.amount, p.currency))}</td></tr></table>
<p class="m">Paid via {e(p.gateway.title())} · reference {e(p.gateway_payment_id or p.gateway_order_id)}</p>
<p class="m">TestAgent by Unical Systems · {e(get_settings().public_url)}</p></body></html>"""
