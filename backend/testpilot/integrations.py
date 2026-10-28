"""Connectors to test-management and bug-tracking tools (release 1.7).

Each project can configure any number of connectors:
  azure_devops - publishes runs to Azure Test Plans (test results, optionally against test points of a plan/suite),
                 attaches the HTML report, and opens Bug work items for failures
  jira         - Jira Cloud (e-mail + API token) or Data Center (personal access token): issues for failures
  github       - GitHub / GitHub Enterprise issues
  gitlab       - GitLab.com / self-managed issues
  webhook      - signed JSON POST of the results to any tool

What is pushed is configurable per connector: the trigger (every finished run, only runs with failures, or only
when someone presses "Push"), and the run types (API test runs, automated browser runs, closed manual test runs).

Pushes go through an outbox (integration_syncs) processed by the worker with retries. One issue is kept per test
case and connector (integration_issues): a repeated failure comments on the open issue instead of opening a new
one, and a recovery adds a "passing again" note. Tokens are Fernet-encrypted and never returned by the API.
External tool URLs must be HTTPS on public addresses (no internal hosts reachable from the worker).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .models import (ApiCase, ApiRun, ApiRunResult, AutomationItem, Evidence, Integration, IntegrationIssue,
                     IntegrationSync, Project, Run, RunStep, Tenant, TestCase, TestRun, TestRunItem, utcnow)
from .security import decrypt_secret, encrypt_secret
from .urlguard import TargetRejected, check_ip, parse_target, resolve

log = logging.getLogger("testpilot.integrations")

KINDS = ("azure_devops", "jira", "github", "gitlab", "webhook")
KIND_LABEL = {"azure_devops": "Azure DevOps", "jira": "Jira", "github": "GitHub Issues", "gitlab": "GitLab Issues",
              "webhook": "Webhook"}
TRIGGERS = ("auto", "failures", "manual")
RUN_TYPES = ("api", "browser", "manual")
SOURCE_OF_TYPE = {"api": "api_run", "browser": "run", "manual": "test_run"}
TYPE_OF_SOURCE = {v: k for k, v in SOURCE_OF_TYPE.items()}
SECRET_FIELDS = {"azure_devops": ("pat",), "jira": ("token",), "github": ("token",), "gitlab": ("token",),
                 "webhook": ("url", "signing_secret")}
REQUIRED_SECRETS = {"azure_devops": ("pat",), "jira": ("token",), "github": ("token",), "gitlab": ("token",), "webhook": ("url",)}
MAX_ATTEMPTS = 5
BACKOFF_MIN = (1, 5, 15, 60, 180)
MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_ATTACH_BYTES = 8 * 1024 * 1024
UA = "TestAgent-Connectors/1.7"
# Tests point connectors at local mock servers; production never sets this.
ALLOW_PRIVATE_FOR_TESTS = False


class ConnectorError(Exception):
    pass


# --------------------------------------------------------------------------- configuration
def _s(v: Any, n: int = 500) -> str:
    return str(v or "").strip()[:n]


def _url(v: Any, what: str) -> str:
    u = _s(v, 1000).rstrip("/")
    if not u:
        raise ConnectorError(f"{what} is required")
    check_endpoint(u)
    return u


def _int(v: Any, what: str) -> int | None:
    if v in (None, ""):
        return None
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ConnectorError(f"{what} must be a number")
    if n <= 0:
        raise ConnectorError(f"{what} must be a positive number")
    return n


LABEL_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,50}$")


def _labels(v: Any) -> list[str]:
    items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v or "")) if x]
    out = [x.strip() for x in items if str(x).strip()][:10]
    for x in out:
        if not LABEL_RE.match(x):
            raise ConnectorError(f"invalid label '{x}' (letters, digits, _ . : - only)")
    return out


def parse_case_map(text: Any) -> dict[str, int]:
    """Lines 'TestAgent key or external ID , ADO test case ID'. Keys: an imported case ID (e.g. TC-001),
    case:<id>, api_case:<id>."""
    if isinstance(text, dict):
        text = "\n".join(f"{k},{v}" for k, v in text.items())
    out: dict[str, int] = {}
    for n, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,;\t]", line) if p.strip()]
        if len(parts) != 2 or not parts[1].isdigit():
            raise ConnectorError(f"test case mapping line {n}: expected 'TestAgent case, ADO test case ID'")
        out[parts[0][:100]] = int(parts[1])
        if len(out) > 20000:
            raise ConnectorError("test case mapping is limited to 20,000 lines")
    return out


def clean_config(kind: str, c: dict) -> dict:
    """Validates the non-secret settings of a connector."""
    if kind not in KINDS:
        raise ConnectorError(f"unknown connector type '{kind}'")
    c = c or {}
    common = {"create_bugs": bool(c.get("create_bugs", kind != "webhook")),
              "comment_on_pass": bool(c.get("comment_on_pass", True)),
              "max_bugs": max(1, min(int(c.get("max_bugs") or 20), 100)),
              "attach_report": bool(c.get("attach_report", True))}
    if kind == "azure_devops":
        org = _url(c.get("org_url"), "Organization URL")
        if not re.match(r"^https://", org) and not ALLOW_PRIVATE_FOR_TESTS:
            raise ConnectorError("Organization URL must start with https://")
        out = {"org_url": org, "project": _s(c.get("project"), 200), "publish_results": bool(c.get("publish_results", True)),
               "plan_id": _int(c.get("plan_id"), "Test plan ID"), "suite_id": _int(c.get("suite_id"), "Test suite ID"),
               "use_external_id": bool(c.get("use_external_id", False)), "case_map": parse_case_map(c.get("case_map")),
               "work_item_type": _s(c.get("work_item_type") or "Bug", 60), "area_path": _s(c.get("area_path"), 300),
               "iteration_path": _s(c.get("iteration_path"), 300), "tags": _labels(c.get("tags") or ["TestAgent"]),
               "priority": max(1, min(int(c.get("priority") or 2), 4))}
        if not out["project"]:
            raise ConnectorError("Azure DevOps project is required")
        if out["plan_id"] and not out["suite_id"]:
            raise ConnectorError("Choose the test suite of the plan (Test suite ID) as well")
        if not out["publish_results"] and not common["create_bugs"]:
            raise ConnectorError("Enable publishing test results, creating bugs, or both")
    elif kind == "jira":
        out = {"base_url": _url(c.get("base_url"), "Jira URL"),
               "deployment": "datacenter" if c.get("deployment") == "datacenter" else "cloud",
               "email": _s(c.get("email"), 254), "project_key": _s(c.get("project_key"), 50).upper(),
               "issue_type": _s(c.get("issue_type") or "Bug", 60), "labels": _labels(c.get("labels") or ["testagent"])}
        if out["deployment"] == "cloud" and "@" not in out["email"]:
            raise ConnectorError("Jira Cloud needs the e-mail address of the API token's account")
        if not re.match(r"^[A-Z][A-Z0-9_]{0,49}$", out["project_key"]):
            raise ConnectorError("Jira project key looks invalid (e.g. QA)")
        common["create_bugs"] = True
    elif kind == "github":
        api = _url(c.get("api_url") or "https://api.github.com", "GitHub API URL")
        repo = _s(c.get("repo"), 200)
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            raise ConnectorError("Repository must look like owner/name")
        out = {"api_url": api, "repo": repo, "labels": _labels(c.get("labels") or ["bug", "testagent"])}
        common["create_bugs"] = True
    elif kind == "gitlab":
        out = {"base_url": _url(c.get("base_url") or "https://gitlab.com", "GitLab URL"), "project": _s(c.get("project"), 300),
               "labels": _labels(c.get("labels") or ["bug", "testagent"])}
        if not out["project"]:
            raise ConnectorError("GitLab project (ID or group/name path) is required")
        common["create_bugs"] = True
    else:
        out = {"include_passed": bool(c.get("include_passed", False))}
        common.update(create_bugs=False, comment_on_pass=False)
    return {**out, **common}


def set_secrets(integ: Integration, secrets: dict, *, keep_existing: bool = True) -> None:
    old = load_secrets(integ) if keep_existing and integ.secret_enc else {}
    vals: dict[str, str] = {}
    for k in SECRET_FIELDS[integ.kind]:
        v = _s(secrets.get(k), 4000) or old.get(k, "")
        if k in REQUIRED_SECRETS[integ.kind] and not v:
            raise ConnectorError(f"{k.replace('_', ' ')} is required")
        if v:
            vals[k] = v
    if integ.kind == "webhook":
        _url(vals["url"], "Webhook URL")
    integ.secret_enc = encrypt_secret(json.dumps(vals))
    main = vals.get(REQUIRED_SECRETS[integ.kind][0], "")
    if integ.kind == "webhook":
        integ.secret_hint = f"{urlsplit(main).hostname} · …{main[-6:]}"[:120]
    else:
        integ.secret_hint = (f"…{main[-4:]}" if len(main) >= 12 else "set")


def load_secrets(integ: Integration) -> dict:
    return json.loads(decrypt_secret(integ.secret_enc)) if integ.secret_enc else {}


def public_config(integ: Integration) -> dict:
    c = dict(integ.config or {})
    if "case_map" in c:
        c["case_map"] = "\n".join(f"{k},{v}" for k, v in (c.get("case_map") or {}).items())
    return c


# --------------------------------------------------------------------------- network guard + HTTP
def check_endpoint(url: str) -> None:
    """External tools only: https on public addresses."""
    scheme, host, port = parse_target(url)
    if ALLOW_PRIVATE_FOR_TESTS:
        return
    if scheme != "https":
        raise ConnectorError("connector URLs must use https")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = resolve(host, port)
        except TargetRejected as e:
            raise ConnectorError(str(e))
    for ip in ips:
        try:
            check_ip(ip, private_ok=False)
        except TargetRejected as e:
            raise ConnectorError(f"{host}: {e}")


class Http:
    def __init__(self, headers: dict | None = None, auth: Any = None):
        self.c = httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                              headers={"User-Agent": UA, **(headers or {})}, auth=auth)

    def close(self) -> None:
        self.c.close()

    def req(self, method: str, url: str, *, ok: tuple = (200, 201, 204), **kw) -> httpx.Response:
        check_endpoint(url)
        try:
            r = self.c.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise ConnectorError(f"{method} {_short(url)}: {type(e).__name__}: {str(e)[:200]}")
        if r.status_code not in ok:
            body = r.text[:400].replace("\n", " ")
            hint = {401: " (check the token)", 403: " (the token lacks permission)", 404: " (check the URL / project)"}.get(r.status_code, "")
            raise ConnectorError(f"{method} {_short(url)} returned HTTP {r.status_code}{hint}: {body}")
        return r

    def json(self, method: str, url: str, **kw) -> Any:
        r = self.req(method, url, **kw)
        return r.json() if r.content else {}


def _short(url: str) -> str:
    p = urlsplit(url)
    return f"{p.hostname}{p.path}"[:150]


# --------------------------------------------------------------------------- normalized results
@dataclass
class Item:
    key: str                     # stable per test case: case:<id> | api_case:<id> | api_result:<run>:<idx>
    title: str
    outcome: str                 # passed | failed | error | skipped | blocked | not_run
    duration_ms: int = 0
    error: str = ""
    detail: str = ""
    external_ids: list[str] = field(default_factory=list)   # e.g. imported case ID TC-001
    attachment: Callable[[], tuple[str, bytes, str] | None] | None = None


@dataclass
class ResultSet:
    source_kind: str
    source_id: int
    title: str
    link: str
    status: str
    project: str
    started_at: datetime | None
    finished_at: datetime | None
    summary: dict
    items: list[Item]
    report: Callable[[], tuple[str, bytes, str] | None] | None = None

    @property
    def failed(self) -> bool:
        return any(i.outcome in ("failed", "error") for i in self.items) or self.status in ("failed", "error")


def _link(db: Session, tenant_id: int, path: str) -> str:
    t = db.get(Tenant, tenant_id)
    return f"{get_settings().public_url}/{t.slug if t else ''}{path}"


def load_source(db: Session, kind: str, sid: int) -> ResultSet:
    if kind == "api_run":
        from . import apitesting

        run = db.get(ApiRun, sid)
        if not run:
            raise ConnectorError("the API run no longer exists")
        proj = db.get(Project, run.project_id)
        items = []
        for r in run.results:
            failed = [a for a in r.assertions if not a.get("ok")]
            detail = f"{r.method} {r.url}\n" + "".join(f"✘ {a.get('name')} — {a.get('detail', '')}\n" for a in failed)
            if r.response and r.response.get("body"):
                detail += f"\nResponse (HTTP {r.http_status}):\n{str(r.response.get('body'))[:1500]}"
            items.append(Item(key=f"api_case:{r.case_id}" if r.case_id else f"api_result:{run.id}:{r.idx}",
                              title=r.name, outcome=r.status, duration_ms=r.duration_ms,
                              error=r.error or "; ".join(f"{a.get('name')}: {a.get('detail', '')}" for a in failed)[:2000],
                              detail=detail[:6000]))
        snap = run.suite_snapshot or {}
        return ResultSet("api_run", run.id, f"API run #{run.id} · {snap.get('name', 'API suite')}",
                         _link(db, run.tenant_id, f"/api-runs/{run.id}"), run.status, proj.name if proj else "",
                         run.started_at, run.finished_at, run.summary or {}, items,
                         report=lambda: (f"api-test-report-{run.id}.html", apitesting.run_html(db, run), "text/html"))
    if kind == "run":
        from . import report, storage

        run = db.get(Run, sid)
        if not run:
            raise ConnectorError("the run no longer exists")
        proj = db.get(Project, run.project_id)
        case = db.get(TestCase, run.test_case_id) if run.test_case_id else None
        steps = db.scalars(select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.idx)).all()
        bad = next((s for s in steps if s.status == "failed"), None)
        detail = "".join(f"{s.idx + 1}. [{s.status}] {s.action}: {s.description}\n" for s in steps)[:4000]
        if bad and bad.error:
            detail += f"\nFailed at step {bad.idx + 1}:\n{bad.error[:2000]}"

        def shot() -> tuple[str, bytes, str] | None:
            if not bad:
                return None
            ev = db.scalars(select(Evidence).where(Evidence.run_id == run.id, Evidence.step_id == bad.id,
                                                   Evidence.kind == "screenshot", Evidence.storage == "local",
                                                   Evidence.deleted_at.is_(None))).first()
            if not ev or not ev.rel_path or ev.size > MAX_ATTACH_BYTES:
                return None
            try:
                return (f"run-{run.id}-step-{bad.idx + 1}.jpg", storage.resolve_evidence(ev.rel_path).read_bytes(), ev.mime)
            except (OSError, PermissionError):
                return None

        dur = int((run.finished_at - run.started_at).total_seconds() * 1000) if run.started_at and run.finished_at else 0
        outcome = {"passed": "passed", "failed": "failed"}.get(run.status, "error")
        name = run.case_snapshot.get("name") or (case.name if case else f"Run {run.id}")
        items = [Item(key=f"case:{run.test_case_id}" if run.test_case_id else f"run:{run.id}", title=name, outcome=outcome,
                      duration_ms=dur, error=run.error or "", detail=detail,
                      external_ids=[case.external_id] if case and case.external_id else [], attachment=shot)]

        def rep() -> tuple[str, bytes, str] | None:
            if run.storage_mode == "external":
                return None
            return (f"test-report-run-{run.id}.html", report.to_html(db, report.build_data(db, run)), "text/html")

        return ResultSet("run", run.id, f"Run #{run.id} · {name}", _link(db, run.tenant_id, f"/runs/{run.id}"), run.status,
                         proj.name if proj else "", run.started_at, run.finished_at, run.summary or {}, items, report=rep)
    if kind == "test_run":
        tr = db.get(TestRun, sid)
        if not tr:
            raise ConnectorError("the test run no longer exists")
        proj = db.get(Project, tr.project_id)
        omap = {"passed": "passed", "failed": "failed", "blocked": "blocked", "retest": "not_run", "skipped": "skipped",
                "untested": "not_run"}
        items = []
        for it in db.scalars(select(TestRunItem).where(TestRunItem.test_run_id == tr.id).order_by(TestRunItem.position)).all():
            c = it.case
            parts = [("Expected", c.expected_result), ("Actual", it.actual_result), ("Notes", it.notes),
                     ("Procedure", c.procedure), ("Pre-conditions", c.preconditions), ("Test data", c.test_data)]
            detail = "".join(f"{k}:\n{v}\n\n" for k, v in parts if v)[:6000]
            title = f"{c.external_id} · {c.name}" if c.external_id else c.name
            items.append(Item(key=f"case:{c.id}", title=title, outcome=omap.get(it.status, "not_run"),
                              error=(it.actual_result or "")[:2000], detail=detail,
                              external_ids=[c.external_id] if c.external_id else []))
        counts: dict[str, int] = {}
        for i in items:
            counts[i.outcome] = counts.get(i.outcome, 0) + 1
        status = "failed" if counts.get("failed") else "passed" if items and counts.get("passed") == len(items) else "completed"
        return ResultSet("test_run", tr.id, f"Test run · {tr.name}", _link(db, tr.tenant_id, f"/test-runs/{tr.id}"), status,
                         proj.name if proj else "", tr.created_at, tr.closed_at or utcnow(),
                         {"total": len(items), **counts}, items)
    raise ConnectorError(f"unknown source '{kind}'")


def _issue_text(rs: ResultSet, item: Item) -> str:
    return (f"TestAgent recorded a failure.\n\nProject: {rs.project}\nSource: {rs.title}\nTest: {item.title}\n"
            f"Result: {item.outcome}{f' ({item.duration_ms} ms)' if item.duration_ms else ''}\n\n"
            + (f"Error:\n{item.error}\n\n" if item.error else "")
            + (f"Details:\n{item.detail}\n\n" if item.detail else "")
            + f"Open in TestAgent: {rs.link}\n")


def _html(text: str) -> str:
    from html import escape

    return "<div>" + escape(text).replace("\n", "<br>") + "</div>"


# --------------------------------------------------------------------------- connectors
class Connector:
    def __init__(self, integ: Integration, secrets: dict):
        self.i, self.cfg, self.sec = integ, integ.config or {}, secrets

    def test(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        self.http.close()

    # issues
    def create_issue(self, rs: ResultSet, item: Item) -> tuple[str, str]:
        raise NotImplementedError

    def comment(self, external_id: str, text: str) -> None:
        raise NotImplementedError

    def is_closed(self, external_id: str) -> bool:
        return False

    def publish(self, rs: ResultSet) -> dict:
        return {}


class AzureDevOps(Connector):
    API = "7.1"

    def __init__(self, integ, secrets):
        super().__init__(integ, secrets)
        tok = base64.b64encode(f":{secrets['pat']}".encode()).decode()
        self.http = Http({"Authorization": f"Basic {tok}", "Accept": "application/json"})
        self.base = f"{self.cfg['org_url']}/{quote(self.cfg['project'], safe='')}"

    def _u(self, path: str, version: str | None = None) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.base}/_apis/{path}{sep}api-version={version or self.API}"

    def test(self) -> str:
        p = self.http.json("GET", f"{self.cfg['org_url']}/_apis/projects/{quote(self.cfg['project'], safe='')}?api-version={self.API}")
        msg = f"Connected to project '{p.get('name', self.cfg['project'])}'"
        if self.cfg.get("plan_id"):
            pts = self._points()
            msg += f"; test plan {self.cfg['plan_id']} / suite {self.cfg['suite_id']} has {len(pts)} test point(s)"
        return msg

    def _points(self) -> dict[int, int]:
        out: dict[int, int] = {}
        skip = 0
        while True:
            j = self.http.json("GET", self._u(f"test/Plans/{self.cfg['plan_id']}/Suites/{self.cfg['suite_id']}/points?$top=1000&$skip={skip}"))
            vals = j.get("value", [])
            for p in vals:
                tc = (p.get("testCase") or {}).get("id")
                if tc and str(tc).isdigit():
                    out.setdefault(int(tc), int(p["id"]))
            if len(vals) < 1000:
                return out
            skip += 1000

    def ado_case_id(self, item: Item) -> int | None:
        m = self.cfg.get("case_map") or {}
        for k in [item.key, *item.external_ids]:
            if k in m:
                return int(m[k])
        if self.cfg.get("use_external_id"):
            for x in item.external_ids:
                d = re.sub(r"^(TC|ADO)[-_ ]?", "", x.strip(), flags=re.I)
                if d.isdigit():
                    return int(d)
        return None

    OUTCOME = {"passed": "Passed", "failed": "Failed", "error": "Error", "blocked": "Blocked", "skipped": "NotApplicable",
               "not_run": "NotExecuted"}

    def publish(self, rs: ResultSet) -> dict:
        if not self.cfg.get("publish_results"):
            return {}
        points: dict[int, int] = {}
        if self.cfg.get("plan_id"):
            points = self._points()
        matched: dict[int, list[Item]] = {}
        unmatched: list[Item] = []
        for it in rs.items:
            tc = self.ado_case_id(it)
            if tc and tc in points:
                matched.setdefault(tc, []).append(it)
            else:
                unmatched.append(it)
        body: dict[str, Any] = {"name": f"TestAgent · {rs.title}"[:250], "automated": rs.source_kind != "test_run",
                                "state": "InProgress", "comment": f"Imported from TestAgent: {rs.link}"}
        if rs.started_at:
            body["startedDate"] = rs.started_at.isoformat()
        if self.cfg.get("plan_id"):
            body["plan"] = {"id": str(self.cfg["plan_id"])}
            if matched:
                body["pointIds"] = [points[tc] for tc in matched]
        run = self.http.json("POST", self._u("test/runs"), json=body)
        rid = run["id"]
        out = {"ado_run_id": rid, "ado_run_url": run.get("webAccessUrl") or f"{self.base}/_testManagement/runs?runId={rid}",
               "matched": sum(len(v) for v in matched.values()), "unmatched": len(unmatched)}

        def res(it: Item) -> dict:
            r = {"outcome": self.OUTCOME.get(it.outcome, "NotExecuted"), "state": "Completed",
                 "durationInMs": it.duration_ms or 0, "comment": f"TestAgent: {rs.link}"[:1000]}
            if it.outcome in ("failed", "error") and it.error:
                r["errorMessage"] = it.error[:4000]
            return r

        if matched:
            existing = self.http.json("GET", self._u(f"test/Runs/{rid}/results?$top=1000")).get("value", [])
            updates = []
            for r in existing:
                tc = (r.get("testCase") or {}).get("id")
                if tc and str(tc).isdigit() and int(tc) in matched:
                    it = matched[int(tc)][0]
                    updates.append({"id": r["id"], **res(it)})
            for i in range(0, len(updates), 200):
                self.http.json("PATCH", self._u(f"test/Runs/{rid}/results"), json=updates[i:i + 200])
        if unmatched:
            rows = [{"testCaseTitle": it.title[:250], "automatedTestName": it.key, "automatedTestStorage": "TestAgent",
                     "automatedTestType": "TestAgent", "priority": 2, **res(it)} for it in unmatched]
            for i in range(0, len(rows), 200):
                self.http.json("POST", self._u(f"test/Runs/{rid}/results"), json=rows[i:i + 200])
        if self.cfg.get("attach_report") and rs.report:
            rep = rs.report()
            if rep and len(rep[1]) <= MAX_REPORT_BYTES:
                try:
                    self.http.json("POST", self._u(f"test/Runs/{rid}/attachments", "7.1-preview.1"),
                                   json={"stream": base64.b64encode(rep[1]).decode(), "fileName": rep[0],
                                         "comment": "TestAgent report", "attachmentType": "GeneralAttachment"})
                    out["report_attached"] = True
                except ConnectorError as e:
                    out["report_attach_error"] = str(e)[:300]
        self.http.json("PATCH", self._u(f"test/Runs/{rid}"), json={"state": "Completed", "completedDate": (rs.finished_at or utcnow()).isoformat()})
        return out

    def create_issue(self, rs: ResultSet, item: Item) -> tuple[str, str]:
        text = _issue_text(rs, item)
        wit = self.cfg.get("work_item_type") or "Bug"
        field_name = "/fields/Microsoft.VSTS.TCM.ReproSteps" if wit.lower() == "bug" else "/fields/System.Description"
        ops = [{"op": "add", "path": "/fields/System.Title", "value": f"[TestAgent] {item.title}"[:255]},
               {"op": "add", "path": field_name, "value": _html(text)},
               {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(self.cfg.get("tags") or ["TestAgent"])},
               {"op": "add", "path": "/relations/-", "value": {"rel": "Hyperlink", "url": rs.link}}]
        if wit.lower() == "bug":
            ops.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": self.cfg.get("priority", 2)})
        for k, f in (("area_path", "System.AreaPath"), ("iteration_path", "System.IterationPath")):
            if self.cfg.get(k):
                ops.append({"op": "add", "path": f"/fields/{f}", "value": self.cfg[k]})
        att = item.attachment() if item.attachment else None
        if att:
            up = self.http.json("POST", self._u(f"wit/attachments?fileName={quote(att[0])}"), content=att[1],
                                headers={"Content-Type": "application/octet-stream"})
            ops.append({"op": "add", "path": "/relations/-", "value": {"rel": "AttachedFile", "url": up["url"],
                                                                       "attributes": {"comment": "Failure screenshot"}}})
        wi = self.http.json("POST", self._u(f"wit/workitems/${quote(wit)}"), json=ops,
                            headers={"Content-Type": "application/json-patch+json"})
        url = ((wi.get("_links") or {}).get("html") or {}).get("href") or f"{self.base}/_workitems/edit/{wi['id']}"
        return str(wi["id"]), url

    def comment(self, external_id: str, text: str) -> None:
        self.http.json("PATCH", self._u(f"wit/workitems/{external_id}"),
                       json=[{"op": "add", "path": "/fields/System.History", "value": _html(text)}],
                       headers={"Content-Type": "application/json-patch+json"})

    def is_closed(self, external_id: str) -> bool:
        wi = self.http.json("GET", self._u(f"wit/workitems/{external_id}?fields=System.State"))
        return (wi.get("fields") or {}).get("System.State", "") in ("Closed", "Done", "Removed", "Resolved")


class Jira(Connector):
    def __init__(self, integ, secrets):
        super().__init__(integ, secrets)
        if self.cfg.get("deployment") == "datacenter":
            self.http = Http({"Authorization": f"Bearer {secrets['token']}", "Accept": "application/json"})
        else:
            self.http = Http({"Accept": "application/json"}, auth=(self.cfg.get("email", ""), secrets["token"]))
        self.base = self.cfg["base_url"]

    def test(self) -> str:
        me = self.http.json("GET", f"{self.base}/rest/api/2/myself")
        p = self.http.json("GET", f"{self.base}/rest/api/2/project/{quote(self.cfg['project_key'])}")
        types = [t.get("name") for t in p.get("issueTypes", [])]
        msg = f"Signed in as {me.get('displayName') or me.get('name')}; project {p.get('key')} – {p.get('name')}"
        if types and self.cfg.get("issue_type") not in types:
            raise ConnectorError(f"{msg}, but issue type '{self.cfg.get('issue_type')}' does not exist there (available: {', '.join(types[:12])})")
        return msg

    def create_issue(self, rs, item):
        j = self.http.json("POST", f"{self.base}/rest/api/2/issue", json={"fields": {
            "project": {"key": self.cfg["project_key"]}, "summary": f"[TestAgent] {item.title}"[:250],
            "description": _issue_text(rs, item), "issuetype": {"name": self.cfg.get("issue_type", "Bug")},
            "labels": self.cfg.get("labels") or []}})
        key = j["key"]
        att = item.attachment() if item.attachment else None
        if att:
            try:
                self.http.req("POST", f"{self.base}/rest/api/2/issue/{key}/attachments", headers={"X-Atlassian-Token": "no-check"},
                              files={"file": (att[0], att[1], att[2])})
            except ConnectorError as e:
                log.warning("jira attachment failed: %s", e)
        return key, f"{self.base}/browse/{key}"

    def comment(self, external_id, text):
        self.http.json("POST", f"{self.base}/rest/api/2/issue/{external_id}/comment", json={"body": text})

    def is_closed(self, external_id):
        j = self.http.json("GET", f"{self.base}/rest/api/2/issue/{external_id}?fields=status")
        return (((j.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key") == "done"


class GitHub(Connector):
    def __init__(self, integ, secrets):
        super().__init__(integ, secrets)
        self.http = Http({"Authorization": f"Bearer {secrets['token']}", "Accept": "application/vnd.github+json",
                          "X-GitHub-Api-Version": "2022-11-28"})
        self.base = f"{self.cfg['api_url']}/repos/{self.cfg['repo']}"

    def test(self):
        j = self.http.json("GET", self.base)
        if not j.get("has_issues", True):
            raise ConnectorError(f"Issues are disabled in {j.get('full_name')}")
        return f"Connected to {j.get('full_name')}"

    def create_issue(self, rs, item):
        body = _issue_text(rs, item).replace("Open in TestAgent:", "**Open in TestAgent:**")
        j = self.http.json("POST", f"{self.base}/issues", json={"title": f"[TestAgent] {item.title}"[:250],
                                                                 "body": f"```\n{body}\n```" if "```" not in body else body,
                                                                 "labels": self.cfg.get("labels") or []})
        return str(j["number"]), j.get("html_url", "")

    def comment(self, external_id, text):
        self.http.json("POST", f"{self.base}/issues/{external_id}/comments", json={"body": text})

    def is_closed(self, external_id):
        return self.http.json("GET", f"{self.base}/issues/{external_id}").get("state") == "closed"


class GitLab(Connector):
    def __init__(self, integ, secrets):
        super().__init__(integ, secrets)
        self.http = Http({"PRIVATE-TOKEN": secrets["token"], "Accept": "application/json"})
        self.base = f"{self.cfg['base_url']}/api/v4/projects/{quote(self.cfg['project'], safe='')}"

    def test(self):
        j = self.http.json("GET", self.base)
        if j.get("issues_enabled") is False:
            raise ConnectorError(f"Issues are disabled in {j.get('path_with_namespace')}")
        return f"Connected to {j.get('path_with_namespace') or j.get('name')}"

    def create_issue(self, rs, item):
        desc = _issue_text(rs, item)
        att = item.attachment() if item.attachment else None
        if att:
            try:
                up = self.http.json("POST", f"{self.base}/uploads", files={"file": (att[0], att[1], att[2])})
                desc += f"\n\n{up.get('markdown', '')}"
            except ConnectorError as e:
                log.warning("gitlab upload failed: %s", e)
        j = self.http.json("POST", f"{self.base}/issues", json={"title": f"[TestAgent] {item.title}"[:250], "description": desc,
                                                                 "labels": ",".join(self.cfg.get("labels") or [])})
        return str(j["iid"]), j.get("web_url", "")

    def comment(self, external_id, text):
        self.http.json("POST", f"{self.base}/issues/{external_id}/notes", json={"body": text})

    def is_closed(self, external_id):
        return self.http.json("GET", f"{self.base}/issues/{external_id}").get("state") == "closed"


class Webhook(Connector):
    def __init__(self, integ, secrets):
        super().__init__(integ, secrets)
        self.http = Http({"Content-Type": "application/json"})

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode()
        h = {"X-TestAgent-Event": payload["event"]}
        if self.sec.get("signing_secret"):
            h["X-TestAgent-Signature"] = "sha256=" + hmac.new(self.sec["signing_secret"].encode(), body, hashlib.sha256).hexdigest()
        self.http.req("POST", self.sec["url"], content=body, headers=h, ok=(200, 201, 202, 204))

    def test(self):
        self._post({"event": "testagent.ping", "sent_at": utcnow().isoformat(), "connector": self.i.name})
        return "Test payload delivered"

    def publish(self, rs):
        items = [i for i in rs.items if self.cfg.get("include_passed") or i.outcome not in ("passed",)][:1000]
        self._post({"event": "testagent.results", "sent_at": utcnow().isoformat(),
                    "source": {"kind": TYPE_OF_SOURCE.get(rs.source_kind), "id": rs.source_id, "title": rs.title, "link": rs.link,
                               "status": rs.status, "started_at": rs.started_at, "finished_at": rs.finished_at},
                    "project": rs.project, "summary": rs.summary,
                    "items": [{"key": i.key, "title": i.title, "outcome": i.outcome, "duration_ms": i.duration_ms,
                               "error": i.error[:2000]} for i in items]})
        return {"delivered_items": len(items)}


CONNECTORS = {"azure_devops": AzureDevOps, "jira": Jira, "github": GitHub, "gitlab": GitLab, "webhook": Webhook}


def connector(integ: Integration) -> Connector:
    return CONNECTORS[integ.kind](integ, load_secrets(integ))


def test_connection(integ: Integration) -> str:
    c = connector(integ)
    try:
        return c.test()
    finally:
        c.close()


# --------------------------------------------------------------------------- processing
def _process_issues(db: Session, integ: Integration, c: Connector, rs: ResultSet) -> dict:
    cfg = integ.config or {}
    out = {"issues_created": [], "issues_updated": [], "issues_recovered": [], "issues_skipped": 0}
    if not cfg.get("create_bugs"):
        return out
    budget = int(cfg.get("max_bugs") or 20)
    for item in rs.items:
        row = db.scalars(select(IntegrationIssue).where(IntegrationIssue.integration_id == integ.id,
                                                        IntegrationIssue.case_key == item.key[:80])).first()
        src = f"{rs.source_kind}:{rs.source_id}"
        if item.outcome in ("failed", "error"):
            if row and row.last_source == src:
                continue  # already handled in an earlier attempt of this push
            if row:
                if not c.is_closed(row.external_id):
                    c.comment(row.external_id, f"✘ Failing again in TestAgent: {rs.title}\n{item.error[:1500]}\n{rs.link}")
                    row.state, row.last_source = "failing", src
                    out["issues_updated"].append({"id": row.external_id, "url": row.external_url})
                    db.flush()
                    continue
                db.delete(row)  # closed in the tracker: open a new one
                db.flush()
                row = None
            if budget <= 0:
                out["issues_skipped"] += 1
                continue
            budget -= 1
            ext_id, url = c.create_issue(rs, item)
            db.add(IntegrationIssue(tenant_id=integ.tenant_id, integration_id=integ.id, case_key=item.key[:80],
                                    title=item.title[:300], external_id=ext_id, external_url=url[:1000], state="failing",
                                    last_source=src))
            db.flush()
            out["issues_created"].append({"id": ext_id, "url": url, "case": item.title[:120]})
        elif item.outcome == "passed" and row and row.state == "failing" and cfg.get("comment_on_pass", True):
            try:
                if not c.is_closed(row.external_id):
                    c.comment(row.external_id, f"✔ Passing again in TestAgent: {rs.title}\n{rs.link}")
            except ConnectorError as e:
                log.info("recovery comment failed: %s", e)
            row.state, row.last_source = "passing", src
            out["issues_recovered"].append({"id": row.external_id, "url": row.external_url})
            db.flush()
    return out


def process(sync_id: int) -> str:
    with session_scope() as db:
        s = db.get(IntegrationSync, sync_id)
        integ = db.get(Integration, s.integration_id)
        result = dict(s.result or {})
        try:
            if not integ or not integ.enabled:
                raise ConnectorError("the connector is disabled")
            rs = load_source(db, s.source_kind, s.source_id)
            c = connector(integ)
            try:
                if not result.get("published"):
                    pub = c.publish(rs)
                    result.update(pub, published=True)
                    s.result = dict(result)
                    db.flush()
                result.update(_process_issues(db, integ, c, rs))
            finally:
                c.close()
            s.status, s.error, s.result, s.finished_at = "done", "", result, utcnow()
            integ.status, integ.last_error, integ.last_sync_at = "ok", "", utcnow()
            return "done"
        except Exception as e:  # any connector failure is recorded and retried
            msg = str(e) if isinstance(e, (ConnectorError, TargetRejected)) else f"{type(e).__name__}: {str(e)[:300]}"
            if not isinstance(e, (ConnectorError, TargetRejected)):
                log.exception("integration sync %s failed", sync_id)
            s.attempts += 1
            s.error, s.result = msg[:2000], result
            if s.attempts >= MAX_ATTEMPTS:
                s.status, s.finished_at = "error", utcnow()
            else:
                s.status = "pending"
                s.next_attempt_at = utcnow() + timedelta(minutes=BACKOFF_MIN[min(s.attempts - 1, len(BACKOFF_MIN) - 1)])
            if integ:
                integ.status, integ.last_error = "error", msg[:1000]
            return s.status


def enqueue(db: Session, integ: Integration, source_kind: str, source_id: int, user_id: int | None = None,
            force: bool = False) -> IntegrationSync:
    s = db.scalars(select(IntegrationSync).where(IntegrationSync.integration_id == integ.id, IntegrationSync.source_kind == source_kind,
                                                 IntegrationSync.source_id == source_id)).first()
    if s:
        if force and s.status in ("done", "error"):
            s.status, s.attempts, s.error, s.next_attempt_at, s.finished_at = "pending", 0, "", utcnow(), None
            s.result, s.requested_by = {}, user_id
        return s
    s = IntegrationSync(tenant_id=integ.tenant_id, integration_id=integ.id, project_id=integ.project_id,
                        source_kind=source_kind, source_id=source_id, requested_by=user_id)
    db.add(s)
    db.flush()
    return s


def source_project(db: Session, kind: str, sid: int) -> tuple[int, int] | None:
    model = {"api_run": ApiRun, "run": Run, "test_run": TestRun}.get(kind)
    obj = db.get(model, sid) if model else None
    return (obj.tenant_id, obj.project_id) if obj else None


def scan(limit: int = 50) -> int:
    """Queues finished runs for connectors with an automatic trigger."""
    n = 0
    with session_scope() as db:
        integs = db.scalars(select(Integration).join(Tenant, Tenant.id == Integration.tenant_id)
                            .where(Integration.enabled.is_(True), Integration.trigger.in_(("auto", "failures")),
                                   Tenant.status == "active")).all()
        for integ in integs:
            types = set(integ.run_types or [])
            failures_only = integ.trigger == "failures"
            synced = lambda kind, col: ~exists().where(IntegrationSync.integration_id == integ.id,  # noqa: E731
                                                       IntegrationSync.source_kind == kind, IntegrationSync.source_id == col)
            if "api" in types:
                q = select(ApiRun).where(ApiRun.tenant_id == integ.tenant_id, ApiRun.project_id == integ.project_id,
                                         ApiRun.status.in_(("passed", "failed")), ApiRun.finished_at >= integ.active_since,
                                         synced("api_run", ApiRun.id))
                if failures_only:
                    q = q.where(ApiRun.status == "failed")
                for r in db.scalars(q.order_by(ApiRun.id).limit(limit)).all():
                    enqueue(db, integ, "api_run", r.id)
                    n += 1
            if "browser" in types:
                trial = exists().where(AutomationItem.trial_run_id == Run.id)
                q = select(Run).where(Run.tenant_id == integ.tenant_id, Run.project_id == integ.project_id,
                                      Run.status.in_(("passed", "failed")), Run.finished_at >= integ.active_since,
                                      ~trial, synced("run", Run.id))
                if failures_only:
                    q = q.where(Run.status == "failed")
                for r in db.scalars(q.order_by(Run.id).limit(limit)).all():
                    enqueue(db, integ, "run", r.id)
                    n += 1
            if "manual" in types:
                q = select(TestRun).where(TestRun.tenant_id == integ.tenant_id, TestRun.project_id == integ.project_id,
                                          TestRun.status == "closed", TestRun.closed_at >= integ.active_since,
                                          synced("test_run", TestRun.id))
                if failures_only:
                    q = q.where(exists().where(TestRunItem.test_run_id == TestRun.id, TestRunItem.status == "failed"))
                for r in db.scalars(q.order_by(TestRun.id).limit(limit)).all():
                    enqueue(db, integ, "test_run", r.id)
                    n += 1
    return n


def due(limit: int = 5) -> list[int]:
    with session_scope() as db:
        rows = db.scalars(select(IntegrationSync).where(IntegrationSync.status == "pending",
                                                        IntegrationSync.next_attempt_at <= utcnow())
                          .order_by(IntegrationSync.next_attempt_at).with_for_update(skip_locked=True).limit(limit)).all()
        for r in rows:
            r.status = "running"
        return [r.id for r in rows]


def reap_stale() -> int:
    with session_scope() as db:
        rows = db.scalars(select(IntegrationSync).where(IntegrationSync.status == "running",
                                                        IntegrationSync.updated_at < utcnow() - timedelta(minutes=15))).all()
        for r in rows:
            r.status, r.next_attempt_at = "pending", utcnow()
        return len(rows)


def tick() -> int:
    """Worker housekeeping: queue finished runs, then push a few due syncs."""
    scan()
    done = 0
    for sid in due():
        process(sid)
        done += 1
    return done


def sync_out(s: IntegrationSync) -> dict:
    return {"id": s.id, "integration_id": s.integration_id, "source_kind": s.source_kind, "source_type": TYPE_OF_SOURCE.get(s.source_kind),
            "source_id": s.source_id, "status": s.status, "attempts": s.attempts, "error": s.error, "result": s.result or {},
            "created_at": s.created_at, "updated_at": s.updated_at, "finished_at": s.finished_at,
            "next_attempt_at": s.next_attempt_at}

