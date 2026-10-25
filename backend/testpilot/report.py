"""Test run reports (self-contained HTML and JSON) built from the database and stored evidence."""
from __future__ import annotations

import base64
import html
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import storage
from .models import Evidence, Project, Run, User

MAX_EMBED_BYTES = 25 * 1024 * 1024


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def build_data(db: Session, run: Run) -> dict:
    project = db.get(Project, run.project_id)
    requester = db.get(User, run.requested_by) if run.requested_by else None
    evidence = db.scalars(select(Evidence).where(Evidence.run_id == run.id).order_by(Evidence.id)).all()
    by_step: dict[int, list[Evidence]] = {}
    for e in evidence:
        if e.step_id:
            by_step.setdefault(e.step_id, []).append(e)
    dur = None
    if run.started_at and run.finished_at:
        dur = int((run.finished_at - run.started_at).total_seconds() * 1000)
    return {
        "report_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "run": {
            "id": run.id,
            "status": run.status,
            "error": run.error,
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "duration_ms": dur,
            "requested_by": requester.username if requester else None,
            "worker": run.worker_id,
            "summary": run.summary,
            "ai_analysis": run.ai_analysis or None,
        },
        "project": {"id": project.id, "name": project.name, "base_url": project.base_url} if project else None,
        "test_case": {
            "id": run.test_case_id,
            "name": run.case_snapshot.get("name"),
            "description": run.case_snapshot.get("description", ""),
        },
        "steps": [
            {
                "index": s.idx + 1,
                "id": s.id,
                "action": s.action,
                "description": s.description,
                "status": s.status,
                "attempts": s.attempts,
                "duration_ms": s.duration_ms,
                "error": s.error,
                "evidence": [{"id": e.id, "kind": e.kind, "sha256": e.sha256, "size": e.size} for e in by_step.get(s.id, [])],
            }
            for s in run.steps
        ],
        "evidence": [
            {"id": e.id, "kind": e.kind, "step_id": e.step_id, "file": e.rel_path.rsplit("/", 1)[-1],
             "mime": e.mime, "size": e.size, "sha256": e.sha256, "created_at": _iso(e.created_at)}
            for e in evidence
        ],
    }


def to_json(data: dict) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False).encode()


_CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#17202a;margin:0;background:#f5f6f8}
.wrap{max-width:1100px;margin:0 auto;padding:32px 24px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 10px}
.muted{color:#5d6b7a}.card{background:#fff;border:1px solid #dde2e8;border-radius:8px;padding:16px 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.k{font-size:12px;color:#5d6b7a;text-transform:uppercase;letter-spacing:.04em}.v{font-weight:600}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;text-transform:uppercase}
.passed{background:#e3f6ea;color:#11683a}.failed,.error{background:#fde8e8;color:#a3201c}
.cancelled,.skipped,.pending{background:#eceff3;color:#46525e}.running,.queued{background:#e6effd;color:#1f4fa8}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dde2e8;border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #eef1f4;vertical-align:top}
th{background:#f0f2f5;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#46525e}
pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;color:#7a1a16}
img.shot{max-width:320px;border:1px solid #dde2e8;border-radius:4px;display:block;margin-top:6px}
code{font:12px ui-monospace,Menlo,Consolas,monospace}.ai{white-space:pre-wrap}
footer{margin-top:32px;font-size:12px;color:#5d6b7a}
@media print{body{background:#fff}.card,table{border-color:#ccc}}
"""


def to_html(db: Session, data: dict) -> bytes:
    e = html.escape
    run, proj, case = data["run"], data["project"] or {}, data["test_case"]
    s = run.get("summary") or {}
    ev_rows = {x["id"]: x for x in data["evidence"]}
    embedded = 0
    rows = []
    for st in data["steps"]:
        imgs = ""
        for ref in st["evidence"]:
            if ref["kind"] != "screenshot":
                continue
            ev = db.get(Evidence, ref["id"])
            try:
                p = storage.resolve_evidence(ev.rel_path)
                if embedded + p.stat().st_size <= MAX_EMBED_BYTES:
                    b = p.read_bytes()
                    embedded += len(b)
                    imgs += f'<img class="shot" alt="Step {st["index"]} screenshot" src="data:{e(ev.mime)};base64,{base64.b64encode(b).decode()}">'
                else:
                    imgs += f'<div class="muted">Screenshot {e(p.name)} not embedded (size limit); see evidence list.</div>'
            except (OSError, PermissionError):
                imgs += '<div class="muted">Screenshot file missing</div>'
        err = f"<pre>{e(st['error'])}</pre>" if st["error"] else ""
        rows.append(
            f"<tr><td>{st['index']}</td><td><code>{e(st['action'])}</code></td><td>{e(st['description'])}{err}{imgs}</td>"
            f"<td><span class='badge {e(st['status'])}'>{e(st['status'])}</span></td><td>{st['attempts']}</td>"
            f"<td>{st['duration_ms']} ms</td></tr>"
        )
    ev_list = "".join(
        f"<tr><td>{x['id']}</td><td>{e(x['kind'])}</td><td>{e(x['file'])}</td><td>{x['size']:,} B</td><td><code>{e(x['sha256'])}</code></td></tr>"
        for x in ev_rows.values()
    )
    ai = f"<h2>AI failure analysis</h2><div class='card ai'>{e(run['ai_analysis'])}</div>" if run.get("ai_analysis") else ""
    run_err = f"<h2>Run error</h2><div class='card'><pre>{e(run['error'])}</pre></div>" if run.get("error") else ""

    def kv(k, v):
        return f"<div><div class='k'>{e(k)}</div><div class='v'>{e(str(v)) if v is not None else '—'}</div></div>"

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TestPilot AI report — run #{run['id']}</title><style>{_CSS}</style></head><body><div class="wrap">
<div class="muted">TestPilot AI · Test execution report</div>
<h1>{e(case.get('name') or 'Test case')} <span class="badge {e(run['status'])}">{e(run['status'])}</span></h1>
<div class="muted">{e(proj.get('name', ''))} · {e(proj.get('base_url', ''))}</div>
<h2>Summary</h2><div class="card grid">
{kv('Run ID', run['id'])}{kv('Requested by', run['requested_by'])}{kv('Started (UTC)', run['started_at'])}
{kv('Finished (UTC)', run['finished_at'])}{kv('Duration', f"{run['duration_ms']} ms" if run['duration_ms'] is not None else None)}
{kv('Steps passed', s.get('passed', 0))}{kv('Steps failed', s.get('failed', 0))}{kv('Steps skipped', s.get('skipped', 0))}
</div>
{('<p>' + e(case['description']) + '</p>') if case.get('description') else ''}
{run_err}
<h2>Steps</h2><table><thead><tr><th>#</th><th>Action</th><th>Description / evidence</th><th>Status</th><th>Attempts</th><th>Duration</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{ai}
<h2>Evidence manifest</h2><table><thead><tr><th>ID</th><th>Kind</th><th>File</th><th>Size</th><th>SHA-256</th></tr></thead><tbody>{ev_list}</tbody></table>
<footer>Generated {e(data['generated_at'])} by TestPilot AI. Evidence files are held in private storage and can be downloaded by authorised users; hashes above identify the exact files.</footer>
</div></body></html>"""
    return doc.encode()
