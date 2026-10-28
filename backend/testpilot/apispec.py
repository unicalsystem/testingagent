"""API test generation (release 1.6).

Turns an OpenAPI 3.x / Swagger 2.0 document -- or, without a spec, a plain list of endpoints -- into
deterministic API test cases. No AI is used here: Claude is only asked for extra cases when the
customer explicitly requests it (see apitesting.ai_suggest).

A case is a plain JSON document (stored in ApiCase.definition):

    {"method": "GET", "path": "/pets/{petId}", "path_params": {"petId": null}, "path_samples": {"petId": 1},
     "query": {"limit": "1"}, "headers": {}, "body": null, "body_type": null, "auth": "suite",
     "expect": {"status": [200], "content_type": "application/json", "schema": {...}, "max_ms": 2000,
                "body_contains": "", "json_equals": {}}}

path_params with a null value are resolved when the run executes, from ids returned by earlier
responses (e.g. GET /pets feeds GET /pets/{petId}), falling back to path_samples.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CATEGORIES = ("positive", "auth", "negative", "performance")
MAX_SPEC_BYTES = 5_000_000
MAX_OPERATIONS = 400
MAX_CASES = 1500
MAX_SCHEMA_JSON = 60_000
SKIP_HEADERS = {"authorization", "content-type", "accept", "content-length", "host", "cookie"}


class SpecError(ValueError):
    pass


# --------------------------------------------------------------------------- parsing
def parse_spec_text(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise SpecError("the specification is empty")
    if len(text.encode("utf-8", "ignore")) > MAX_SPEC_BYTES:
        raise SpecError("the specification is larger than 5 MB")
    t = text.strip()
    if t.startswith("{"):
        try:
            data = json.loads(t)
        except ValueError as e:
            raise SpecError(f"invalid JSON: {e}") from e
    else:
        try:
            import yaml

            data = yaml.safe_load(t)
        except Exception as e:  # yaml.YAMLError and friends
            raise SpecError(f"invalid YAML: {str(e)[:300]}") from e
    if not isinstance(data, dict):
        raise SpecError("this is not an OpenAPI / Swagger document")
    is3 = str(data.get("openapi", "")).startswith("3")
    is2 = str(data.get("swagger", "")).startswith("2")
    if not (is3 or is2):
        raise SpecError("only OpenAPI 3.x and Swagger 2.0 documents are supported")
    if not isinstance(data.get("paths"), dict) or not data["paths"]:
        raise SpecError("the document has no paths")
    return data


def spec_title(doc: dict) -> str:
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    t = str(info.get("title") or "API")[:200]
    v = str(info.get("version") or "")[:40]
    return f"{t} {v}".strip()


def spec_base_url(doc: dict, spec_url: str = "") -> str:
    """Best guess of the API base URL from the document (servers / host+basePath)."""
    if str(doc.get("openapi", "")).startswith("3"):
        servers = doc.get("servers") or []
        if servers and isinstance(servers[0], dict) and isinstance(servers[0].get("url"), str):
            u = servers[0]["url"]
            for name, var in (servers[0].get("variables") or {}).items():
                if isinstance(var, dict) and "default" in var:
                    u = u.replace("{" + name + "}", str(var["default"]))
            if urlsplit(u).scheme in ("http", "https"):
                return u.rstrip("/")
            if spec_url:
                return urljoin(spec_url, u).rstrip("/")
        if spec_url:
            p = urlsplit(spec_url)
            return f"{p.scheme}://{p.netloc}"
        return ""
    host = doc.get("host")
    if not host and spec_url:
        host = urlsplit(spec_url).netloc
    if not host:
        return ""
    schemes = doc.get("schemes") or [urlsplit(spec_url).scheme or "https"]
    scheme = "https" if "https" in schemes else str(schemes[0])
    return f"{scheme}://{host}{doc.get('basePath') or ''}".rstrip("/")


def _resolve(doc: dict, node: Any) -> dict:
    """Follow local $refs (#/...) at the top of a node. External refs resolve to an empty schema."""
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen or len(seen) > 30:
            return {}
        seen.add(ref)
        cur: Any = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return {}
        node = cur
    return node if isinstance(node, dict) else {}


def deref(doc: dict, schema: Any, depth: int = 0, budget: list[int] | None = None) -> dict:
    """Fully dereferenced copy of a schema, depth- and size-limited (recursive schemas are cut)."""
    budget = budget if budget is not None else [3000]
    budget[0] -= 1
    s = _resolve(doc, schema)
    if depth > 8 or budget[0] <= 0:
        return {}
    out: dict = {}
    for k, v in s.items():
        if k in ("properties", "patternProperties") and isinstance(v, dict):
            out[k] = {pk: deref(doc, pv, depth + 1, budget) for pk, pv in list(v.items())[:200]}
        elif k in ("items", "additionalProperties", "not") and isinstance(v, dict):
            out[k] = deref(doc, v, depth + 1, budget)
        elif k in ("allOf", "oneOf", "anyOf") and isinstance(v, list):
            out[k] = [deref(doc, x, depth + 1, budget) for x in v[:20]]
        elif k in ("example", "examples", "default", "enum", "description", "xml", "externalDocs"):
            if k in ("example", "default", "enum"):
                out[k] = v
        elif not isinstance(v, (dict, list)) or k in ("required", "type"):
            out[k] = v
    return out


def _types(s: dict) -> list[str]:
    t = s.get("type")
    if isinstance(t, list):
        return [str(x) for x in t]
    if isinstance(t, str):
        return [t]
    if "properties" in s:
        return ["object"]
    if "items" in s:
        return ["array"]
    return []


# --------------------------------------------------------------------------- sample values
def sample(doc: dict, schema: Any, depth: int = 0, name: str = "") -> Any:
    s = _resolve(doc, schema)
    if depth > 6:
        return None
    if "example" in s:
        return copy.deepcopy(s["example"])
    if isinstance(s.get("examples"), list) and s["examples"]:
        return copy.deepcopy(s["examples"][0])
    if "default" in s:
        return copy.deepcopy(s["default"])
    if isinstance(s.get("enum"), list) and s["enum"]:
        return copy.deepcopy(s["enum"][0])
    if "const" in s:
        return copy.deepcopy(s["const"])
    if isinstance(s.get("allOf"), list):
        merged: dict = {}
        for part in s["allOf"]:
            v = sample(doc, part, depth + 1, name)
            if isinstance(v, dict):
                merged.update(v)
        if merged:
            return merged
    for k in ("oneOf", "anyOf"):
        if isinstance(s.get(k), list) and s[k]:
            return sample(doc, s[k][0], depth + 1, name)
    types = [t for t in _types(s) if t != "null"] or ["string"]
    t = types[0]
    if t == "object":
        props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
        req = [r for r in (s.get("required") or []) if isinstance(r, str)]
        keys = [k for k in props if k in req] or list(props)[:12]
        if not req and props:
            # readOnly properties are produced by the server, not sent
            keys = [k for k in keys if not _resolve(doc, props[k]).get("readOnly")]
        return {k: sample(doc, props[k], depth + 1, k) for k in keys[:30]}
    if t == "array":
        item = sample(doc, s.get("items", {}), depth + 1, name)
        return [] if item is None else [item]
    if t == "integer":
        return int(s.get("minimum", 1) if isinstance(s.get("minimum"), (int, float)) else 1)
    if t == "number":
        return float(s.get("minimum", 1) if isinstance(s.get("minimum"), (int, float)) else 1.5)
    if t == "boolean":
        return True
    fmt = str(s.get("format", ""))
    by_fmt = {"date-time": "2026-01-01T09:00:00Z", "date": "2026-01-01", "email": "qa.tester@example.com",
              "uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "uri": "https://example.com", "url": "https://example.com",
              "hostname": "example.com", "ipv4": "192.0.2.10", "byte": "dGVzdA==", "password": "Test-Password-1",
              "time": "09:00:00", "phone": "+919000000000"}
    if fmt in by_fmt:
        return by_fmt[fmt]
    n = name.lower()
    if "email" in n:
        return "qa.tester@example.com"
    if n.endswith("name"):
        return "TestAgent QA"
    v = "test"
    if isinstance(s.get("minLength"), int) and s["minLength"] > len(v):
        v = (v * (s["minLength"] // 4 + 1))[: s["minLength"]]
    if isinstance(s.get("maxLength"), int) and s["maxLength"] < len(v):
        v = v[: max(s["maxLength"], 1)]
    return v


# --------------------------------------------------------------------------- schema validation
def validate(schema: dict, value: Any, path: str = "$", errors: list[str] | None = None, depth: int = 0) -> list[str]:
    """Validates a JSON value against a (dereferenced) schema. Checks type, nullability, enum,
    required properties, object properties and array items. Lenient on everything else so that
    differences in style (formats, lengths, extra properties) do not produce false failures."""
    errors = errors if errors is not None else []
    if len(errors) >= 10 or depth > 12 or not isinstance(schema, dict) or not schema:
        return errors
    if isinstance(schema.get("allOf"), list):
        for part in schema["allOf"]:
            validate(part, value, path, errors, depth + 1)
    for k in ("oneOf", "anyOf"):
        if isinstance(schema.get(k), list) and schema[k]:
            if not any(not validate(part, value, path, [], depth + 1) for part in schema[k]):
                errors.append(f"{path}: does not match any of the allowed shapes ({k})")
            return errors
    types = _types(schema)
    if value is None:
        if schema.get("nullable") or "null" in types or not types:
            return errors
        errors.append(f"{path}: is null but must be {'/'.join(types)}")
        return errors
    if types:
        ok = any(_is_type(t, value) for t in types)
        if not ok:
            errors.append(f"{path}: expected {'/'.join(types)}, got {_jtype(value)}")
            return errors
    if isinstance(schema.get("enum"), list) and schema["enum"] and value not in schema["enum"]:
        errors.append(f"{path}: value {json.dumps(value)[:60]} is not one of {json.dumps(schema['enum'])[:120]}")
    if isinstance(value, dict):
        for r in schema.get("required") or []:
            if isinstance(r, str) and r not in value:
                errors.append(f"{path}: missing required property '{r}'")
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for k, sub in props.items():
            if k in value:
                validate(sub, value[k], f"{path}.{k}", errors, depth + 1)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value[:50]):
            validate(schema["items"], item, f"{path}[{i}]", errors, depth + 1)
    return errors


def _is_type(t: str, v: Any) -> bool:
    return {
        "object": isinstance(v, dict), "array": isinstance(v, list), "string": isinstance(v, str),
        "boolean": isinstance(v, bool), "null": v is None,
        "integer": isinstance(v, int) and not isinstance(v, bool) or (isinstance(v, float) and v.is_integer()),
        "number": isinstance(v, (int, float)) and not isinstance(v, bool),
    }.get(t, True)


def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def status_matches(expected: list, status: int) -> bool:
    for e in expected or ["2xx"]:
        if isinstance(e, int) and e == status:
            return True
        if isinstance(e, str):
            es = e.strip().lower()
            if es.isdigit() and int(es) == status:
                return True
            if len(es) == 3 and es.endswith("xx") and es[0].isdigit() and status // 100 == int(es[0]):
                return True
    return False


# --------------------------------------------------------------------------- operations
def _media(content: Any) -> tuple[str, dict]:
    """Pick the JSON media type (or the first one) from an OpenAPI 3 content map."""
    if not isinstance(content, dict) or not content:
        return "", {}
    for ct in content:
        if ct.split(";")[0].strip().lower() == "application/json" or ct.lower().endswith("+json"):
            return ct, content[ct] if isinstance(content[ct], dict) else {}
    for ct in content:
        if "x-www-form-urlencoded" in ct:
            return ct, content[ct] if isinstance(content[ct], dict) else {}
    ct = next(iter(content))
    return ct, content[ct] if isinstance(content[ct], dict) else {}


def operations(doc: dict) -> list[dict]:
    is3 = str(doc.get("openapi", "")).startswith("3")
    global_sec = doc.get("security")
    produces = doc.get("produces") or []
    consumes = doc.get("consumes") or []
    out: list[dict] = []
    for path, raw_item in doc["paths"].items():
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        item = _resolve(doc, raw_item)
        common = [_resolve(doc, p) for p in item.get("parameters") or []]
        for m in HTTP_METHODS:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            params: dict[tuple[str, str], dict] = {}
            for p in common + [_resolve(doc, p) for p in op.get("parameters") or []]:
                if isinstance(p.get("name"), str) and isinstance(p.get("in"), str):
                    params[(p["in"], p["name"])] = p
            body_schema, body_ct, body_required, body_example = None, "", False, None
            if is3 and isinstance(op.get("requestBody"), dict):
                rb = _resolve(doc, op["requestBody"])
                body_ct, media = _media(rb.get("content"))
                body_schema = media.get("schema")
                body_required = bool(rb.get("required"))
                if "example" in media:
                    body_example = media["example"]
                elif isinstance(media.get("examples"), dict) and media["examples"]:
                    ex = _resolve(doc, next(iter(media["examples"].values())))
                    body_example = ex.get("value")
            if not is3:
                for (where, _), p in list(params.items()):
                    if where == "body":
                        body_schema, body_required = p.get("schema"), bool(p.get("required"))
                        body_ct = (op.get("consumes") or consumes or ["application/json"])[0]
                forms = {n: p for (w, n), p in params.items() if w == "formData"}
                if forms and body_schema is None:
                    body_ct = "application/x-www-form-urlencoded"
                    body_schema = {"type": "object", "properties": {n: {k: v for k, v in p.items() if k not in ("in", "name")}
                                                                   for n, p in forms.items()},
                                   "required": [n for n, p in forms.items() if p.get("required")]}
                    body_required = any(p.get("required") for p in forms.values())
            responses: dict[str, dict] = {}
            for code, r in (op.get("responses") or {}).items():
                r = _resolve(doc, r)
                if is3:
                    ct, media = _media(r.get("content"))
                    responses[str(code)] = {"content_type": ct, "schema": media.get("schema")}
                else:
                    ct = (op.get("produces") or produces or ["application/json"])[0] if r.get("schema") else ""
                    responses[str(code)] = {"content_type": ct, "schema": r.get("schema")}
            sec = op.get("security", global_sec)
            secured = isinstance(sec, list) and bool(sec) and all(isinstance(x, dict) and x for x in sec)
            out.append({
                "method": m.upper(), "path": path, "operation_id": str(op.get("operationId") or "")[:120],
                "summary": str(op.get("summary") or op.get("description") or "")[:200],
                "params": params, "body_schema": body_schema, "body_ct": body_ct, "body_required": body_required,
                "body_example": body_example, "responses": responses, "secured": secured,
                "deprecated": bool(op.get("deprecated")),
            })
            if len(out) >= MAX_OPERATIONS:
                return out
    return out


# --------------------------------------------------------------------------- case generation
def _success(op: dict) -> tuple[list, dict]:
    codes = sorted(c for c in op["responses"] if c.isdigit() and c.startswith("2"))
    if codes:
        return [int(c) for c in codes], op["responses"][codes[0]]
    if "2XX" in op["responses"] or "2xx" in op["responses"]:
        return ["2xx"], op["responses"].get("2XX") or op["responses"].get("2xx") or {}
    return ["2xx"], op["responses"].get("default", {})


def _schema_for_case(doc: dict, schema: Any) -> dict | None:
    if not schema:
        return None
    d = deref(doc, schema)
    if not d or len(json.dumps(d, default=str)) > MAX_SCHEMA_JSON:
        return None
    return d


def _param_value(doc: dict, p: dict) -> Any:
    if "example" in p:
        return p["example"]
    if isinstance(p.get("examples"), dict) and p["examples"]:
        ex = _resolve(doc, next(iter(p["examples"].values())))
        if "value" in ex:
            return ex["value"]
    schema = p.get("schema") if isinstance(p.get("schema"), dict) else {k: v for k, v in p.items() if k in ("type", "format", "enum", "default", "minimum")}
    return sample(doc, schema, name=p.get("name", ""))


def _has_explicit_value(doc: dict, p: dict) -> bool:
    s = _resolve(doc, p.get("schema")) if isinstance(p.get("schema"), dict) else p
    return "example" in p or bool(p.get("examples")) or any(k in s for k in ("example", "default", "enum"))


def _label(method: str, path: str) -> str:
    return f"{method} {path}"


def _sort_key(c: dict) -> tuple:
    order = {"GET": 0, "HEAD": 1, "OPTIONS": 1, "POST": 2, "PUT": 3, "PATCH": 3, "DELETE": 5}
    cat = {"positive": 0, "performance": 1, "negative": 2, "auth": 3}
    d = c["definition"]
    return (order.get(d["method"], 4), d["path"].count("/"), d["path"].count("{"), cat.get(c["category"], 9), d["path"])


def _case(name: str, category: str, d: dict, *, operation: str = "", tags: list[str] | None = None) -> dict:
    d.setdefault("path_params", {})
    d.setdefault("path_samples", {})
    d.setdefault("query", {})
    d.setdefault("headers", {})
    d.setdefault("body", None)
    d.setdefault("body_type", None)
    d.setdefault("auth", "suite")
    d.setdefault("expect", {})
    d["operation"] = operation
    d["tags"] = tags or []
    return {"name": name[:300], "category": category, "definition": d, "is_write": d["method"] in WRITE_METHODS}


def generate_from_spec(doc: dict, *, has_auth: bool, max_ms: int) -> list[dict]:
    cases: list[dict] = []
    invalid_cred_done = False
    for op in operations(doc):
        method, path = op["method"], op["path"]
        lbl = _label(method, path)
        tags = ["deprecated"] if op["deprecated"] else []
        statuses, ok_resp = _success(op)
        path_params, path_samples, query, headers = {}, {}, {}, {}
        for (where, name), p in op["params"].items():
            if where == "path":
                path_params[name] = _param_value(doc, p) if _has_explicit_value(doc, p) else None
                path_samples[name] = _param_value(doc, p)
            elif where == "query" and p.get("required"):
                query[name] = _param_value(doc, p)
            elif where == "header" and p.get("required") and name.lower() not in SKIP_HEADERS:
                headers[name] = _param_value(doc, p)
        body, body_type = None, None
        if op["body_schema"] is not None or op["body_example"] is not None:
            body = op["body_example"] if op["body_example"] is not None else sample(doc, op["body_schema"])
            body_type = "form" if "x-www-form-urlencoded" in (op["body_ct"] or "") else "json"
        base = {"method": method, "path": path, "path_params": path_params, "path_samples": path_samples,
                "query": {k: _q(v) for k, v in query.items()}, "headers": {k: _q(v) for k, v in headers.items()},
                "body": body, "body_type": body_type}
        expect = {"status": statuses, "max_ms": max_ms}
        if ok_resp.get("content_type") and method != "HEAD":
            expect["content_type"] = ok_resp["content_type"].split(";")[0].strip()
        schema = _schema_for_case(doc, ok_resp.get("schema"))
        if schema and method != "HEAD":
            expect["schema"] = schema
        title = op["summary"] or op["operation_id"]
        cases.append(_case(f"{lbl} — {_status_txt(statuses)}" + (f" ({title})" if title else ""), "positive",
                           {**copy.deepcopy(base), "auth": "suite", "expect": expect}, operation=op["operation_id"], tags=tags))
        if op["secured"] and has_auth:
            cases.append(_case(f"{lbl} — rejects requests without credentials", "auth",
                               {**copy.deepcopy(base), "auth": "none", "expect": {"status": [401, 403]}},
                               operation=op["operation_id"], tags=tags))
            if not invalid_cred_done and method == "GET":
                invalid_cred_done = True
                cases.append(_case(f"{lbl} — rejects invalid credentials", "auth",
                                   {**copy.deepcopy(base), "auth": "invalid", "expect": {"status": [401, 403]}},
                                   operation=op["operation_id"], tags=tags))
        req_q = [n for (w, n), p in op["params"].items() if w == "query" and p.get("required")]
        if req_q:
            d = copy.deepcopy(base)
            d["query"].pop(req_q[0], None)
            cases.append(_case(f"{lbl} — missing required query parameter '{req_q[0]}' is rejected", "negative",
                               {**d, "expect": {"status": ["4xx"]}}, operation=op["operation_id"], tags=tags))
        for (w, n), p in op["params"].items():
            if w != "path":
                continue
            ps = _resolve(doc, p.get("schema")) if isinstance(p.get("schema"), dict) else p
            t = (_types(ps) or ["string"])[0]
            bad = "invalid-id-x" if t in ("integer", "number") else ("not-a-uuid" if ps.get("format") == "uuid" else None)
            if bad:
                d = copy.deepcopy(base)
                d["path_params"][n] = bad
                cases.append(_case(f"{lbl} — invalid path parameter '{n}' is rejected", "negative",
                                   {**d, "expect": {"status": ["4xx"]}}, operation=op["operation_id"], tags=tags))
                break
        bs = _resolve(doc, op["body_schema"]) if op["body_schema"] else {}
        if method in WRITE_METHODS and op["body_required"] and body_type == "json" and bs.get("required"):
            d = copy.deepcopy(base)
            d["body"] = {}
            cases.append(_case(f"{lbl} — empty body is rejected", "negative",
                               {**d, "expect": {"status": ["4xx"]}}, operation=op["operation_id"], tags=tags))
        if len(cases) >= MAX_CASES:
            break
    cases.append(_case("Unknown endpoint returns a client error", "negative",
                       {"method": "GET", "path": "/__testagent_not_found__", "expect": {"status": ["4xx"]}}))
    cases.sort(key=_sort_key)
    return cases[:MAX_CASES]


def _q(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return "" if v is None else str(v)


def _status_txt(statuses: list) -> str:
    return "returns " + " or ".join(str(s) for s in statuses)


# --------------------------------------------------------------------------- endpoint list (no spec)
LINE_RE = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)(.*)$", re.I)


def parse_endpoints(text: str) -> list[dict]:
    """One endpoint per line:  METHOD /path [JSON body] [-> 200,201]
    e.g.  GET /users   |   POST /users {"name": "QA"} -> 201   |   DELETE /users/{id} -> 204"""
    out, errors = [], []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            errors.append(f"line {n}: expected 'METHOD /path', e.g. 'GET /users'")
            continue
        method, path, rest = m.group(1).upper(), m.group(2), m.group(3).strip()
        if not path.startswith("/"):
            errors.append(f"line {n}: the path must start with /")
            continue
        statuses: list = ["2xx"]
        if "->" in rest:
            rest, st = rest.rsplit("->", 1)
            rest = rest.strip()
            try:
                statuses = [int(x) for x in re.split(r"[,\s]+", st.strip()) if x]
            except ValueError:
                errors.append(f"line {n}: the expected status after -> must be numbers, e.g. -> 200,201")
                continue
        body = None
        if rest:
            try:
                body = json.loads(rest)
            except ValueError:
                errors.append(f"line {n}: the request body must be valid JSON")
                continue
        out.append({"method": method, "path": path.split("?")[0], "query": _query(path), "body": body, "status": statuses})
        if len(out) >= MAX_OPERATIONS:
            break
    if errors:
        raise SpecError("; ".join(errors[:8]))
    if not out:
        raise SpecError("enter at least one endpoint, e.g. GET /health")
    return out


def _query(path: str) -> dict:
    if "?" not in path:
        return {}
    from urllib.parse import parse_qsl

    return dict(parse_qsl(path.split("?", 1)[1]))


def generate_from_endpoints(endpoints: list[dict], *, has_auth: bool, requires_auth: bool, max_ms: int) -> list[dict]:
    cases: list[dict] = []
    invalid_done = False
    for e in endpoints:
        method, path = e["method"], e["path"]
        params = re.findall(r"\{([^}/]+)\}", path)
        base = {"method": method, "path": path, "path_params": {p: None for p in params},
                "path_samples": {p: 1 for p in params}, "query": e.get("query") or {}, "headers": {},
                "body": e.get("body"), "body_type": "json" if e.get("body") is not None else None}
        cases.append(_case(f"{method} {path} — {_status_txt(e['status'])}", "positive",
                           {**copy.deepcopy(base), "auth": "suite", "expect": {"status": e["status"], "max_ms": max_ms}}))
        if has_auth and requires_auth:
            cases.append(_case(f"{method} {path} — rejects requests without credentials", "auth",
                               {**copy.deepcopy(base), "auth": "none", "expect": {"status": [401, 403]}}))
            if not invalid_done and method == "GET":
                invalid_done = True
                cases.append(_case(f"{method} {path} — rejects invalid credentials", "auth",
                                   {**copy.deepcopy(base), "auth": "invalid", "expect": {"status": [401, 403]}}))
    cases.append(_case("Unknown endpoint returns a client error", "negative",
                       {"method": "GET", "path": "/__testagent_not_found__", "expect": {"status": ["4xx"]}}))
    cases.sort(key=_sort_key)
    return cases


# --------------------------------------------------------------------------- validation of edited cases
def clean_definition(d: Any) -> dict:
    """Validates a case definition coming from the UI or from Claude. Raises SpecError."""
    if not isinstance(d, dict):
        raise SpecError("the case definition must be a JSON object")
    method = str(d.get("method", "")).upper()
    if method not in {m.upper() for m in HTTP_METHODS}:
        raise SpecError(f"unsupported method '{d.get('method')}'")
    path = str(d.get("path", ""))
    if not path.startswith("/") or len(path) > 2000 or any(c in path for c in "\r\n\t\x00 ") or "://" in path:
        raise SpecError("the path must be a relative path starting with / (no host, no spaces)")
    auth = d.get("auth", "suite")
    if auth not in ("suite", "none", "invalid"):
        raise SpecError("auth must be suite, none or invalid")

    def strmap(v: Any, what: str) -> dict:
        if v in (None, ""):
            return {}
        if not isinstance(v, dict) or len(v) > 50:
            raise SpecError(f"{what} must be an object with at most 50 entries")
        out = {}
        for k, val in v.items():
            if not isinstance(k, str) or not re.match(r"^[A-Za-z0-9_.\-\[\]]{1,100}$", k):
                raise SpecError(f"invalid {what} name '{k}'")
            out[k] = _q(val)
        return out

    headers = strmap(d.get("headers"), "headers")
    if any(k.lower() in ("host", "content-length", "transfer-encoding", "connection") for k in headers):
        raise SpecError("Host, Content-Length, Transfer-Encoding and Connection headers are set automatically")
    pp = d.get("path_params") or {}
    if not isinstance(pp, dict):
        raise SpecError("path_params must be an object")
    body_type = d.get("body_type")
    if body_type not in (None, "json", "form", "text"):
        raise SpecError("body_type must be json, form or text")
    body = d.get("body")
    if len(json.dumps(body, default=str)) > 200_000:
        raise SpecError("the request body is larger than 200 KB")
    exp = d.get("expect") or {}
    if not isinstance(exp, dict):
        raise SpecError("expect must be an object")
    statuses = exp.get("status") or ["2xx"]
    if not isinstance(statuses, list) or not statuses or not all(
            (isinstance(s, int) and 100 <= s <= 599) or (isinstance(s, str) and re.match(r"^[1-5](xx|\d\d)$", s.lower()))
            for s in statuses):
        raise SpecError("expect.status must be a list like [200] or [\"2xx\"]")
    clean_exp: dict = {"status": [s.lower() if isinstance(s, str) else s for s in statuses]}
    if exp.get("max_ms") not in (None, ""):
        try:
            clean_exp["max_ms"] = max(1, min(int(exp["max_ms"]), 120_000))
        except (TypeError, ValueError):
            raise SpecError("expect.max_ms must be a number of milliseconds")
    for k in ("content_type", "body_contains"):
        if exp.get(k):
            clean_exp[k] = str(exp[k])[:500]
    if isinstance(exp.get("schema"), dict) and len(json.dumps(exp["schema"], default=str)) <= MAX_SCHEMA_JSON:
        clean_exp["schema"] = exp["schema"]
    if isinstance(exp.get("json_equals"), dict):
        clean_exp["json_equals"] = {str(k)[:200]: v for k, v in list(exp["json_equals"].items())[:20]}
    return {"method": method, "path": path,
            "path_params": {str(k)[:100]: (None if v is None else _q(v)) for k, v in list(pp.items())[:20]},
            "path_samples": {str(k)[:100]: _q(v) for k, v in list((d.get("path_samples") or {}).items())[:20]},
            "query": strmap(d.get("query"), "query"), "headers": headers, "body": body, "body_type": body_type,
            "auth": auth, "expect": clean_exp, "operation": str(d.get("operation", ""))[:120],
            "tags": [str(t)[:40] for t in (d.get("tags") or [])][:10]}
