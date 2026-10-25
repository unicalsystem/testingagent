#!/usr/bin/env bash
# Builds and (re)deploys the current checkout. Safe to re-run.
#   bash deploy/scripts/deploy.sh                 # deploy what is checked out
#   WITH_SAMPLE=1 bash deploy/scripts/deploy.sh   # also run the internal sample app
# Steps: preflight -> build images tagged with the git commit -> backup (if data exists)
#        -> migrate -> start -> health check. Never deletes volumes.
source "$(dirname "$0")/common.sh"
require_files

command -v docker >/dev/null || die "docker is not installed (run bootstrap-vps.sh)"
if [[ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
  warn "working tree has uncommitted changes; the image tag will be marked -dirty"
  DIRTY="-dirty"
fi
TAG="$(git rev-parse --short=12 HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)${DIRTY:-}"
PREV="$(current_tag)"
export IMAGE_TAG="$TAG"
PROFILES=(); EXTRA=()
if [[ "${WITH_SAMPLE:-0}" == "1" ]]; then PROFILES=(--profile sample); EXTRA=(sample-app); fi

log "release $TAG (previous: ${PREV:-none})"
log "validating compose configuration"
"${COMPOSE[@]}" "${PROFILES[@]}" config --quiet

log "building images"
"${COMPOSE[@]}" "${PROFILES[@]}" build --pull api web "${EXTRA[@]}"
docker tag "testpilot-backend:$TAG" testpilot-backend:latest
docker tag "testpilot-web:$TAG" testpilot-web:latest

log "checking application configuration"
"${COMPOSE[@]}" run --rm --no-deps api python -m testpilot.cli check-config

log "starting database"
"${COMPOSE[@]}" up -d db
for i in $(seq 1 30); do
  "${COMPOSE[@]}" exec -T db pg_isready -q && break; sleep 2
  [[ $i == 30 ]] && die "database did not become ready"
done

set -a; . ./.env; set +a
HAS_DATA=$("${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT to_regclass('public.alembic_version') IS NOT NULL" 2>/dev/null || echo f)
if [[ "$HAS_DATA" == "t" && "${SKIP_BACKUP:-0}" != "1" ]]; then
  log "existing data found: taking a backup before migrating"
  bash "$(dirname "$0")/backup.sh" >/dev/null
fi

log "running database migrations"
"${COMPOSE[@]}" --profile tools run --rm migrate

log "starting services"
"${COMPOSE[@]}" "${PROFILES[@]}" up -d --remove-orphans api worker web "${EXTRA[@]}"

if bash "$(dirname "$0")/health.sh"; then
  [[ -n "$PREV" && "$PREV" != "$TAG" ]] && echo "$PREV" > "$STATE_DIR/previous"
  echo "$TAG" > "$STATE_DIR/current"
  echo "$(date -u +%FT%TZ) $TAG" >> "$STATE_DIR/history"
  log "deployed $TAG"
else
  warn "health check failed for $TAG"
  [[ -n "$PREV" ]] && warn "roll back with: bash deploy/scripts/rollback.sh   (returns to $PREV)"
  exit 1
fi
