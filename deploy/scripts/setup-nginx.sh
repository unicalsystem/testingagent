#!/usr/bin/env bash
# Installs the host nginx site and obtains a Let's Encrypt certificate.
#   sudo LETSENCRYPT_EMAIL=you@unical.in bash deploy/scripts/setup-nginx.sh
# Run after DNS for testingagent.unical.in points at this VPS. Safe to re-run.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
DOMAIN=testingagent.unical.in
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EMAIL="${LETSENCRYPT_EMAIL:?set LETSENCRYPT_EMAIL}"

MYIP=$(curl -4 -fsS https://ifconfig.me || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
echo "VPS IPv4: ${MYIP:-unknown}   $DOMAIN resolves to: ${DNSIP:-nothing}"
[[ -n "$DNSIP" && "$DNSIP" == "$MYIP" ]] || { echo "DNS does not point at this VPS yet. Fix the A record and retry."; exit 1; }

install -d /var/www/certbot /etc/nginx/snippets
install -m 0644 "$APP_DIR/deploy/nginx/testpilot-proxy.conf" /etc/nginx/snippets/testpilot-proxy.conf

if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
  echo "== temporary HTTP-only site for the ACME challenge"
  cat > /etc/nginx/sites-available/$DOMAIN.conf <<NGX
server {
    listen 80; listen [::]:80;
    server_name $DOMAIN;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 503; }
}
NGX
  ln -sf /etc/nginx/sites-available/$DOMAIN.conf /etc/nginx/sites-enabled/$DOMAIN.conf
  nginx -t && systemctl reload nginx
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive
fi

echo "== full HTTPS site"
install -m 0644 "$APP_DIR/deploy/nginx/$DOMAIN.conf" /etc/nginx/sites-available/$DOMAIN.conf
ln -sf /etc/nginx/sites-available/$DOMAIN.conf /etc/nginx/sites-enabled/$DOMAIN.conf
nginx -t && systemctl reload nginx

echo "== renewal hook (reload nginx after renewal)"
install -d /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
certbot renew --dry-run --quiet && echo "certificate renewal dry-run OK"
echo "Done: https://$DOMAIN"
