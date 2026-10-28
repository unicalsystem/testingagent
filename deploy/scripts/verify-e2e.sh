#!/usr/bin/env bash
# End-to-end verification on the server against the bundled sample app:
# seeds the sample project, runs its three test cases through the real worker, and checks
# results, screenshots and reports. Requires: deploy with WITH_SAMPLE=1.
#   bash deploy/scripts/verify-e2e.sh
source "$(dirname "$0")/common.sh"
"${COMPOSE[@]}" ps --status running sample-app | grep -q sample-app || die "sample-app is not running (deploy with WITH_SAMPLE=1)"
"${COMPOSE[@]}" exec -T api python -m testpilot.cli seed-sample --url http://sample-app:8080
"${COMPOSE[@]}" exec -T api python - <<'PY'
import sys, time
from sqlalchemy import select
from testpilot import storage
from testpilot.db import session_scope
from testpilot.models import Evidence, Project, Run, RunStep, TestCase
from testpilot.steps import describe

with session_scope() as db:
    from testpilot.config import get_settings
    from testpilot.models import Tenant
    t = db.scalars(select(Tenant).where(Tenant.slug == get_settings().default_tenant_slug)).one()
    p = db.scalars(select(Project).where(Project.tenant_id == t.id, Project.name == "Sample App (Acme Tasks)")).one()
    ids = []
    for c in db.scalars(select(TestCase).where(TestCase.project_id == p.id, TestCase.archived.is_(False))).all():
        r = Run(tenant_id=p.tenant_id, project_id=p.id, test_case_id=c.id, status="queued",
                case_snapshot={"name": c.name, "description": c.description, "steps": c.steps})
        db.add(r); db.flush()
        for i, s in enumerate(c.steps):
            db.add(RunStep(tenant_id=p.tenant_id, run_id=r.id, idx=i, action=s["action"], description=describe(s)[:500]))
        ids.append(r.id)
print("queued runs", ids, flush=True)
deadline = time.time() + 300
while time.time() < deadline:
    with session_scope() as db:
        runs = [db.get(Run, i) for i in ids]
        if all(r.status not in ("queued", "running") for r in runs):
            break
    time.sleep(3)
ok = True
with session_scope() as db:
    for i in ids:
        r = db.get(Run, i)
        ev = db.scalars(select(Evidence).where(Evidence.run_id == i)).all()
        kinds = sorted({e.kind for e in ev})
        missing = [e.rel_path for e in ev if not storage.resolve_evidence(e.rel_path).is_file()]
        good = r.status == "passed" and "screenshot" in kinds and "report_html" in kinds and not missing
        ok &= good
        print(f"run {i}: {r.status:9} {r.case_snapshot['name']:<34} evidence={kinds} {'OK' if good else 'PROBLEM ' + (r.error or str(missing))}")
sys.exit(0 if ok else 1)
PY
set -a; . ./.env; set +a
log "end-to-end verification passed. Open $PUBLIC_URL/${DEFAULT_TENANT_SLUG:-unical}/projects to see the runs and download reports."
