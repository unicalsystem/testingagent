#!/usr/bin/env bash
# Switches the running containers back to the previously deployed image tag.
#   bash deploy/scripts/rollback.sh            # to the previous release
#   bash deploy/scripts/rollback.sh <tag>      # to a specific release (see .deploy/history)
# Database migrations are NOT reverted automatically. TestPilot migrations are additive, so an
# older release runs on a newer schema. If a release ever needs the old schema, restore the
# backup that deploy.sh took before migrating (restore.sh), which is an explicit, confirmed step.
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
