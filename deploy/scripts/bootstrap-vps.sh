#!/usr/bin/env bash
# One-time preparation of a fresh Hostinger VPS (Ubuntu 22.04/24.04). Run as root:
#   sudo bash deploy/scripts/bootstrap-vps.sh
# Idempotent: safe to re-run. It never touches existing nginx sites, databases or volumes.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
. /etc/os-release
[[ "$ID" == "ubuntu" || "$ID" == "debian" ]] || { echo "Ubuntu/Debian expected, found $ID"; exit 1; }

# aaPanel servers already run their own nginx (/www/server/nginx), SSL and firewall.
# Installing Ubuntu's nginx/certbot/ufw there would fight over ports 80/443, so skip them.
AAPANEL=0
[[ -d /www/server/panel ]] && AAPANEL=1
echo "== packages (aaPanel detected: $AAPANEL)"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git jq unzip
if [[ $AAPANEL == 0 ]]; then
  apt-get install -y -q nginx certbot ufw
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "== installing Docker Engine + compose plugin (official repository)"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
if [[ $AAPANEL == 0 ]]; then
  systemctl enable --now nginx
  echo "== firewall (adds rules only; existing rules are kept)"
  ufw allow OpenSSH >/dev/null
  ufw allow 'Nginx Full' >/dev/null
  if ufw status | grep -q inactive; then
    echo "ufw is inactive. Enable it with:  ufw enable   (SSH is already allowed)"
  fi
else
  echo "== aaPanel: nginx, SSL and firewall are managed in the panel (see docs/AAPANEL.md)"
  docker compose version >/dev/null 2>&1 || { echo "docker compose plugin missing: apt-get install -y docker-compose-plugin"; exit 1; }
fi

echo "== directories"
install -d -m 0750 /var/backups/testpilot
[[ $AAPANEL == 0 ]] && install -d -m 0755 /var/www/certbot

echo "== swap (Chromium needs headroom on small plans)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "created 2G swap"
fi

echo
echo "Bootstrap complete. Docker $(docker --version | cut -d' ' -f3 | tr -d ,), $(docker compose version --short)."
echo "Public IPv4 of this VPS (use it for the DNS A record): $(curl -4 -fsS https://ifconfig.me || echo 'check hPanel')"
