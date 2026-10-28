#!/usr/bin/env bash
# Switches the running containers back to the previously deployed image tag.
#   bash deploy/scripts/rollback.sh            # to the previous release
#   bash deploy/scripts/rollback.sh <tag>      # to a specific release (see .deploy/history)
# Database migrations are NOT reverted automatically.
# EXCEPTION — release 1.1 (migration 0003, multi-tenancy) is NOT backward compatible: release 1.0
# cannot run on the 1.1 schema. To go back from 1.1 to 1.0, first revert the schema with the 1.1
# image while it is still the current one (works while only the default organization exists):
#   docker compose -f docker-compose.prod.yml --profile tools run --rm migrate alembic downgrade 0002
# then run this script. If customer organizations already exist, restore the backup deploy.sh took
# before migrating instead (restore.sh) — that discards everything created since the upgrade.
source "$(dirname "$0")/common.sh"
require_files
TARGET="${1:-$(cat "$STATE_DIR/previous" 2>/dev/null || true)}"
[[ -n "$TARGET" ]] || die "no previous release recorded; pass a tag (see .deploy/history)"
docker image inspect "testpilot-backend:$TARGET" >/dev/null 2>&1 || die "image testpilot-backend:$TARGET not found locally"
docker image inspect "testpilot-web:$TARGET" >/dev/null 2>&1 || die "image testpilot-web:$TARGET not found locally"
CUR="$(current_tag)"
confirm "Roll back from ${CUR:-unknown} to $TARGET?" || die "aborted"
export IMAGE_TAG="$TARGET"
"${COMPOSE[@]}" up -d --no-build api worker web
bash "$(dirname "$0")/health.sh" || die "health check failed after rollback"
echo "$TARGET" > "$STATE_DIR/current"
[[ -n "$CUR" ]] && echo "$CUR" > "$STATE_DIR/previous"
echo "$(date -u +%FT%TZ) $TARGET (rollback)" >> "$STATE_DIR/history"
log "rolled back to $TARGET"
