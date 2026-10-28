#!/usr/bin/env bash
# Restores a backup made by backup.sh. DESTRUCTIVE: replaces the current database and
# evidence. A safety backup of the current state is taken first. Requires typed confirmation.
#   bash deploy/scripts/restore.sh /var/backups/testpilot/20261025T020000Z
source "$(dirname "$0")/common.sh"
SRC="${1:?usage: restore.sh <backup-dir>}"
[[ -f "$SRC/db.dump" && -f "$SRC/evidence.tar.gz" ]] || die "$SRC is not a TestAgent backup"
( cd "$SRC" && sha256sum -c --quiet SHA256SUMS ) || die "checksum verification failed"
require_files
set -a; . ./.env; set +a
CUR="$(current_tag)"
export IMAGE_TAG="${CUR:-latest}"   # restore runs the currently deployed release, not :latest

warn "This replaces the current database and all evidence with the backup from $SRC."
read -r -p "Type RESTORE to continue: " a; [[ "$a" == "RESTORE" ]] || die "aborted"

log "safety backup of the current state first (no pruning)"
NO_PRUNE=1 bash "$(dirname "$0")/backup.sh"

log "re-checking the backup is intact before touching the database"
( cd "$SRC" && sha256sum -c --quiet SHA256SUMS ) || die "backup changed or disappeared; nothing was modified"
"${COMPOSE[@]}" exec -T db pg_restore --list < "$SRC/db.dump" >/dev/null || die "db.dump is not a readable pg_dump archive; nothing was modified"

log "stopping api, worker and web"
"${COMPOSE[@]}" stop web api worker

log "restoring database"
"${COMPOSE[@]}" exec -T db psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$POSTGRES_DB' AND pid<>pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";" -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\";"
"${COMPOSE[@]}" exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error < "$SRC/db.dump"

log "restoring evidence"
docker run --rm -v testpilot_evidence:/data -v "$SRC":/backup:ro alpine:3.20 \
  sh -c 'find /data -mindepth 1 -delete && tar -xzf /backup/evidence.tar.gz -C /data && chown -R 10001:10001 /data'

log "bringing the schema up to the running release (no-op if already current)"
"${COMPOSE[@]}" --profile tools run --rm migrate

log "starting services"
"${COMPOSE[@]}" up -d
bash "$(dirname "$0")/health.sh"
