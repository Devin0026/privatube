#!/usr/bin/env bash
# Restore the database from a backup made by the worker (daily, in /var/lib/privatube/backups).
#   sudo bash ./deploy/restore.sh /var/lib/privatube/backups/privatube-YYYYMMDD-HHMMSS.db.gz
# The current database is kept next to it as privatube.db.before-restore-<time>.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
FILE="${1:?give the backup file}"
DATA=/var/lib/privatube
RUN="runuser -u privatube -- bash -c"
PY='set -a; . /etc/privatube/env; exec /opt/privatube/venv/bin/python -m privatube.cli'

install -o privatube -g privatube -m 600 "$FILE" "$DATA/restore.db.gz"
trap 'rm -f "$DATA/restore.db.gz"' EXIT
$RUN "$PY check-backup $DATA/restore.db.gz"          # refuse a broken file before touching anything
read -r -p "Replace the live database with this backup? [y/N] " ok
[ "$ok" = y ] || { echo "cancelled"; exit 1; }
systemctl stop privatube-web privatube-worker
$RUN "$PY restore $DATA/restore.db.gz" || { systemctl start privatube-web privatube-worker; exit 1; }
systemctl start privatube-web privatube-worker
echo "Done. Sign in again if your session is newer than the backup."
