#!/usr/bin/env bash
# Shared helpers. Sourced by the other scripts; run them from anywhere.
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$APP_DIR"
# shellcheck disable=SC2034  # used by the scripts that source this file
COMPOSE=(docker compose -f docker-compose.prod.yml)
BACKUP_DIR="${BACKUP_DIR:-/var/backups/testpilot}"
STATE_DIR="$APP_DIR/.deploy"
mkdir -p "$STATE_DIR"

log()  { printf '\033[1;34m[testpilot]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

confirm() {  # confirm "question" — skipped (yes) only when ASSUME_YES=1
  [[ "${ASSUME_YES:-0}" == "1" ]] && return 0
  read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy]$ ]]
}

require_files() {
  [[ -f .env ]] || die ".env is missing. Copy .env.example to .env and fill it in (see docs/DEPLOYMENT.md)."
  [[ -f config.local ]] || die "config.local is missing. Copy config.local.example to config.local (MySQL can stay disabled)."
  [[ -d config.local ]] && die "config.local is a directory (created by a bind mount). Remove it and copy config.local.example."
  grep -q '__generate' .env && die ".env still contains __generate__ placeholders."
  # config.local is mounted into containers that run as uid 10001: owner-only, owned by that uid.
  chown 10001:10001 config.local 2>/dev/null || warn "could not chown config.local to 10001 (run as root)"
  chmod 600 config.local .env
  grep -q '__same_as' .env && die ".env DATABASE_URL still contains the placeholder password."
  return 0
}

current_tag() { cat "$STATE_DIR/current" 2>/dev/null || echo ""; }
