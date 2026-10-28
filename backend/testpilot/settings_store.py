"""Platform settings persisted in PostgreSQL (configurable from the UI, no restart needed).

Documents (platform_settings.key):
  auth        password/Microsoft policy for the platform (Unical) organization, lockout, sessions, reset,
              which sign-in methods customer organizations may use
  recaptcha   Google reCAPTCHA v3 configuration (secret encrypted)
  trusted_ips CIDRs that skip CAPTCHA on every protected page
  smtp        outgoing e-mail (password encrypted)
  storage     storage infrastructure: enabled providers, default organization policy, limits (release 1.3)
  legal       legal entity, grievance officer / DPO contacts, policy versions (release 1.3)
  privacy     retention periods for personal data and response deadlines (release 1.3)
  billing     plans enforcement, self-service sign-up, Razorpay / PayPal, seller and tax details (release 1.9)

Secrets are stored Fernet-encrypted with CREDENTIAL_ENCRYPTION_KEY under keys ending in `_enc` and are
never returned by any API: `public_view()` strips them and reports only whether one is set.
"""
from __future__ import annotations

import copy
import ipaddress
from typing import Any

from sqlalchemy.orm import Session

from .config import get_settings
from .models import PlatformSetting, Tenant, TenantAuthSettings
from .security import decrypt_secret, encrypt_secret

CAPTCHA_ACTIONS = ("login", "register", "password_reset")

DEFAULTS: dict[str, dict[str, Any]] = {
    "auth": {
        "platform_password_enabled": True,      # Unical organization sign-in methods
        "platform_microsoft_enabled": False,
        "tenant_password_allowed": True,        # what customer organizations may enable
        "tenant_microsoft_allowed": True,
        "login_identifier": "username_or_email",  # username | username_or_email
        "max_failed_logins": 5,
        "lockout_minutes": 15,
        "session_ttl_hours": None,              # None = use the .env value
        "session_idle_minutes": None,
        "password_reset_enabled": False,        # needs SMTP
        "password_reset_ttl_minutes": 30,
    },
    "recaptcha": {
        "enabled": False,
        "site_key": "",
        "secret_enc": "",
        "secret_hint": "",
        "min_score": 0.5,
        "actions": ["login", "register", "password_reset"],
        # What to do when Google cannot be reached: "deny" (default, fail closed) or "allow_and_audit".
        "unavailable_policy": "deny",
        "last_test_at": None,
        "last_test_ok": None,
        "last_test_message": "",
    },
    "trusted_ips": {"cidrs": [], "note": ""},
    "smtp": {
        "enabled": False,
        "host": "",
        "port": 587,
        "security": "starttls",  # starttls | ssl | none
        "username": "",
        "password_enc": "",
        "password_hint": "",
        "from_address": "",
        "from_name": "TestAgent",
        "last_test_at": None,
        "last_test_ok": None,
        "last_test_message": "",
    },
    "storage": {
        "local_enabled": True,          # Unical-managed storage (this server's evidence volume)
        "sharepoint_enabled": True,     # customer-configured SharePoint
        "gdrive_enabled": True,         # customer-configured Google Drive (Shared Drives)
        "default_policy": "choice",     # choice | unical_only | external_only (organizations without an override)
        "default_retention_days": 0,    # suggested to new organizations; 0 = keep
        "max_artifact_mb": 200,         # larger artifacts are not stored (logged in the run log)
        "spool_max_hours": 24,          # how long artifacts wait on this server for an unavailable external store
        "base_folder": "TestAgent-AI",
        "local_last_check_at": None,
        "local_last_ok_at": None,
        "local_last_error": "",
    },
    "legal": {
        "entity_name": "Unical Systems",
        "address": "DSL Abacus IT Park, IDA Uppal, Hyderabad, Telangana, India",
        "contact_email": "",
        "grievance_officer_name": "",
        "grievance_officer_email": "",
        "grievance_officer_phone": "",
        "dpo_email": "",
        "uk_representative": "",
        "us_contact": "",
        "hosting_location": "India",
        "terms_version": "2026-10-26",
        "privacy_version": "2026-10-26",
    },
    "billing": {
        "enforce": True,                 # plans and limits apply to organizations (Unical itself is exempt)
        "self_signup": True,             # registration creates the organization at once (no Super Admin approval)
        "razorpay_enabled": False,
        "razorpay_key_id": "",
        "razorpay_key_secret_enc": "",
        "razorpay_key_secret_hint": "",
        "razorpay_webhook_secret_enc": "",
        "razorpay_webhook_secret_hint": "",
        "paypal_enabled": False,
        "paypal_mode": "sandbox",        # sandbox | live
        "paypal_client_id": "",
        "paypal_client_secret_enc": "",
        "paypal_client_secret_hint": "",
        "seller_name": "Unical Systems Private Limited",
        "seller_address": "DSL Abacus IT Park, IDA Uppal, Hyderabad, Telangana, India",
        "seller_tax_id": "",             # GSTIN
        "seller_email": "",
        "tax_label": "GST",
        "tax_percent_inr": 18,           # added on top of INR prices
        "tax_percent_usd": 0,
        "invoice_prefix": "TA",
        "invoice_counter": 0,
        "reminder_days": [7, 1],
        "last_test": {},
    },
    "privacy": {
        "request_response_days": 30,       # strictest common deadline (UK GDPR: one month)
        "audit_retention_days": 730,
        "registration_retention_days": 365,  # rejected registrations
        "session_retention_days": 30,
    },
}


def get(db: Session, key: str) -> dict[str, Any]:
    row = db.get(PlatformSetting, key)
    out = copy.deepcopy(DEFAULTS[key])
    if row and isinstance(row.value, dict):
        out.update({k: v for k, v in row.value.items() if k in out})
    return out


def put(db: Session, key: str, value: dict[str, Any], user_id: int | None) -> dict[str, Any]:
    clean = {k: v for k, v in value.items() if k in DEFAULTS[key]}
    row = db.get(PlatformSetting, key)
    if row:
        merged = {**get(db, key), **clean}
        row.value, row.updated_by = merged, user_id
    else:
        merged = {**copy.deepcopy(DEFAULTS[key]), **clean}
        db.add(PlatformSetting(key=key, value=merged, updated_by=user_id))
    db.flush()
    return merged


def public_view(doc: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in doc.items():
        if k.endswith("_enc"):
            out[k[:-4] + "_set"] = bool(v)
        else:
            out[k] = v
    return out


def seal(value: str) -> tuple[str, str]:
    """Encrypt a secret; returns (ciphertext, hint)."""
    return encrypt_secret(value), ("…" + value[-4:] if len(value) >= 8 else "…")


def unseal(ciphertext: str) -> str:
    return decrypt_secret(ciphertext) if ciphertext else ""


# ---- auth helpers ---------------------------------------------------------------------------
def auth(db: Session) -> dict[str, Any]:
    a = get(db, "auth")
    s = get_settings()
    a["session_ttl_hours"] = a["session_ttl_hours"] or s.session_ttl_hours
    a["session_idle_minutes"] = a["session_idle_minutes"] or s.session_idle_minutes
    return a


def tenant_auth(db: Session, tenant: Tenant) -> TenantAuthSettings:
    t = db.get(TenantAuthSettings, tenant.id)
    if not t:
        t = TenantAuthSettings(tenant_id=tenant.id, password_enabled=True, microsoft_enabled=False, trusted_ips=[])
        db.add(t)
        db.flush()
    return t


def is_platform_tenant(tenant: Tenant) -> bool:
    return tenant.slug == get_settings().default_tenant_slug


def methods(db: Session, tenant: Tenant) -> dict[str, bool]:
    """Effective sign-in methods for an organization (platform policy ∧ organization setting)."""
    a = auth(db)
    if is_platform_tenant(tenant):
        return {"password": bool(a["platform_password_enabled"]), "microsoft": bool(a["platform_microsoft_enabled"])}
    t = tenant_auth(db, tenant)
    return {"password": bool(a["tenant_password_allowed"] and t.password_enabled),
            "microsoft": bool(a["tenant_microsoft_allowed"] and t.microsoft_enabled)}


# ---- trusted IPs ----------------------------------------------------------------------------
def parse_cidrs(values: list[str]) -> list[str]:
    out = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        try:
            net = ipaddress.ip_network(v, strict=False)
        except ValueError:
            raise ValueError(f"'{v}' is not a valid IP address or CIDR range")
        if net.prefixlen < (8 if net.version == 4 else 32):
            raise ValueError(f"'{v}' is too broad (at most /8 for IPv4, /32 for IPv6); "
                             "to turn the CAPTCHA off for everyone use its On/Off switch")
        out.append(str(net))
    if len(out) > 200:
        raise ValueError("at most 200 entries")
    return sorted(set(out))


def ip_in(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def ip_trusted(db: Session, ip: str, tenant: Tenant | None = None) -> bool:
    if ip_in(ip, get(db, "trusted_ips")["cidrs"]):
        return True
    if tenant is not None:
        return ip_in(ip, tenant_auth(db, tenant).trusted_ips or [])
    return False
