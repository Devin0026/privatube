#!/usr/bin/env bash
# One-line install (or update) of PrivaTube on a Debian server:
#
#   curl -fsSL https://raw.githubusercontent.com/Devin0026/privatube/main/deploy/get.sh \
#     | sudo APP_HOST=videos.example.com ADMIN=yourname bash
#
# APP_HOST  the site's domain name (its DNS record must already point at this server)
# ADMIN     optional: create this admin account on the first install (password shown once)
# Also passed through to install.sh: HTTPS_PORT, CLOUDFLARE (see README).
# Run the same line again later to update; data and settings are kept.
set -euo pipefail

main() {   # in a function, so a half-downloaded script never runs
    : "${APP_HOST:?set APP_HOST, e.g. ... | sudo APP_HOST=videos.example.com bash}"
    local repo="${REPO_URL:-https://github.com/Devin0026/privatube.git}" branch="${BRANCH:-main}"
    local src=/opt/privatube/src
    [ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

    if ! command -v git >/dev/null; then
        echo "== git"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates >/dev/null
    fi
    echo "== PrivaTube source ($branch)"
    if [ -d "$src/.git" ]; then
        git -C "$src" fetch -q --depth 1 origin "$branch"
        git -C "$src" reset -q --hard FETCH_HEAD
    else
        install -d -m 755 /opt/privatube
        git clone -q --depth 1 -b "$branch" "$repo" "$src"
    fi
    echo "   $(git -C "$src" log -1 --format='%h %s')"

    bash "$src/deploy/install.sh"

    if [ -n "${ADMIN:-}" ]; then
        if sqlite3 /var/lib/privatube/privatube.db "SELECT 1 FROM users WHERE role='admin' LIMIT 1" | grep -q 1; then
            echo "(an admin account exists already: ADMIN=$ADMIN ignored)"
        else
            echo
            echo "== admin account $ADMIN (write the password down now, it is shown once)"
            bash "$src/deploy/create-admin.sh" "$ADMIN"
        fi
    fi
}

main "$@"
