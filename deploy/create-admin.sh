#!/usr/bin/env bash
# Create an account (admin by default). The generated password is shown once.
#   sudo bash ./deploy/create-admin.sh NAME [--viewer]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
NAME="${1:?give a username}"
ROLE="--admin"; [ "${2:-}" = "--viewer" ] && ROLE=""
exec runuser -u privatube -- bash -c 'set -a; . /etc/privatube/env; exec /opt/privatube/venv/bin/python -m privatube.cli create-user "$0" $1' "$NAME" "$ROLE"
