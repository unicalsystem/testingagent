"""Reports for API test runs and uptime monitors (release 1.6).

Plain data in, self-contained HTML / canonical JSON out, so reports can be produced by the API on
demand and also tested without a database. The JSON report carries a SHA-256 fingerprint of its
own canonical content (the "fingerprint" field is excluded from the hash).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from html import escape
from typing import Any

CSS = """
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1d2433;margin:0;background:#f5f7fb}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:#667085}.small{font-size:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.card{background:#fff;border:1px solid #e3e8f0;border-radius:10px;padding:16px;margin-bottom:14px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}
.stat{background:#fff;border:1px solid #e3e8f0;border-radius:10px;padding:12px}.stat .k{font-size:12px;color:#667085}.stat .v{font-size:22px;font-weight:600}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8f0;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eef1f6;vertical-align:top}th{background:#f8fafc;font-size:12px;color:#475467;font-weight:600}
.b{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.passed,.up{background:#e6f6ee;color:#0f7a43}.failed,.down{background:#fdecec;color:#b42318}.error{background:#fff4e5;color:#b54708}
.skipped,.unknown,.paused,.cancelled{background:#eef1f6;color:#475467}.running,.queued{background:#e8effd;color:#2a63d4}
details{background:#fff;border:1px solid #e3e8f0;border-radius:10px;margin-bottom:8px}summary{cursor:pointer;padding:10px 12px;list-style:none}
summary::-webkit-details-marker{display:none}.inner{padding:0 12px 12px}.a-ok{color:#0f7a43}.a-bad{color:#b42318}
pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:320px;overflow:auto}
.bar{height:8px;border-radius:99px;background:#fdecec;overflow:hidden}.bar i{display:block;height:100%;background:#12b76a}
footer{margin-top:30px;font-size:12px;color:#667085}
@media print{body{background:#fff}details{break-inside:avoid}details>*{display:block}}
"""


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat(timespec="seconds")
    return "" if v is None else str(v)


def _fmt(v: Any) -> str:
    if isinstance(v, str) and v:
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%d %b %Y %H:%M:%S UTC")
    return "—"


def _dur(seconds: float | int | None) -> str:
    if not seconds:
        return "0 s"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d} d" if d else "", f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s and not d else ""]
    return " ".join(p for p in parts if p) or "0 s"


def canonical(data: dict) -> bytes:
    body = {k: v for k, v in data.items() if k != "fingerprint"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_iso).encode()


def fingerprint(data: dict) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def to_json(data: dict) -> bytes:
    out = dict(data)
    out["fingerprint"] = {"algorithm": "sha256", "value": fingerprint(data)}
    return json.dumps(out, indent=2, ensure_ascii=False, default=_iso).encode()


# --------------------------------------------------------------------------- API test run
def run_html(data: dict) -> bytes:
    run, s, results = data["run"], data["summary"], data["results"]
    suite = run.get("suite", {})
    e = escape
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>API test report #{run['id']} · {e(suite.get('name', ''))}</title><style>{CSS}</style></head><body><div class='wrap'>"]
    out.append(f"<div class='muted small'>TestAgent · API test report · {e(run.get('organization', ''))}</div>"
               f"<h1>{e(suite.get('name', 'API suite'))} — run #{run['id']} <span class='b {e(run['status'])}'>{e(run['status'])}</span></h1>"
               f"<div class='mono small muted'>{e(suite.get('base_url', ''))}</div>")
    if run.get("error"):
        out.append(f"<div class='card error'><b>Run error:</b> {e(run['error'])}</div>")
    stats = [("Test cases", s.get("total", 0)), ("Passed", s.get("passed", 0)), ("Failed", s.get("failed", 0)),
             ("Errors", s.get("error", 0)), ("Skipped", s.get("skipped", 0)), ("Pass rate", f"{s.get('pass_rate', 0)}%"),
             ("Endpoints", s.get("endpoints", 0)), ("Avg response", f"{s.get('avg_ms', 0)} ms"), ("p95 response", f"{s.get('p95_ms', 0)} ms")]
    out.append("<div class='stats'>" + "".join(f"<div class='stat'><div class='k'>{k}</div><div class='v'>{e(str(v))}</div></div>" for k, v in stats) + "</div>")
    meta = [("Project", run.get("project", {}).get("name", "")), ("Specification", suite.get("spec_title") or suite.get("spec_source", "")),
            ("Authentication", suite.get("auth_type", "none")), ("Write tests", "included" if run.get("options", {}).get("include_writes") else "skipped (read-only run)"),
            ("Requested by", run.get("requested_by", "")), ("Started", _fmt(run.get("started_at"))), ("Finished", _fmt(run.get("finished_at")))]
    out.append("<table><tbody>" + "".join(f"<tr><th style='width:200px'>{k}</th><td>{e(str(v or '—'))}</td></tr>" for k, v in meta) + "</tbody></table>")

    if s.get("by_category"):
        out.append("<h2>By category</h2><table><thead><tr><th>Category</th><th>Total</th><th>Passed</th><th>Failed</th><th>Errors</th><th>Skipped</th></tr></thead><tbody>")
        for c, v in s["by_category"].items():
            out.append(f"<tr><td>{e(c)}</td><td>{v.get('total', 0)}</td><td>{v.get('passed', 0)}</td><td>{v.get('failed', 0)}</td><td>{v.get('error', 0)}</td><td>{v.get('skipped', 0)}</td></tr>")
        out.append("</tbody></table>")

    eps: dict[tuple, dict] = {}
    for r in results:
        k = (r.get("method", ""), r.get("path", ""))
        x = eps.setdefault(k, {"n": 0, "passed": 0, "failed": 0, "ms": []})
        x["n"] += 1
        if r["status"] == "passed":
            x["passed"] += 1
        elif r["status"] in ("failed", "error"):
            x["failed"] += 1
        if r.get("duration_ms"):
            x["ms"].append(r["duration_ms"])
    out.append("<h2>Endpoints</h2><table><thead><tr><th>Endpoint</th><th>Cases</th><th>Passed</th><th>Failed / error</th><th>Avg time</th></tr></thead><tbody>")
    for (m, p), x in sorted(eps.items(), key=lambda kv: kv[0][1]):
        avg = f"{int(sum(x['ms']) / len(x['ms']))} ms" if x["ms"] else "—"
        out.append(f"<tr><td class='mono'>{e(m)} {e(p)}</td><td>{x['n']}</td><td>{x['passed']}</td><td>{x['failed']}</td><td>{avg}</td></tr>")
    out.append("</tbody></table>")

    order = {"failed": 0, "error": 1, "passed": 2, "skipped": 3}
    out.append("<h2>Results</h2><p class='muted small'>Failures first. Credentials are masked in every excerpt.</p>")
    for r in sorted(results, key=lambda r: (order.get(r["status"], 9), r.get("idx", 0))):
        out.append(f"<details{' open' if r['status'] in ('failed', 'error') else ''}><summary><span class='b {e(r['status'])}'>{e(r['status'])}</span> "
                   f"<b>{e(r.get('name', ''))}</b> <span class='muted small'>· {e(r.get('category', ''))} · "
                   f"{e(str(r.get('http_status') or '—'))} · {r.get('duration_ms', 0)} ms</span></summary><div class='inner'>")
        out.append(f"<div class='mono small'>{e(r.get('method', ''))} {e(r.get('url', ''))}</div>")
        if r.get("error"):
            out.append(f"<p class='a-bad'><b>{'Skipped' if r['status'] == 'skipped' else 'Error'}:</b> {e(r['error'])}</p>")
        if r.get("assertions"):
            out.append("<ul>" + "".join(
                f"<li class='{'a-ok' if a.get('ok') else 'a-bad'}'>{'✔' if a.get('ok') else '✘'} {e(a.get('name', ''))}"
                f"{(' — <span class=muted>' + e(str(a.get('detail', ''))) + '</span>') if a.get('detail') else ''}</li>"
                for a in r["assertions"]) + "</ul>")
        req, resp = r.get("request") or {}, r.get("response") or {}
        if req.get("headers") or req.get("body"):
            out.append("<div class='small muted'>Request</div><pre>" + e("\n".join(f"{k}: {v}" for k, v in (req.get("headers") or {}).items())
                       + ("\n\n" + req["body"] if req.get("body") else "")) + "</pre>")
        if resp:
            out.append("<div class='small muted'>Response</div><pre>" + e("\n".join(f"{k}: {v}" for k, v in (resp.get("headers") or {}).items())
                       + ("\n\n" + resp.get("body", "") if resp.get("body") else "")) + "</pre>")
        out.append("</div></details>")
    out.append(f"<footer>Generated by TestAgent (Unical Systems) on {_fmt(data.get('generated_at'))}. "
               f"SHA-256 of the JSON report: <span class='mono'>{e(fingerprint(data))}</span></footer></div></body></html>")
    return "".join(out).encode()


# --------------------------------------------------------------------------- uptime / downtime report
def monitor_html(data: dict) -> bytes:
    m, st, days, incidents = data["monitor"], data["stats"], data["daily"], data["incidents"]
    e = escape
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Uptime report · {e(m['name'])}</title><style>{CSS}</style></head><body><div class='wrap'>"]
    out.append(f"<div class='muted small'>TestAgent · Uptime &amp; downtime report · {e(data.get('organization', ''))}</div>"
               f"<h1>{e(m['name'])} <span class='b {e(m['status'])}'>{e(m['status'])}</span></h1>"
               f"<div class='mono small muted'>{e(m.get('method', 'GET'))} {e(m['url'])}</div>"
               f"<p class='muted small'>Period: {_fmt(data['period_start'])} – {_fmt(data['period_end'])} ({data['days']} days). "
               f"Checked every {m.get('interval_seconds', 0) // 60 or 1} min; down after {m.get('failure_threshold', 1)} consecutive failed check(s).</p>")
    stats = [("Uptime", f"{st['uptime_pct']}%"), ("Downtime", _dur(st["downtime_seconds"])), ("Incidents", st["incidents"]),
             ("Mean time to recover", _dur(st["mttr_seconds"])), ("Checks", st["checks"]), ("Failed checks", st["failed_checks"]),
             ("Avg response", f"{st['avg_ms']} ms"), ("p95 response", f"{st['p95_ms']} ms")]
    out.append("<div class='stats'>" + "".join(f"<div class='stat'><div class='k'>{k}</div><div class='v'>{e(str(v))}</div></div>" for k, v in stats) + "</div>")
    out.append("<h2>Incidents</h2>")
    if incidents:
        out.append("<table><thead><tr><th>Started</th><th>Recovered</th><th>Duration</th><th>Cause</th></tr></thead><tbody>")
        for i in incidents:
            out.append(f"<tr><td>{_fmt(i['started_at'])}</td><td>{_fmt(i['ended_at']) if i.get('ended_at') else '<b>ongoing</b>'}</td>"
                       f"<td>{_dur(i['duration_seconds'])}</td><td>{e(i.get('cause', ''))}</td></tr>")
        out.append("</tbody></table>")
    else:
        out.append("<div class='card'>No downtime in this period.</div>")
    out.append("<h2>Daily availability</h2><table><thead><tr><th>Date (UTC)</th><th>Uptime</th><th></th><th>Downtime</th><th>Checks</th><th>Avg response</th></tr></thead><tbody>")
    for d in days:
        pct = d["uptime_pct"]
        bar = f"<div class='bar'><i style='width:{pct if pct is not None else 0}%'></i></div>" if pct is not None else ""
        out.append(f"<tr><td>{e(d['date'])}</td><td>{'—' if pct is None else f'{pct}%'}</td><td style='width:30%'>{bar}</td>"
                   f"<td>{_dur(d['downtime_seconds'])}</td><td>{d['checks']}</td><td>{d['avg_ms'] or '—'}{' ms' if d['avg_ms'] else ''}</td></tr>")
    out.append("</tbody></table>")
    out.append(f"<footer>Generated by TestAgent (Unical Systems) on {_fmt(data.get('generated_at'))}. Uptime is measured from TestAgent's "
               f"checks; downtime is the time between the first failed check of an incident and the first successful check after it.</footer>"
               f"</div></body></html>")
    return "".join(out).encode()
