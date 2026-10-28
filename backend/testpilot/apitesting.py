"""API testing service (release 1.6): credentials, target policy, case generation, run queue and the
worker side of API runs and OpenAPI imports.

Security model (same as browser runs):
* every host contacted (API base URL, OAuth2 token URL, OpenAPI document URL) must be on the
  organization's authorised target allowlist; private addresses only for operator-listed organizations;
* all traffic leaves through a per-run EgressProxy that vets and pins every connection and redirect;
* credentials are Fernet-encrypted at rest, never returned by the API, masked in results and logs;
* write requests (POST/PUT/PATCH/DELETE) are only sent when the suite allows them AND the run asks for them.
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from . import access, apiexec, apireport, apispec, storage_policy, targets
from .config import get_settings
from .db import session_scope
from .egress_proxy import EgressProxy
from .logging_setup import register_secret
from .models import (ApiCase, ApiRun, ApiRunResult, ApiSuite, Project, Tenant, User, utcnow)
from .security import SecretsUnavailable, decrypt_secret, encrypt_secret
from .urlguard import Policy, TargetRejected, normalize_host, parse_target

log = logging.getLogger("testpilot.apitest")
CLAIM_LOCK_KEY = 74_220_016
TERMINAL = ("passed", "failed", "error", "cancelled")
SECRET_FIELDS = {"api_key": ("api_key",), "bearer": ("token",), "basic": ("password",), "oauth2": ("client_secret",), "none": ()}
CONFIG_FIELDS = {"api_key": ("name", "location"), "bearer": (), "basic": ("username",),
                 "oauth2": ("token_url", "client_id", "scope", "audience", "client_auth"), "none": ()}
MAX_SPEC_DOWNLOAD = apispec.MAX_SPEC_BYTES


class ApiTestError(ValueError):
    pass


# --------------------------------------------------------------------------- credentials
def set_credentials(suite: ApiSuite, auth_type: str, config: dict, secrets: dict, *, keep_existing: bool = True) -> None:
    """Stores non-secret settings in auth_config and secret values encrypted. Empty secret fields keep the
    stored value when the type is unchanged (so the form never needs to show a secret again)."""
    if auth_type not in SECRET_FIELDS:
        raise ApiTestError(f"unknown authentication type '{auth_type}'")
    cfg = {k: str(config.get(k, "")).strip()[:500] for k in CONFIG_FIELDS[auth_type]}
    if auth_type == "api_key":
        cfg["location"] = "query" if cfg.get("location") == "query" else "header"
        cfg["name"] = cfg.get("name") or "X-API-Key"
        if not all(c.isalnum() or c in "-_." for c in cfg["name"]):
            raise ApiTestError("the API key name may only contain letters, digits, - _ .")
    if auth_type == "oauth2":
        if not cfg.get("token_url"):
            raise ApiTestError("the OAuth2 token URL is required")
        try:
            parse_target(cfg["token_url"])
        except TargetRejected as e:
            raise ApiTestError(f"token URL: {e}")
        if not cfg.get("client_id"):
            raise ApiTestError("the OAuth2 client ID is required")
        cfg["client_auth"] = "basic" if cfg.get("client_auth") == "basic" else "body"
    if auth_type == "basic" and not cfg.get("username"):
        raise ApiTestError("the username is required for basic authentication")
    old = load_secrets(suite) if keep_existing and suite.auth_type == auth_type and suite.auth_secret_enc else {}
    vals = {}
    for k in SECRET_FIELDS[auth_type]:
        v = str(secrets.get(k) or "")
        if len(v) > 8000:
            raise ApiTestError(f"{k} is too long")
        vals[k] = v or old.get(k, "")
        if not vals[k]:
            raise ApiTestError(f"{k.replace('_', ' ')} is required for this authentication type")
    try:
        suite.auth_secret_enc = encrypt_secret(json.dumps(vals)) if vals else ""
    except SecretsUnavailable as e:
        raise ApiTestError(str(e))
    suite.auth_type, suite.auth_config = auth_type, cfg
    first = next(iter(vals.values()), "")
    labels = {"none": "No authentication", "api_key": f"API key in {cfg.get('location', 'header')} {cfg.get('name', '')}",
              "bearer": "Bearer token", "basic": f"Basic auth as {cfg.get('username', '')}",
              "oauth2": f"OAuth2 client credentials ({cfg.get('client_id', '')[:40]})"}
    suite.auth_hint = (labels[auth_type] + (f" · …{first[-4:]}" if len(first) >= 12 else ""))[:120]


def load_secrets(suite: ApiSuite) -> dict:
    if not suite.auth_secret_enc:
        return {}
    return json.loads(decrypt_secret(suite.auth_secret_enc))


# --------------------------------------------------------------------------- target policy
def suite_hosts(suite: ApiSuite, *, include_spec: bool = False) -> tuple[set[str], set[int]]:
    urls = [suite.base_url] if suite.base_url else []
    if suite.auth_type == "oauth2" and (suite.auth_config or {}).get("token_url"):
        urls.append(suite.auth_config["token_url"])
    if include_spec and suite.spec_source == "url" and suite.spec_url:
        urls.append(suite.spec_url)
    hosts, ports = set(), {80, 443}
    for u in urls:
        _, h, p = parse_target(u)
        hosts.add(h)
        ports.add(p)
    return hosts, ports


def policy_for_hosts(db: Session, tenant_id: int, hosts: set[str], ports: set[int]) -> targets.ProjectPolicy:
    approved = targets.allowlist(db, tenant_id)
    missing = sorted(hosts - approved)
    problems = ["not on the authorised target allowlist: " + ", ".join(missing)] if missing else []
    ok = frozenset(hosts & approved)
    private_ok = frozenset(ok & get_settings().private_targets) if tenant_id in targets.private_tenant_ids(db) else frozenset()
    return targets.ProjectPolicy(Policy(ok, private_ok), ports, problems)


def suite_policy(db: Session, suite: ApiSuite, *, include_spec: bool = False) -> targets.ProjectPolicy:
    try:
        hosts, ports = suite_hosts(suite, include_spec=include_spec)
    except TargetRejected as e:
        return targets.ProjectPolicy(Policy(frozenset(), frozenset()), set(), [str(e)])
    return policy_for_hosts(db, suite.tenant_id, hosts, ports)


@contextlib.contextmanager
def guarded_client(policy: targets.ProjectPolicy, *, verify_tls: bool = True, on_block=None) -> Iterator[httpx.Client]:
    """httpx client whose every connection goes through a fresh EgressProxy for this policy."""
    proxy = EgressProxy(policy.policy, policy.ports, on_block=on_block)
    port = proxy.start()
    try:
        with httpx.Client(proxy=f"http://127.0.0.1:{port}", verify=verify_tls, follow_redirects=True, max_redirects=5,
                          trust_env=False, timeout=15.0) as client:
            yield client
    finally:
        proxy.stop()


# --------------------------------------------------------------------------- generation
def generate(db: Session, suite: ApiSuite, doc: dict | None = None) -> int:
    """Replaces the generated (spec/auto) cases of a suite. Cases written by hand or drafted by AI are kept."""
    has_auth = suite.auth_type != "none"
    if suite.spec_source in ("url", "upload"):
        if doc is None:
            doc = apispec.parse_spec_text(suite.spec_text)
        cases, source = apispec.generate_from_spec(doc, has_auth=has_auth, max_ms=suite.max_response_ms), "spec"
        suite.spec_title = apispec.spec_title(doc)[:300]
    else:
        eps = apispec.parse_endpoints(suite.endpoints_text)
        cases, source = apispec.generate_from_endpoints(eps, has_auth=has_auth, requires_auth=suite.requires_auth,
                                                        max_ms=suite.max_response_ms), "auto"
    db.execute(delete(ApiCase).where(ApiCase.tenant_id == suite.tenant_id, ApiCase.suite_id == suite.id,
                                     ApiCase.source.in_(("spec", "auto"))))
    start = (db.scalar(select(func.max(ApiCase.position)).where(ApiCase.suite_id == suite.id)) or 0) + 1
    for i, c in enumerate(cases):
        d = c["definition"]
        db.add(ApiCase(tenant_id=suite.tenant_id, suite_id=suite.id, position=start + i, name=c["name"],
                       category=c["category"], method=d["method"], path=d["path"][:2048], definition=d,
                       source=source, is_write=c["is_write"]))
    return len(cases)


def case_out(c: ApiCase) -> dict:
    return {"id": c.id, "suite_id": c.suite_id, "position": c.position, "name": c.name, "category": c.category,
            "method": c.method, "path": c.path, "definition": c.definition, "source": c.source,
            "is_write": c.is_write, "enabled": c.enabled, "updated_at": c.updated_at}


# --------------------------------------------------------------------------- queue
def run_block_reason(db: Session, suite: ApiSuite) -> str | None:
    pol = suite_policy(db, suite)
    if pol.problems:
        return "Target policy: " + "; ".join(pol.problems) + ". An administrator must authorise the host."
    block = storage_policy.run_block_reason(db, suite.tenant_id)
    if block:
        return block
    if not db.scalar(select(func.count()).select_from(ApiCase).where(ApiCase.suite_id == suite.id, ApiCase.enabled.is_(True))):
        return "The suite has no enabled test cases. Generate them first."
    if suite.auth_type != "none" and not suite.auth_secret_enc:
        return "Credentials are missing. Add them under Credentials."
    return None


def queue_run(db: Session, suite: ApiSuite, user: User | None, *, include_writes: bool = False) -> ApiRun:
    reason = run_block_reason(db, suite)
    if reason:
        raise ApiTestError(reason)
    if db.scalar(select(func.count()).select_from(ApiRun).where(ApiRun.suite_id == suite.id,
                                                               ApiRun.status.in_(("queued", "running")))):
        raise ApiTestError("A run of this suite is already queued or running")
    run = ApiRun(tenant_id=suite.tenant_id, project_id=suite.project_id, suite_id=suite.id,
                 requested_by=user.id if user else None,
                 options={"include_writes": bool(include_writes and suite.allow_writes)},
                 suite_snapshot={"name": suite.name, "base_url": suite.base_url, "auth_type": suite.auth_type,
                                 "spec_title": suite.spec_title, "spec_source": suite.spec_source})
    db.add(run)
    db.flush()
    return run


# --------------------------------------------------------------------------- reports
def report_data(db: Session, run: ApiRun) -> dict:
    project = db.get(Project, run.project_id)
    tenant = db.get(Tenant, run.tenant_id)
    who = db.get(User, run.requested_by) if run.requested_by else None
    results = [{"idx": r.idx, "case_id": r.case_id, "name": r.name, "category": r.category, "method": r.method,
                "path": r.path, "url": r.url, "status": r.status, "http_status": r.http_status,
                "duration_ms": r.duration_ms, "assertions": r.assertions, "request": r.request,
                "response": r.response, "error": r.error} for r in run.results]
    return {
        "format": "testagent.api_report", "version": 1,
        "run": {"id": run.id, "status": run.status, "error": run.error, "options": run.options,
                "suite": {"id": run.suite_id, **(run.suite_snapshot or {})},
                "project": {"id": run.project_id, "name": project.name if project else ""},
                "organization": tenant.name if tenant else "",
                "requested_by": (who.display_name or who.username) if who else "",
                "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at},
        "summary": run.summary or apiexec.summarize(results),
        "results": results,
        "generated_at": run.finished_at or utcnow(),
    }


def run_html(db: Session, run: ApiRun) -> bytes:
    return apireport.run_html(report_data(db, run))


def run_json(db: Session, run: ApiRun) -> bytes:
    return apireport.to_json(report_data(db, run))


# --------------------------------------------------------------------------- worker: runs
def reap_stale() -> int:
    cutoff = utcnow() - timedelta(minutes=get_settings().stale_run_minutes)
    with session_scope() as db:
        n = db.execute(update(ApiRun).where(ApiRun.status == "running",
                                            func.coalesce(ApiRun.heartbeat_at, ApiRun.started_at) < cutoff)
                       .values(status="error", finished_at=utcnow(),
                               error="Worker stopped responding; the run was abandoned. Run it again.")).rowcount or 0
        n += db.execute(update(ApiSuite).where(ApiSuite.import_state == "running", ApiSuite.updated_at < cutoff)
                        .values(import_state="error", import_error="The import was interrupted. Try again.")).rowcount or 0
    return n


def claim(worker_id: str) -> int | None:
    with session_scope() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLAIM_LOCK_KEY})
        running = db.scalar(select(func.count()).select_from(ApiRun).where(ApiRun.status == "running")) or 0
        if running >= max(1, get_settings().max_concurrent_runs):
            return None
        run = db.scalars(select(ApiRun).join(Tenant, Tenant.id == ApiRun.tenant_id)
                         .where(ApiRun.status == "queued", Tenant.status == "active")
                         .order_by(ApiRun.created_at, ApiRun.id).with_for_update(of=ApiRun, skip_locked=True).limit(1)).first()
        if not run:
            return None
        if run.cancel_requested:
            run.status, run.finished_at = "cancelled", utcnow()
            return None
        run.status, run.started_at, run.heartbeat_at, run.worker_id = "running", utcnow(), utcnow(), worker_id
        return run.id


class _Abort(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def execute(run_id: int) -> None:
    s = get_settings()
    deadline = time.monotonic() + max(s.run_timeout_seconds, 60) * 3  # API runs: many fast requests
    status, error = "error", ""
    try:
        with session_scope() as db:
            run = db.get(ApiRun, run_id)
            suite = db.get(ApiSuite, run.suite_id)
            project = db.get(Project, run.project_id)
            tenant = db.get(Tenant, run.tenant_id)
            if not tenant or tenant.status != "active":
                raise _Abort("error", "The organization is not active; the run was not executed")
            if not suite or suite.archived or not project or project.archived or suite.tenant_id != run.tenant_id:
                raise _Abort("error", "The API suite is no longer available")
            if run.requested_by:
                u = db.get(User, run.requested_by)
                if not u or not u.is_active or u.tenant_id != run.tenant_id or not access.has(db, u, project.id, "run.execute"):
                    raise _Abort("error", "The person who started this run no longer has permission to run tests in this project")
            block = storage_policy.run_block_reason(db, run.tenant_id)
            if block:
                raise _Abort("error", block)
            pol = suite_policy(db, suite)
            if pol.problems:
                raise _Abort("error", "Target policy rejected this suite: " + "; ".join(pol.problems))
            try:
                secrets = load_secrets(suite)
            except (SecretsUnavailable, ValueError) as e:
                raise _Abort("error", f"Credentials cannot be read: {e}")
            for v in secrets.values():
                register_secret(v)
            cases = [(c.id, c.name, c.category, c.is_write, dict(c.definition)) for c in db.scalars(
                select(ApiCase).where(ApiCase.tenant_id == run.tenant_id, ApiCase.suite_id == suite.id,
                                      ApiCase.enabled.is_(True)).order_by(ApiCase.position, ApiCase.id)).all()]
            cfg = dict(base_url=suite.base_url, auth_type=suite.auth_type, auth_config=dict(suite.auth_config or {}),
                       verify_tls=suite.verify_tls, timeout_s=max(1, min(suite.timeout_ms, 120_000)) / 1000,
                       headers=dict(suite.default_headers or {}), max_ms=suite.max_response_ms)
            include_writes = bool((run.options or {}).get("include_writes")) and suite.allow_writes
        status, error = _execute_cases(run_id, cases, cfg, secrets, include_writes, deadline)
    except _Abort as a:
        status, error = a.status, a.reason
    except Exception as e:
        log.exception("API run %s crashed", run_id)
        status, error = "error", f"Internal error: {type(e).__name__}"
    _finish(run_id, status, error)


def _execute_cases(run_id: int, cases: list, cfg: dict, secrets: dict, include_writes: bool, deadline: float) -> tuple[str, str]:
    blocked: list[str] = []
    with session_scope() as db:
        pol = suite_policy(db, db.get(ApiSuite, db.get(ApiRun, run_id).suite_id))
    with guarded_client(pol, verify_tls=cfg["verify_tls"], on_block=blocked.append) as client:
        auth = apiexec.Authenticator(cfg["auth_type"], cfg["auth_config"], secrets, client, cfg["timeout_s"])
        engine = apiexec.Engine(client, cfg["base_url"], auth, timeout_s=cfg["timeout_s"], default_headers=cfg["headers"],
                                default_max_ms=cfg["max_ms"])
        qkeys = (cfg["auth_config"].get("name", ""),) if cfg["auth_type"] == "api_key" and cfg["auth_config"].get("location") == "query" else ()
        hkeys = (cfg["auth_config"].get("name", ""),) if cfg["auth_type"] == "api_key" else ()
        for i, (cid, name, cat, is_write, d) in enumerate(cases):
            with session_scope() as db:
                run = db.get(ApiRun, run_id)
                run.heartbeat_at = utcnow()
                cancelled = run.cancel_requested
            if cancelled:
                return "cancelled", "Cancelled by user"
            if time.monotonic() > deadline:
                return "error", "The run exceeded its time limit"
            if is_write and not include_writes:
                r = {"status": "skipped", "http_status": None, "duration_ms": 0, "assertions": [], "request": {}, "response": {},
                     "url": cfg["base_url"].rstrip("/") + d["path"], "method": d["method"],
                     "error": "Write request (POST/PUT/PATCH/DELETE) not sent: this run is read-only. "
                              "Allow write tests on the suite and tick 'Include write tests' to run it."}
            else:
                r = engine.run_case(d)
            red = apiexec.Redactor(list(secrets.values()) + auth.secret_values(), query_keys=qkeys)
            req = r.get("request") or {}
            row = ApiRunResult(
                idx=i, case_id=cid, name=name[:300], category=cat, method=d["method"], path=d["path"][:2048],
                url=red.url(r.get("url", ""))[:4096], status=r["status"], http_status=r.get("http_status"),
                duration_ms=int(r.get("duration_ms") or 0),
                assertions=[{**a, "detail": red.text(str(a.get("detail", "")))[:2000]} for a in r.get("assertions", [])],
                request={"headers": red.headers(req.get("headers") or {}, hkeys), "body": red.text(req.get("body", ""))},
                response={**(r.get("response") or {}), "body": red.text((r.get("response") or {}).get("body", ""))},
                error=red.text(r.get("error", ""))[:4000])
            with session_scope() as db:
                run = db.get(ApiRun, run_id)
                row.tenant_id, row.run_id = run.tenant_id, run.id
                db.add(row)
    if blocked:
        log.warning("API run %s: %s request(s) blocked by the egress policy", run_id, len(blocked))
    return "done", ""


def _finish(run_id: int, status: str, error: str) -> None:
    try:
        with session_scope() as db:
            run = db.get(ApiRun, run_id)
            res = [{"status": r.status, "duration_ms": r.duration_ms, "category": r.category, "method": r.method, "path": r.path}
                   for r in run.results]
            summary = apiexec.summarize(res)
            if status == "done":
                status = "passed" if summary["failed"] == 0 and summary["error"] == 0 else "failed"
                if summary["executed"] == 0 and summary["error"] and summary["error"] == summary["total"] - summary["skipped"]:
                    status, error = "error", "No request reached the API. See the first result for the reason."
            run.status, run.error, run.summary, run.finished_at = status, error[:4000], summary, utcnow()
            db.flush()
            run.report_sha256 = apireport.fingerprint(report_data(db, run))
        log.info("API run %s finished: %s", run_id, status)
    except Exception:
        log.exception("could not finalise API run %s", run_id)
        with session_scope() as db:
            db.execute(update(ApiRun).where(ApiRun.id == run_id, ApiRun.status == "running")
                       .values(status="error", finished_at=utcnow(), error=(error or "Failed to finalise the run")[:4000]))


def run_once(worker_id: str) -> int | None:
    rid = claim(worker_id)
    if rid:
        execute(rid)
    return rid


# --------------------------------------------------------------------------- worker: OpenAPI import by URL
def import_once() -> int | None:
    with session_scope() as db:
        suite = db.scalars(select(ApiSuite).join(Tenant, Tenant.id == ApiSuite.tenant_id)
                           .where(ApiSuite.import_state == "pending", ApiSuite.archived.is_(False), Tenant.status == "active")
                           .order_by(ApiSuite.updated_at).with_for_update(of=ApiSuite, skip_locked=True).limit(1)).first()
        if not suite:
            return None
        suite.import_state, suite.import_error = "running", ""
        sid = suite.id
        pol = suite_policy(db, suite, include_spec=True)
        url, verify = suite.spec_url, suite.verify_tls
    try:
        if pol.problems:
            raise ApiTestError("Target policy: " + "; ".join(pol.problems))
        with guarded_client(pol, verify_tls=verify) as client:
            try:
                with client.stream("GET", url, headers={"Accept": "application/json, application/yaml, text/yaml, */*",
                                                        "User-Agent": apiexec.USER_AGENT}, timeout=30) as r:
                    if r.status_code != 200:
                        raise ApiTestError(f"the specification URL returned HTTP {r.status_code}")
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_SPEC_DOWNLOAD:
                            raise ApiTestError("the specification is larger than 5 MB")
            except httpx.HTTPError as e:
                raise ApiTestError("could not download the specification: " + apiexec.describe_error(e))
        txt = bytes(buf).decode("utf-8", "replace")
        doc = apispec.parse_spec_text(txt)
        with session_scope() as db:
            suite = db.get(ApiSuite, sid)
            suite.spec_text = txt
            if not suite.base_url:
                base = apispec.spec_base_url(doc, url)
                if not base:
                    raise ApiTestError("the document does not name a server URL; enter the API base URL in the suite settings")
                parse_target(base)
                suite.base_url = base
            n = generate(db, suite, doc)
            suite.import_state, suite.import_error = "done", ""
            log.info("API suite %s: imported %s and generated %s case(s)", sid, suite.spec_title, n)
            if suite.run_after_import:
                suite.run_after_import = False
                u = db.get(User, suite.import_requested_by) if suite.import_requested_by else None
                try:
                    queue_run(db, suite, u, include_writes=False)
                except ApiTestError as e:
                    suite.import_error = f"Cases were generated but the run was not started: {e}"
    except (ApiTestError, apispec.SpecError, TargetRejected) as e:
        with session_scope() as db:
            suite = db.get(ApiSuite, sid)
            suite.import_state, suite.import_error, suite.run_after_import = "error", str(e)[:2000], False
    except Exception as e:
        log.exception("API suite %s import crashed", sid)
        with session_scope() as db:
            suite = db.get(ApiSuite, sid)
            suite.import_state, suite.import_error, suite.run_after_import = "error", f"Internal error: {type(e).__name__}", False
    return sid


def tick(worker_id: str) -> bool:
    """One unit of API-testing work for the worker loop. True if something was done."""
    if import_once():
        return True
    return bool(run_once(worker_id))


# --------------------------------------------------------------------------- AI suggestions (API container only)
SYSTEM_SUGGEST = """You are TestAgent, a senior API test engineer. You propose additional API test cases
for the endpoints you are given, by calling the submit_api_cases tool exactly once.

Rules:
- Only use the endpoints listed (method + path). Never invent hosts or absolute URLs; paths start with /.
- Never include credentials. Use auth "suite" (valid credentials), "none" or "invalid".
- Prefer read-only (GET) cases. Write requests are allowed but will only run if the customer enables them.
- Cover edge cases the deterministic generator does not: boundary values, pagination, filtering, sorting,
  unusual but valid inputs, wrong types in the body, very long strings, and business rules implied by names.
- expect.status is a list of integers or classes like "4xx". Keep assertions realistic; do not guess exact
  response bodies unless they are obvious.
- path parameters: put null to reuse an id returned by an earlier response, or a concrete value."""


def ai_suggest(suite: ApiSuite, existing: list[ApiCase], instructions: str, count: int) -> dict:
    from . import ai

    if suite.spec_source in ("url", "upload") and suite.spec_text:
        doc = apispec.parse_spec_text(suite.spec_text)
        eps = [f"{o['method']} {o['path']}" + (f" — {o['summary']}" if o["summary"] else "") for o in apispec.operations(doc)][:150]
    else:
        eps = [f"{e['method']} {e['path']}" for e in apispec.parse_endpoints(suite.endpoints_text)][:150]
    have = [f"{c.method} {c.path}: {c.name}" for c in existing][:200]
    case_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 300},
            "category": {"type": "string", "enum": list(apispec.CATEGORIES)},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
            "path": {"type": "string"},
            "path_params": {"type": "object"},
            "query": {"type": "object"},
            "headers": {"type": "object"},
            "body": {},
            "auth": {"type": "string", "enum": ["suite", "none", "invalid"]},
            "expect": {"type": "object", "properties": {
                "status": {"type": "array", "items": {}}, "max_ms": {"type": "integer"},
                "body_contains": {"type": "string"}, "json_equals": {"type": "object"}}, "required": ["status"]},
        },
        "required": ["name", "category", "method", "path", "expect"],
    }
    tool = {"name": "submit_api_cases", "description": "Submit additional API test cases.",
            "input_schema": {"type": "object", "properties": {"cases": {"type": "array", "maxItems": 25, "items": case_schema}},
                             "required": ["cases"]}}
    user = (f"API: {suite.spec_title or suite.name}\nBase URL: {suite.base_url}\n"
            f"Authentication: {suite.auth_type}\n\nEndpoints:\n" + "\n".join(eps) +
            "\n\nExisting cases (do not repeat them):\n" + ("\n".join(have) or "(none)") +
            f"\n\nNumber of new cases wanted: {count}\n")
    if instructions:
        user += f"\nTester's instructions:\n<instructions>\n{instructions[:4000]}\n</instructions>\n"
    resp = ai._call(max_tokens=get_settings().claude_max_tokens, system=SYSTEM_SUGGEST, tools=[tool],
                    tool_choice={"type": "auto"},
                    messages=[{"role": "user", "content": user + "\nCall the submit_api_cases tool exactly once."}])
    payload = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use" and b.name == "submit_api_cases"), None)
    if payload is None:
        raw = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        try:
            payload = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        raise ai.AIUnavailable("Claude did not return API test cases")
    out, rejected = [], []
    for i, c in enumerate((payload.get("cases") or [])[:25]):
        try:
            d = apispec.clean_definition(c)
            cat = c.get("category") if c.get("category") in apispec.CATEGORIES else "positive"
            out.append({"name": str(c.get("name") or f"{d['method']} {d['path']}")[:300], "category": cat,
                        "definition": d, "is_write": d["method"] in apispec.WRITE_METHODS})
        except (apispec.SpecError, TypeError, AttributeError) as e:
            rejected.append({"index": i, "error": str(e)[:300]})
    return {"cases": out, "rejected": rejected, "model": get_settings().claude_model}


def host_of(url: str) -> str:
    return normalize_host(urlsplit(url).hostname or "")
