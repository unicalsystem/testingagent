"""Microsoft Entra ID sign-in (OpenID Connect, authorization code flow + PKCE, confidential client).

Each organization has its OWN configuration (identity_providers row). A sign-in is bound end to end:
  start:    the organization is resolved from the URL / request body (never from a user-supplied Entra
            tenant id); a server-side transaction stores state hash, nonce, PKCE verifier, organization,
            provider row and purpose; a random browser-binding cookie is set and its hash stored.
  callback: the transaction is looked up by state, must be unused, unexpired and carry the same browser
            binding; it is consumed once. The code is redeemed with the PKCE verifier and the client
            secret of THAT organization's configuration. The ID token is verified: RS256 signature
            against the provider's JWKS, issuer == the configured Entra tenant's issuer, tid == configured
            tenant id, audience == client id, nonce, exp/nbf/iat (60 s leeway), and a stable subject (oid).
  identity: users are matched by (organization, issuer, oid) only — never by e-mail. Linking happens
            explicitly (signed-in user links, an invitation is redeemed, or the organization enabled
            domain-based provisioning). Access to projects still comes only from assignments.
Only the "openid profile email" scopes are requested; no Microsoft Graph permissions are needed.
The connection test uses the client-credentials grant (proves tenant, client id and secret) without
any API permission.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWK
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .config import get_settings
from .models import IdentityProvider, OidcTransaction

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HOST_RE = re.compile(r"^login\.(microsoftonline\.com|microsoftonline\.us|partner\.microsoftonline\.cn|chinacloudapi\.cn)$")
TX_TTL = timedelta(minutes=10)
BINDING_COOKIE = "tp_oidc"
_cache: dict[str, tuple[float, Any]] = {}


class OidcError(Exception):
    """`code` is a short, user-safe reason shown on the sign-in page; `detail` goes to logs/audit only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def redirect_uri() -> str:
    return f"{get_settings().public_url}/api/public/oidc/microsoft/callback"


# ---- HTTP (module-level functions so tests can substitute a fake Microsoft) --------------------
def http_get_json(url: str) -> dict:
    r = httpx.get(url, timeout=10.0)
    r.raise_for_status()
    return r.json()


def http_post_form(url: str, data: dict) -> tuple[int, dict]:
    r = httpx.post(url, data=data, timeout=15.0)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": "invalid_response"}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = fn()
    _cache[key] = (time.time() + ttl, val)
    return val


def validate_config(entra_tenant_id: str, client_id: str, cloud_host: str) -> None:
    if not GUID_RE.match(entra_tenant_id or ""):
        raise OidcError("config", "Directory (tenant) ID must be the GUID of a single Microsoft Entra tenant")
    if not GUID_RE.match(client_id or ""):
        raise OidcError("config", "Application (client) ID must be a GUID")
    if not HOST_RE.match(cloud_host or ""):
        raise OidcError("config", "Unsupported Microsoft cloud host")


def discovery(idp: IdentityProvider) -> dict:
    validate_config(idp.entra_tenant_id, idp.client_id, idp.cloud_host)
    tid = idp.entra_tenant_id.lower()
    url = f"https://{idp.cloud_host}/{tid}/v2.0/.well-known/openid-configuration"

    def load():
        try:
            d = http_get_json(url)
        except Exception as e:  # network / HTTP error
            raise OidcError("provider_unreachable", f"discovery failed: {type(e).__name__}")
        base = f"https://{idp.cloud_host}/"
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
            if not str(d.get(k, "")).startswith(base):
                raise OidcError("provider_config", f"unexpected {k} in discovery document")
        if d["issuer"].rstrip("/") != f"https://{idp.cloud_host}/{tid}/v2.0":
            raise OidcError("provider_config", "discovery issuer does not match the configured tenant")
        return d

    return _cached("disc:" + url, 3600, load)


def _jwks(uri: str, force: bool = False) -> dict:
    if force:
        _cache.pop("jwks:" + uri, None)
    return _cached("jwks:" + uri, 3600, lambda: http_get_json(uri))


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


# ---- flow ------------------------------------------------------------------------------------
def start(db: Session, idp: IdentityProvider, purpose: str, *, user_id: int | None = None,
          invitation_id: int | None = None, login_hint: str = "") -> tuple[str, str]:
    """Returns (authorization URL, browser-binding cookie value)."""
    if purpose not in ("login", "link", "invite"):
        raise ValueError(purpose)
    d = discovery(idp)
    state, nonce, verifier, binding = (secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                                       secrets.token_urlsafe(64), secrets.token_urlsafe(32))
    now = datetime.now(timezone.utc)
    db.add(OidcTransaction(state_hash=_h(state), tenant_id=idp.tenant_id, provider_id=idp.id, purpose=purpose,
                           nonce=nonce, code_verifier=verifier, user_id=user_id, invitation_id=invitation_id,
                           browser_binding=_h(binding), expires_at=now + TX_TTL))
    params = {
        "client_id": idp.client_id, "response_type": "code", "redirect_uri": redirect_uri(),
        "response_mode": "query", "scope": "openid profile email", "state": state, "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return d["authorization_endpoint"] + "?" + urlencode(params), binding


def consume(db: Session, state: str, binding: str | None) -> OidcTransaction:
    """Look up and atomically consume the transaction for this state (single use)."""
    from sqlalchemy import update

    if not state or len(state) > 200:
        raise OidcError("invalid_state")
    now = datetime.now(timezone.utc)
    tx = db.scalars(select(OidcTransaction).where(OidcTransaction.state_hash == _h(state))).first()
    if not tx:
        raise OidcError("invalid_state", "unknown state")
    if not binding or _h(binding) != tx.browser_binding:
        raise OidcError("invalid_state", "browser binding mismatch")
    claimed = db.execute(update(OidcTransaction).where(OidcTransaction.id == tx.id, OidcTransaction.used_at.is_(None),
                                                       OidcTransaction.expires_at > now).values(used_at=now)).rowcount
    if claimed != 1:
        raise OidcError("expired", "transaction used or expired")
    return tx


def redeem(db: Session, tx: OidcTransaction, code: str) -> dict:
    """Exchange the code with THIS transaction's provider configuration; return verified ID-token claims."""
    idp = db.get(IdentityProvider, tx.provider_id)
    if not idp or idp.tenant_id != tx.tenant_id or idp.status != "active" or not idp.client_secret_enc:
        raise OidcError("not_configured", "provider removed or not active")
    d = discovery(idp)
    status, tok = http_post_form(d["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": idp.client_id, "client_secret": settings_store.unseal(idp.client_secret_enc),
        "code_verifier": tx.code_verifier, "scope": "openid profile email",
    })
    if status != 200 or "id_token" not in tok:
        raise OidcError("token_exchange", str(tok.get("error_description") or tok.get("error") or status)[:300])
    return verify_id_token(idp, d, tok["id_token"], tx.nonce)


def verify_id_token(idp: IdentityProvider, d: dict, id_token: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise OidcError("invalid_token", f"header: {e}")
    if header.get("alg") != "RS256":
        raise OidcError("invalid_token", "unexpected algorithm")
    kid = header.get("kid")
    keys = _jwks(d["jwks_uri"]).get("keys", [])
    if not any(k.get("kid") == kid for k in keys):
        keys = _jwks(d["jwks_uri"], force=True).get("keys", [])  # key rotation
    jwk = next((k for k in keys if k.get("kid") == kid), None)
    if not jwk:
        raise OidcError("invalid_token", "signing key not found")
    tid = idp.entra_tenant_id.lower()
    issuer = d["issuer"].replace("{tenantid}", tid)
    try:
        claims = jwt.decode(id_token, PyJWK(jwk, algorithm="RS256").key, algorithms=["RS256"], audience=idp.client_id,
                            issuer=issuer, leeway=60, options={"require": ["exp", "iat", "aud", "iss", "sub"]})
    except jwt.PyJWTError as e:
        raise OidcError("invalid_token", type(e).__name__)
    if not nonce or claims.get("nonce") != nonce:
        raise OidcError("invalid_token", "nonce mismatch")
    if str(claims.get("tid", "")).lower() != tid:
        raise OidcError("invalid_token", "tenant mismatch")
    if not claims.get("oid"):
        raise OidcError("invalid_token", "no oid claim")
    return claims


def emails_of(claims: dict) -> set[str]:
    """Addresses we trust for this identity: the UPN (preferred_username, issued by the organization's
    directory) and the `email` claim only if Microsoft marks the domain verified (xms_edov) or it equals
    the UPN. The `email` claim alone is user-editable for some account types."""
    upn = str(claims.get("preferred_username") or "").strip().lower()
    mail = str(claims.get("email") or "").strip().lower()
    out = {upn} if "@" in upn else set()
    if "@" in mail and (claims.get("xms_edov") in (True, 1, "1", "true") or mail == upn):
        out.add(mail)
    return out


def email_of(claims: dict) -> str:
    """Primary trusted address (UPN first)."""
    e = emails_of(claims)
    upn = str(claims.get("preferred_username") or "").strip().lower()
    return upn if upn in e else (sorted(e)[0] if e else "")


def check_restrictions(idp: IdentityProvider, claims: dict) -> None:
    email = email_of(claims)
    if idp.allowed_domains:
        dom = email.rsplit("@", 1)[-1] if "@" in email else ""
        if dom not in {x.lower() for x in idp.allowed_domains}:
            raise OidcError("domain_not_allowed", dom)
    if idp.allowed_group_ids:
        if "_claim_names" in claims and "groups" in (claims.get("_claim_names") or {}):
            raise OidcError("groups_overage", "token has too many groups; use fewer or app-assigned groups")
        groups = {str(g).lower() for g in claims.get("groups", []) or []}
        if not groups & {g.lower() for g in idp.allowed_group_ids}:
            raise OidcError("group_not_allowed")


def test_connection(idp: IdentityProvider, secret: str) -> tuple[bool, str]:
    """Discovery + client-credentials token request: proves tenant id, client id and secret."""
    try:
        _cache.clear()
        d = discovery(idp)
    except OidcError as e:
        return False, e.detail or e.code
    status, tok = http_post_form(d["token_endpoint"], {
        "grant_type": "client_credentials", "client_id": idp.client_id, "client_secret": secret,
        "scope": f"https://graph.{'microsoft.com' if idp.cloud_host.endswith('.com') else 'microsoft.us'}/.default",
    })
    if status == 200 and tok.get("access_token"):
        return True, "Connected: tenant, client ID and secret accepted by Microsoft"
    msg = str(tok.get("error_description") or tok.get("error") or f"HTTP {status}").split("\r\n")[0]
    return False, msg[:300]
