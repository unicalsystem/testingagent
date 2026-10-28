"""Storage policy: what Unical permits, what each organization chose, and where a run's artifacts go.

Three layers, never conflated:
  1. Platform (Unical Super Admin): which providers are enabled and the default organization policy.
  2. Organization policy: `tenant_storage.policy_override` (set by the Super Admin) or the default:
       choice         the organization chooses Unical-managed storage or its own SharePoint/Drive
       unical_only    Unical-managed storage only
       external_only  the organization's own SharePoint/Drive only
  3. Organization choice: `tenant_storage.mode/provider`, made by the organization's administrators.

A platform change never rewrites an organization's choice. If a change would make a current choice
invalid, the Super Admin must acknowledge the affected organizations; their choice is then flagged
`needs_reselection` and their runs are refused until an administrator chooses again. Nothing falls
back to Unical storage silently.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .models import IdentityProvider, Project, Tenant, TenantStorage, utcnow
from .storage_providers import FACTORIES, GoogleDrive, Provider, StorageError, clean_name

POLICY_LABELS = {"choice": "Organization chooses", "unical_only": "Unical-managed storage only",
                 "external_only": "Customer-owned storage only"}
PROVIDER_LABELS = {"local": "Unical-managed storage", "sharepoint": "SharePoint", "gdrive": "Google Drive"}
FOLDER_DEFAULTS = {"base_folder": "TestAgent-AI", "project_folder": "name_id", "run_prefix": "run-",
                   "test_cases_folder": "test-cases", "evidence_folder": "evidence", "reports_folder": "reports"}


def platform(db: Session) -> dict[str, Any]:
    return settings_store.get(db, "storage")


def row(db: Session, tenant_id: int) -> TenantStorage:
    r = db.get(TenantStorage, tenant_id)
    if not r:
        from sqlalchemy.dialects.postgresql import insert

        # Concurrent first requests may both get here: insert once, never fail.
        db.execute(insert(TenantStorage).values(
            tenant_id=tenant_id, mode="", provider="", config={}, status="unconfigured", credentials_enc="",
            retention_days=int(platform(db)["default_retention_days"] or 0)).on_conflict_do_nothing(index_elements=["tenant_id"]))
        r = db.get(TenantStorage, tenant_id)
    return r


def policy_of(db: Session, r: TenantStorage, plat: dict | None = None) -> str:
    plat = plat or platform(db)
    return r.policy_override or plat["default_policy"]


def options(policy: str, plat: dict[str, Any]) -> list[tuple[str, str]]:
    """(mode, provider) pairs an organization may choose under a policy."""
    out: list[tuple[str, str]] = []
    if policy in ("choice", "unical_only") and plat["local_enabled"]:
        out.append(("unical", "local"))
    if policy in ("choice", "external_only"):
        out += [("external", p) for p in ("sharepoint", "gdrive") if plat[f"{p}_enabled"]]
    return out


def allowed(db: Session, r: TenantStorage, plat: dict | None = None) -> list[tuple[str, str]]:
    plat = plat or platform(db)
    return options(policy_of(db, r, plat), plat)


def choice_valid(r: TenantStorage, opts: list[tuple[str, str]]) -> bool:
    return (r.mode, r.provider) in opts


def run_block_reason(db: Session, tenant_id: int) -> str | None:
    """Why runs cannot start right now, or None. Checked when a run is queued and again when it starts."""
    r = row(db, tenant_id)
    if not r.mode:
        return ("Choose where this organization's test evidence is stored (Administration → Storage & data "
                "residency) before running tests")
    if r.needs_reselection:
        return ("Unical changed the storage policy for this organization; an administrator must choose the storage "
                "again (Administration → Storage & data residency). " + (r.reselection_reason or ""))
    if not choice_valid(r, allowed(db, r)):
        return "The selected storage is no longer permitted; an administrator must choose the storage again"
    if r.mode == "unical" and not r.unical_consent_at:
        return "An administrator must agree to Unical-managed storage before tests can run"
    if r.mode == "external":
        d = r.config.get("destination") or {}
        if not r.credentials_enc or not d.get("folder_id"):
            return f"The {PROVIDER_LABELS[r.provider]} connection is not finished (credentials and destination folder)"
    return None


# ---- credentials & providers --------------------------------------------------------------------
def credentials(db: Session, tenant_id: int, provider: str, enc: str) -> dict[str, Any]:
    if not enc:
        return {}
    c = json.loads(settings_store.unseal(enc))
    if provider == "sharepoint" and c.get("source") == "sso":
        idp = db.scalars(select(IdentityProvider).where(IdentityProvider.tenant_id == tenant_id,
                                                        IdentityProvider.provider == "microsoft")).first()
        if not idp or not idp.client_secret_enc:
            raise StorageError("The Microsoft 365 app registration from Identity & SSO is not configured")
        c = {"entra_tenant_id": idp.entra_tenant_id, "client_id": idp.client_id,
             "client_secret": settings_store.unseal(idp.client_secret_enc), "login_host": idp.cloud_host}
    return c


def build_provider(provider: str, creds: dict[str, Any], dest: dict[str, Any]) -> Provider:
    f = FACTORIES[provider]
    if provider == "sharepoint":
        return f(creds.get("entra_tenant_id", ""), creds.get("client_id", ""), creds.get("client_secret", ""), dest,
                 login_host=creds.get("login_host") or "login.microsoftonline.com")
    return f(creds.get("service_account") or {}, dest)


def provider_for(db: Session, r: TenantStorage) -> Provider:
    """The organization's active external store."""
    if r.mode != "external" or r.provider not in FACTORIES:
        raise StorageError("This organization does not use external storage")
    return build_provider(r.provider, credentials(db, r.tenant_id, r.provider, r.credentials_enc),
                          (r.config or {}).get("destination") or {})


def pending_provider(db: Session, r: TenantStorage) -> Provider:
    """The external store being set up (not used by runs until activated)."""
    p = (r.config or {}).get("pending") or {}
    if p.get("provider") not in FACTORIES or not p.get("credentials_enc"):
        raise StorageError("Enter and save the connection credentials first")
    return build_provider(p["provider"], credentials(db, r.tenant_id, p["provider"], p["credentials_enc"]),
                          p.get("destination") or {})


def seal_credentials(provider: str, body: dict[str, Any]) -> tuple[str, str]:
    """Validates and encrypts credentials from the configuration form. Returns (ciphertext, hint)."""
    if provider == "sharepoint":
        if body.get("source") == "sso":
            return settings_store.seal(json.dumps({"source": "sso"}))[0], "Identity & SSO app registration"
        tid, cid, sec = (str(body.get(k) or "").strip() for k in ("entra_tenant_id", "client_id", "client_secret"))
        if not (tid and cid and sec):
            raise StorageError("Directory (tenant) ID, application (client) ID and client secret are required")
        return (settings_store.seal(json.dumps({"source": "own", "entra_tenant_id": tid, "client_id": cid,
                                                "client_secret": sec}))[0], f"app {cid[:8]}…, secret …{sec[-4:]}")
    if provider == "gdrive":
        sa = GoogleDrive.parse_key(str(body.get("service_account_json") or ""))
        return settings_store.seal(json.dumps({"service_account": sa}))[0], sa["client_email"]
    raise StorageError("Unknown provider")


def check(db: Session, r: TenantStorage) -> tuple[bool, str]:
    """Live test of the ACTIVE store (writes, verifies and deletes a probe file). Records health on the row."""
    r.last_check_at = utcnow()
    try:
        msg = provider_for(db, r).check()
        r.status, r.last_ok_at, r.last_error = "ok", utcnow(), ""
        return True, msg
    except StorageError as e:
        r.status, r.last_error = "error", str(e)[:1000]
        return False, str(e)


# ---- folder layout --------------------------------------------------------------------------------
def folders(db: Session, r: TenantStorage) -> dict[str, str]:
    f = {**FOLDER_DEFAULTS, "base_folder": platform(db)["base_folder"] or "TestAgent-AI"}
    f.update({k: v for k, v in (r.config.get("folders") or {}).items() if k in FOLDER_DEFAULTS and v})
    return f


def project_folder(f: dict[str, str], project: Project) -> str:
    if f["project_folder"] == "id":
        return f"project-{project.id}"
    if f["project_folder"] == "name":
        return clean_name(project.name, f"project-{project.id}")
    return clean_name(f"{project.name} ({project.id})", f"project-{project.id}")


def run_parts(f: dict[str, str], project: Project, run_id: int) -> list[str]:
    """[base, project, run] — TestAgent-AI/{project}/{execution-id}/"""
    return [clean_name(f["base_folder"], "TestAgent-AI"), project_folder(f, project), clean_name(f"{f['run_prefix']}{run_id}", f"run-{run_id}")]


def sub_for_kind(f: dict[str, str], kind: str) -> str:
    if kind in ("report_html", "report_json", "ai_analysis"):
        return clean_name(f["reports_folder"], "reports")
    if kind == "test_case":
        return clean_name(f["test_cases_folder"], "test-cases")
    return clean_name(f["evidence_folder"], "evidence")


# ---- platform policy changes ----------------------------------------------------------------------
def affected(db: Session, plat: dict[str, Any], overrides: dict[int, str | None] | None = None) -> list[dict]:
    """Organizations whose current storage choice would be invalid under `plat` (+ per-tenant overrides)."""
    out = []
    for r, t in db.execute(select(TenantStorage, Tenant).join(Tenant, Tenant.id == TenantStorage.tenant_id)).all():
        if not r.mode:
            continue
        pol = overrides[r.tenant_id] if overrides and r.tenant_id in overrides else r.policy_override
        pol = pol or plat["default_policy"]
        if (r.mode, r.provider) not in options(pol, plat):
            out.append({"tenant_id": t.id, "slug": t.slug, "name": t.name,
                        "current": PROVIDER_LABELS.get(r.provider, r.provider), "policy": POLICY_LABELS[pol]})
    return out


def flag_reselection(db: Session, tenant_ids: list[int], reason: str) -> None:
    for tid in tenant_ids:
        r = row(db, tid)
        r.needs_reselection, r.reselection_reason = True, reason[:500]


def view(db: Session, r: TenantStorage) -> dict[str, Any]:
    plat = platform(db)
    pol = policy_of(db, r, plat)
    opts = options(pol, plat)
    cfg = r.config or {}
    return {
        "policy": pol, "policy_label": POLICY_LABELS[pol], "policy_override": r.policy_override,
        "options": [{"mode": m, "provider": p, "label": PROVIDER_LABELS[p]} for m, p in opts],
        "mode": r.mode, "provider": r.provider,
        "unical_consent_at": r.unical_consent_at, "unical_consent_source": r.unical_consent_source,
        "destination": cfg.get("destination") or {}, "folders": folders(db, r),
        # an external setup in progress (not used by runs until activated); never includes the secret
        "pending": ({k: v for k, v in cfg["pending"].items() if k != "credentials_enc"} if cfg.get("pending") else None),
        "credentials_set": bool(r.credentials_enc), "credentials_hint": r.credentials_hint,
        "status": r.status, "last_check_at": r.last_check_at, "last_ok_at": r.last_ok_at, "last_error": r.last_error,
        "needs_reselection": r.needs_reselection, "reselection_reason": r.reselection_reason,
        "allow_delete": r.allow_delete, "retention_days": r.retention_days, "ai_enabled": r.ai_enabled,
        "run_block_reason": run_block_reason(db, r.tenant_id),
        "max_artifact_mb": plat["max_artifact_mb"], "spool_max_hours": plat["spool_max_hours"],
    }


def unusable_policies(plat: dict[str, Any], policies: set[str]) -> list[str]:
    """Policies (from the default and the overrides in use) that would leave nothing to choose."""
    return [POLICY_LABELS[p] for p in sorted(policies) if not options(p, plat)]
