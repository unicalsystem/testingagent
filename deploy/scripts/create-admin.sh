#!/usr/bin/env bash
# Creates the first admin (prompts for the password; it is never echoed or logged).
#   bash deploy/scripts/create-admin.sh admin
source "$(dirname "$0")/common.sh"
USERNAME="${1:-admin}"
read -r -s -p "Password for $USERNAME (12+ chars, letters and digits): " PW; echo
printf '%s\n' "$PW" | "${COMPOSE[@]}" exec -T api python -m testpilot.cli create-admin --username "$USERNAME" ${RESET:+--reset}
