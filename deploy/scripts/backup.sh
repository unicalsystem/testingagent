#!/usr/bin/env bash
# Backs up PostgreSQL (pg_dump custom format) and the evidence volume. Non-destructive.
#   bash deploy/scripts/backup.sh            -> /var/backups/testpilot/<timestamp>/
# Keeps the newest $KEEP backups (default 14). Schedule it from cron, e.g. daily at 02:30:
#   30 2 * * * cd /opt/testpilot && bash deploy/scripts/backup.sh >> /var/log/testpilot-backup.log 2>&1
source "$(dirname "$0")/common.sh"
KEEP="${KEEP:-14}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST="$BACKUP_DIR/$TS"
install -d -m 0750 "$DEST"

set -a; . ./.env; set +a
log "database dump -> $DEST/db.dump"
"${COMPOSE[@]}" exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner > "$DEST/db.dump"
[[ -s "$DEST/db.dump" ]] || die "database dump is empty"

log "evidence volume -> $DEST/evidence.tar.gz"
docker run --rm -v testpilot_evidence:/data:ro -v "$DEST":/backup alpine:3.20 \
  tar -czf /backup/evidence.tar.gz -C /data .

cp -p .env "$DEST/env.backup"; cp -p config.local "$DEST/config.local.backup"
chmod 600 "$DEST"/*.backup
echo "$(current_tag)" > "$DEST/image_tag"
( cd "$DEST" && sha256sum db.dump evidence.tar.gz > SHA256SUMS )
log "backup complete: $DEST ($(du -sh "$DEST" | cut -f1))"

# NO_PRUNE=1 (used by restore.sh) skips pruning so the backup being restored cannot be removed.
OLD=()
[[ "${NO_PRUNE:-0}" == "1" ]] || mapfile -t OLD < <(ls -1d "$BACKUP_DIR"/2*Z 2>/dev/null | sort | head -n -"$KEEP")
for d in "${OLD[@]}"; do log "pruning old backup $d"; rm -rf -- "$d"; done
echo "$DEST"
