"""Executes API test cases and uptime checks over HTTP (release 1.6).

Pure engine without database access, so it can be unit-tested with any HTTP server. In the worker
every request goes through the per-run EgressProxy (egress_proxy.py): the same allowlist, private-
address and DNS-pinning rules as browser runs apply to every connection and every redirect hop.
"""
from __future__ import annotations

import base64
import json
import re
import statistics
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl

import httpx

from . import apispec

MAX_BODY = 1_000_000          # bytes read from a response for validation
REQ_EXCERPT = 2_000           # bytes of request body kept in the result
RESP_EXCERPT = 4_000          # bytes of response body kept in the result
MASK_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"}
KEEP_RESP_HEADERS = ("content-type", "content-length", "location", "www-authenticate", "retry-after",
                     "cache-control", "strict-transport-security", "x-request-id")
INVALID_TOKEN = "testagent-invalid-credential-0000"
USER_AGENT = "TestAgent-API/1.6 (+https://testagent.coolaudit.com)"


class AuthError(Exception):
    pass


class Authenticator:
    """Applies the suite's credentials. mode: suite (valid), none (omitted), invalid (deliberately wrong)."""

    def __init__(self, auth_type: str, config: dict, secrets: dict, client: httpx.Client | None = None,
                 timeout_s: float = 15.0):
        self.type = auth_type or "none"
        self.config = config or {}
        self.secrets = secrets or {}
        self.client = client
        self.timeout_s = timeout_s
        self._token: str | None = None
        self._token_exp = 0.0

    def secret_values(self) -> list[str]:
        vals = [v for v in self.secrets.values() if isinstance(v, str) and v]
        if self._token:
            vals.append(self._token)
        if self.type == "basic" and self.secrets.get("password"):
            user = self.config.get("username", "")
            vals.append(base64.b64encode(f"{user}:{self.secrets['password']}".encode()).decode())
        return vals

    def apply(self, mode: str, headers: dict, params: dict) -> None:
        if mode == "none" or self.type == "none":
            return
        bad = mode == "invalid"
        if self.type == "api_key":
            name = self.config.get("name") or "X-API-Key"
            val = INVALID_TOKEN if bad else self.secrets.get("api_key", "")
            if self.config.get("location") == "query":
                params[name] = val
            else:
                headers[name] = val
        elif self.type == "bearer":
            headers["Authorization"] = f"Bearer {INVALID_TOKEN if bad else self.secrets.get('token', '')}"
        elif self.type == "basic":
            user = self.config.get("username", "")
            pw = INVALID_TOKEN if bad else self.secrets.get("password", "")
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        elif self.type == "oauth2":
            headers["Authorization"] = f"Bearer {INVALID_TOKEN if bad else self.oauth_token()}"

    def oauth_token(self) -> str:
        if self._token and time.monotonic() < self._token_exp - 30:
            return self._token
        if not self.client:
            raise AuthError("no HTTP client for the OAuth2 token request")
        url = self.config.get("token_url", "")
        data = {"grant_type": "client_credentials"}
        if self.config.get("scope"):
            data["scope"] = self.config["scope"]
        if self.config.get("audience"):
            data["audience"] = self.config["audience"]
        cid, csec = self.config.get("client_id", ""), self.secrets.get("client_secret", "")
        auth = None
        if self.config.get("client_auth") == "basic":
            auth = (cid, csec)
        else:
            data.update(client_id=cid, client_secret=csec)
        try:
            r = self.client.post(url, data=data, auth=auth, timeout=self.timeout_s,
                                 headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        except httpx.HTTPError as e:
            raise AuthError(f"OAuth2 token request failed: {describe_error(e)}") from e
        if r.status_code != 200:
            raise AuthError(f"OAuth2 token endpoint returned HTTP {r.status_code}")
        try:
            j = r.json()
            tok = str(j["access_token"])
        except (ValueError, KeyError, TypeError) as e:
            raise AuthError("OAuth2 token endpoint did not return an access_token") from e
        self._token = tok
        try:
            self._token_exp = time.monotonic() + float(j.get("expires_in", 3600))
        except (TypeError, ValueError):
            self._token_exp = time.monotonic() + 3600
        return tok


def describe_error(e: Exception) -> str:
    msg = str(e) or type(e).__name__
    if isinstance(e, httpx.ProxyError) or "403 Forbidden" in msg and "CONNECT" in msg.upper():
        return "Connection refused by the TestAgent target policy (host not authorised, or a private/blocked address)"
    if isinstance(e, httpx.ConnectTimeout):
        return "Connection timed out"
    if isinstance(e, httpx.ReadTimeout):
        return "No response before the timeout"
    if isinstance(e, httpx.ConnectError):
        if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate" in msg.lower():
            return "TLS certificate could not be verified: " + msg[:200]
        return "Could not connect: " + msg[:200]
    if isinstance(e, httpx.TooManyRedirects):
        return "Too many redirects"
    return f"{type(e).__name__}: {msg[:300]}"


class Redactor:
    def __init__(self, secrets: list[str], query_keys: tuple[str, ...] = ()):
        self.secrets = sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True)
        self.query_keys = {k.lower() for k in query_keys if k}

    def text(self, s: str) -> str:
        for v in self.secrets:
            s = s.replace(v, "[secret]")
        return s

    def url(self, u: str) -> str:
        p = urlsplit(u)
        if p.query and self.query_keys:
            q = [(k, "[secret]" if k.lower() in self.query_keys else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
            u = urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
        return self.text(u)

    def headers(self, h: dict, extra: tuple[str, ...] = ()) -> dict:
        mask = MASK_HEADERS | {x.lower() for x in extra}
        return {k: ("[secret]" if k.lower() in mask else self.text(str(v))[:500]) for k, v in h.items()}


def _excerpt(b: bytes, n: int) -> str:
    t = b[:n].decode("utf-8", "replace")
    return t + ("…" if len(b) > n else "")


def json_path_get(obj: Any, path: str) -> Any:
    """Very small JSON path: $.a.b[0].c"""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    for tok in re.findall(r"\.([^.\[\]]+)|\[(\d+)\]", p):
        key, idx = tok
        if key:
            if not isinstance(obj, dict) or key not in obj:
                raise KeyError(path)
            obj = obj[key]
        else:
            i = int(idx)
            if not isinstance(obj, list) or i >= len(obj):
                raise KeyError(path)
            obj = obj[i]
    return obj


class Engine:
    """Runs cases in order, carrying ids from earlier responses into later path parameters."""

    def __init__(self, client: httpx.Client, base_url: str, auth: Authenticator, *, timeout_s: float = 15.0,
                 default_headers: dict | None = None, default_max_ms: int = 0):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s
        self.default_headers = dict(default_headers or {})
        self.default_max_ms = default_max_ms
        self.collections: dict[str, dict] = {}
        self.vars: dict[str, Any] = {}

    # ---- id harvesting ----
    def _harvest(self, path: str, data: Any) -> None:
        obj = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            obj = data[0]
        elif isinstance(data, dict):
            for k in ("data", "items", "results", "content", "records", "value"):
                v = data.get(k)
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    obj = v[0]
                    break
                if isinstance(v, dict) and k == "data":
                    obj = v
                    break
            if obj is None:
                obj = data
        if not isinstance(obj, dict):
            return
        coll = re.sub(r"/\{[^}/]+\}$", "", path)
        self.collections[coll] = obj
        for k, v in obj.items():
            if isinstance(v, (str, int)) and not isinstance(v, bool) and (k.lower() == "id" or k.lower().endswith("id")):
                self.vars[k] = v

    def _param(self, d: dict, name: str) -> str:
        v = (d.get("path_params") or {}).get(name)
        if v is not None and v != "":
            return str(v)
        path = d["path"]
        i = path.find("{" + name + "}")
        coll = path[:i].rstrip("/") if i > 0 else ""
        obj = self.collections.get(coll)
        if obj:
            low = {k.lower(): k for k in obj}
            for cand in (name, "id", re.sub(r"(_id|Id|ID)$", "", name)):
                if cand.lower() in low and isinstance(obj[low[cand.lower()]], (str, int)):
                    return str(obj[low[cand.lower()]])
        if name in self.vars:
            return str(self.vars[name])
        s = (d.get("path_samples") or {}).get(name)
        return "1" if s in (None, "") else str(s)

    def build_url(self, d: dict) -> str:
        def rep(m: re.Match) -> str:
            return quote(self._param(d, m.group(1)), safe="")

        path = re.sub(r"\{([^}/]+)\}", rep, d["path"])
        return self.base_url + path

    # ---- one case ----
    def run_case(self, d: dict) -> dict:
        res: dict[str, Any] = {"status": "error", "http_status": None, "duration_ms": 0, "assertions": [],
                               "request": {}, "response": {}, "error": "", "url": "", "method": d["method"]}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.5", **self.default_headers,
                   **(d.get("headers") or {})}
        params = dict(d.get("query") or {})
        try:
            self.auth.apply(d.get("auth", "suite"), headers, params)
        except AuthError as e:
            res["error"] = str(e)
            return res
        url = self.build_url(d)
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        res["url"] = url
        kw: dict[str, Any] = {}
        body = d.get("body")
        bt = d.get("body_type")
        sent = b""
        if body is not None and d["method"] not in ("GET", "HEAD"):
            if bt == "form" and isinstance(body, dict):
                sent = urlencode({k: apispec._q(v) for k, v in body.items()}).encode()
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            elif bt == "text":
                sent = str(body).encode()
                headers.setdefault("Content-Type", "text/plain")
            else:
                sent = json.dumps(body).encode()
                headers.setdefault("Content-Type", "application/json")
            kw["content"] = sent
        res["request"] = {"headers": headers, "body": _excerpt(sent, REQ_EXCERPT) if sent else ""}
        t0 = time.monotonic()
        try:
            with self.client.stream(d["method"], url, headers=headers, timeout=self.timeout_s, **kw) as r:
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BODY:
                        break
                ms = int((time.monotonic() - t0) * 1000)
                raw = bytes(buf[:MAX_BODY])
                truncated = len(buf) > MAX_BODY
                rh = {k: v for k, v in r.headers.items() if k.lower() in KEEP_RESP_HEADERS}
                status = r.status_code
                final_url = str(r.url)
        except httpx.HTTPError as e:
            res["duration_ms"] = int((time.monotonic() - t0) * 1000)
            res["error"] = describe_error(e)
            return res
        res.update(http_status=status, duration_ms=ms)
        res["response"] = {"headers": rh, "body": _excerpt(raw, RESP_EXCERPT), "size": len(raw),
                           "truncated": truncated, "final_url": final_url if final_url != url else ""}
        exp = d.get("expect") or {}
        a = res["assertions"]
        want = exp.get("status") or ["2xx"]
        a.append({"name": f"Status is {' or '.join(str(s) for s in want)}", "ok": apispec.status_matches(want, status),
                  "detail": f"got {status}"})
        max_ms = exp.get("max_ms") or (self.default_max_ms if d.get("_perf") else 0)
        if max_ms:
            a.append({"name": f"Response time ≤ {max_ms} ms", "ok": ms <= max_ms, "detail": f"{ms} ms"})
        ctype = (rh.get("content-type") or rh.get("Content-Type") or "").split(";")[0].strip().lower()
        if exp.get("content_type") and apispec.status_matches(want, status):
            wanted = exp["content_type"].lower()
            ok = ctype == wanted or (wanted.endswith("json") and ctype.endswith("json"))
            a.append({"name": f"Content-Type is {exp['content_type']}", "ok": ok, "detail": ctype or "(none)"})
        parsed: Any = None
        is_json = ctype.endswith("json") or (raw[:1] in (b"{", b"[") and not ctype)
        if is_json and raw and not truncated:
            try:
                parsed = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                if exp.get("schema") or exp.get("json_equals"):
                    a.append({"name": "Body is valid JSON", "ok": False, "detail": "the body could not be parsed as JSON"})
        if exp.get("schema") and apispec.status_matches(want, status):
            if truncated:
                a.append({"name": "Body matches the documented schema", "ok": True, "detail": "skipped: body larger than 1 MB"})
            elif parsed is not None or not raw:
                errs = apispec.validate(exp["schema"], parsed)
                a.append({"name": "Body matches the documented schema", "ok": not errs, "detail": "; ".join(errs)[:2000] or "valid"})
        if exp.get("body_contains"):
            txt = raw.decode("utf-8", "replace")
            a.append({"name": f"Body contains “{exp['body_contains'][:80]}”", "ok": exp["body_contains"] in txt, "detail": ""})
        for p, v in (exp.get("json_equals") or {}).items():
            try:
                got = json_path_get(parsed, p)
                a.append({"name": f"{p} equals {json.dumps(v)[:80]}", "ok": got == v, "detail": f"got {json.dumps(got)[:200]}"})
            except KeyError:
                a.append({"name": f"{p} equals {json.dumps(v)[:80]}", "ok": False, "detail": "not present in the response"})
        res["status"] = "passed" if all(x["ok"] for x in a) else "failed"
        if 200 <= status < 300 and parsed is not None and d["method"] in ("GET", "POST", "PUT", "PATCH"):
            self._harvest(d["path"], parsed)
        return res


# --------------------------------------------------------------------------- uptime check
def check_once(client: httpx.Client, url: str, *, method: str = "GET", headers: dict | None = None,
               auth: Authenticator | None = None, expected: list | None = None, body_contains: str = "",
               max_ms: int = 0, timeout_s: float = 10.0) -> dict:
    """One monitoring probe. Returns {ok, http_status, duration_ms, error}."""
    h = {"User-Agent": USER_AGENT + " monitor", "Accept": "*/*", **(headers or {})}
    params: dict = {}
    try:
        if auth:
            auth.apply("suite", h, params)
    except AuthError as e:
        return {"ok": False, "http_status": None, "duration_ms": 0, "error": str(e)[:1000]}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    t0 = time.monotonic()
    try:
        with client.stream(method, url, headers=h, timeout=timeout_s) as r:
            buf = bytearray()
            if body_contains and method != "HEAD":
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BODY:
                        break
            ms = int((time.monotonic() - t0) * 1000)
            status = r.status_code
    except httpx.HTTPError as e:
        return {"ok": False, "http_status": None, "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": describe_error(e)[:1000]}
    problems = []
    if not apispec.status_matches(expected or ["2xx"], status):
        problems.append(f"HTTP {status} (expected {', '.join(str(s) for s in (expected or ['2xx']))})")
    if body_contains and body_contains not in buf.decode("utf-8", "replace"):
        problems.append(f"response does not contain “{body_contains[:80]}”")
    if max_ms and ms > max_ms:
        problems.append(f"slow response: {ms} ms (limit {max_ms} ms)")
    return {"ok": not problems, "http_status": status, "duration_ms": ms, "error": "; ".join(problems)[:1000]}


# --------------------------------------------------------------------------- summary
def summarize(results: list[dict]) -> dict:
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("passed", "failed", "error", "skipped")}
    executed = [r for r in results if r["status"] in ("passed", "failed")]
    times = sorted(r["duration_ms"] for r in results if r["status"] in ("passed", "failed", "error") and r["duration_ms"])
    by_cat: dict[str, dict] = {}
    for r in results:
        c = by_cat.setdefault(r.get("category") or "other", {"total": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0})
        c["total"] += 1
        c[r["status"]] = c.get(r["status"], 0) + 1
    endpoints = {(r.get("method"), r.get("path")) for r in results if r.get("path")}
    ran = counts["passed"] + counts["failed"] + counts["error"]
    return {
        "total": len(results), **counts,
        "pass_rate": round(100 * counts["passed"] / ran, 1) if ran else 0.0,
        "avg_ms": int(statistics.fmean(times)) if times else 0,
        "p95_ms": times[min(len(times) - 1, int(0.95 * len(times)))] if times else 0,
        "max_ms": times[-1] if times else 0,
        "endpoints": len(endpoints),
        "by_category": by_cat,
        "executed": len(executed),
    }
