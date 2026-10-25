#!/usr/bin/env bash
# Health check of every component. Exit 0 only when all required checks pass.
#   bash deploy/scripts/health.sh
source "$(dirname "$0")/common.sh"
set -a; . ./.env; set +a
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
note() { printf '  \033[33mINFO\033[0m %s\n' "$*"; }

wait_healthy() {  # service, seconds
  for _ in $(seq 1 "$2"); do
    s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "testpilot-$1-1" 2>/dev/null || echo missing)
    [[ "$s" == healthy ]] && return 0; sleep 1
  done; return 1
}
echo "TestPilot AI health ($(date -u +%FT%TZ))"
for svc in db api worker web; do
  if wait_healthy "$svc" 90; then pass "container $svc healthy"; else fail "container $svc not healthy ($(docker inspect -f '{{.State.Status}}' testpilot-$svc-1 2>/dev/null || echo missing))"; fi
done

PORT="${WEB_PORT:-8081}"
DOMAIN="${PUBLIC_URL#https://}"; DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN%%/*}"
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null && pass "web serves on 127.0.0.1:$PORT" || fail "web not answering on 127.0.0.1:$PORT"
R=$(curl -fsS -H "Host: $DOMAIN" "http://127.0.0.1:$PORT/api/ready" || true)
[[ "$R" == *'"ok"'* ]] && pass "API ready (database reachable, migrated)" || fail "API not ready: ${R:-no response}"

STATUS=$("${COMPOSE[@]}" exec -T api python - <<'PY' 2>/dev/null || true
import json
from datetime import timedelta
from sqlalchemy import select, func
from testpilot.db import session_scope
from testpilot.models import WorkerHeartbeat, utcnow
from testpilot import ai, report_store
with session_scope() as db:
    w = db.scalar(select(func.count()).select_from(WorkerHeartbeat).where(WorkerHeartbeat.last_seen > utcnow() - timedelta(seconds=90)))
print(json.dumps({"workers": w, "ai": ai.status(), "mysql": report_store.check()}))
PY
)
if [[ -n "$STATUS" ]]; then
  W=$(jq -r .workers <<<"$STATUS"); [[ "$W" -ge 1 ]] && pass "$W worker(s) online" || fail "no worker heartbeat in the last 90 s"
  [[ $(jq -r .ai.configured <<<"$STATUS") == true ]] && pass "Claude API key configured (model $(jq -r .ai.model <<<"$STATUS"))" \
    || note "Claude API key not set: AI features disabled, everything else works"
  if [[ $(jq -r .mysql.enabled <<<"$STATUS") == true ]]; then
    [[ $(jq -r .mysql.ok <<<"$STATUS") == true ]] && pass "MySQL report store reachable ($(jq -r .mysql.host <<<"$STATUS"))" \
      || fail "MySQL report store: $(jq -r .mysql.error <<<"$STATUS")"
  else note "MySQL report store disabled (config.local)"; fi
else
  fail "could not query application status from the api container"
fi

if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '(^|[^0-9.])(0\.0\.0\.0|\*|\[::\]):5432$'; then
  fail "PostgreSQL port 5432 is listening publicly"
else pass "PostgreSQL not exposed on the host"; fi

PUB=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "https://$DOMAIN/api/health" 2>/dev/null || true)
if [[ "$PUB" == 200 ]]; then pass "public URL https://$DOMAIN reachable over HTTPS"
else note "public URL not reachable yet (HTTP ${PUB:-none}); expected until DNS and TLS are set up"; fi
exit $FAIL
