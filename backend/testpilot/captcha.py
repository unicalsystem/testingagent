"""Google reCAPTCHA v3, verified server-side.

A protected action passes when ANY of these holds:
  - reCAPTCHA is disabled, or the action is not in the protected list;
  - the client IP is in the platform trusted list (or, for organization sign-in, that organization's list);
  - Google confirms the token: success, the expected action, our hostname, score >= minimum,
    and the token has not been seen before (replay).
If Google cannot be reached, the configured `unavailable_policy` decides: "deny" (default) or
"allow_and_audit" (allowed, and an audit event is recorded). Nothing is bypassed silently.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import settings_store
from .config import get_settings
from .models import CaptchaTokenSeen, Tenant

log = logging.getLogger("testpilot.captcha")
VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


class CaptchaUnavailable(Exception):
    pass


def _post(data: dict) -> dict:
    """Call Google's verification endpoint. Separate function so tests can substitute it."""
    try:
        r = httpx.post(VERIFY_URL, data=data, timeout=8.0)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise CaptchaUnavailable(type(e).__name__) from e


def client_config(db: Session, action: str, ip: str, tenant: Tenant | None = None) -> dict:
    cfg = settings_store.get(db, "recaptcha")
    required = bool(cfg["enabled"] and cfg["site_key"] and cfg["secret_enc"] and action in cfg["actions"]
                    and not settings_store.ip_trusted(db, ip, tenant))
    return {"required": required, "site_key": cfg["site_key"] if required else "", "action": action}


def verify(db: Session, request: Request, action: str, token: str | None, tenant: Tenant | None = None,
           audit=None) -> None:
    """Raise HTTPException(400) unless the request passes CAPTCHA for `action`."""
    from .api.deps import client_ip

    cfg = settings_store.get(db, "recaptcha")
    if not cfg["enabled"] or action not in cfg["actions"] or not cfg["secret_enc"]:
        return
    ip = client_ip(request)
    if settings_store.ip_trusted(db, ip, tenant):
        return
    if not token or len(token) > 4000:
        raise HTTPException(400, "CAPTCHA verification is required. Reload the page and try again.")
    th = hashlib.sha256(token.encode()).hexdigest()
    try:
        res = _post({"secret": settings_store.unseal(cfg["secret_enc"]), "response": token, "remoteip": ip})
    except CaptchaUnavailable as e:
        log.error("reCAPTCHA verification unavailable: %s", e)
        if cfg["unavailable_policy"] == "allow_and_audit":
            if audit:
                audit("captcha_unavailable_allowed", action, error=str(e))
            return
        raise HTTPException(503, "CAPTCHA verification is temporarily unavailable. Try again shortly.")
    expected_host = urlsplit(get_settings().public_url).hostname
    problems = []
    if not res.get("success"):
        problems.append("rejected:" + ",".join(res.get("error-codes", []) or ["unknown"]))
    if res.get("action") != action:
        problems.append("action")
    if (res.get("hostname") or "") != expected_host:
        problems.append("hostname")
    try:
        if float(res.get("score", 0.0)) < float(cfg["min_score"]):
            problems.append("score")
    except (TypeError, ValueError):
        problems.append("score")
    if problems:
        log.info("reCAPTCHA rejected for %s: %s", action, problems)
        raise HTTPException(400, "CAPTCHA check failed. Reload the page and try again.")
    # replay protection (Google also rejects duplicates; this keeps it explicit and local)
    try:
        with db.begin_nested():
            db.add(CaptchaTokenSeen(token_hash=th))
    except IntegrityError:
        raise HTTPException(400, "CAPTCHA token already used. Reload the page and try again.")
    db.execute(delete(CaptchaTokenSeen).where(CaptchaTokenSeen.seen_at < datetime.now(timezone.utc) - timedelta(hours=1)))


def test_secret(secret: str) -> tuple[bool, str]:
    """Checks that Google accepts the secret key (a dummy token must fail with invalid-input-response,
    not invalid-input-secret)."""
    try:
        res = _post({"secret": secret, "response": "testpilot-configuration-check"})
    except CaptchaUnavailable as e:
        return False, f"Google could not be reached ({e})"
    codes = res.get("error-codes", []) or []
    if "invalid-input-secret" in codes or "missing-input-secret" in codes:
        return False, "Google rejected the secret key"
    return True, "Secret key accepted by Google"
