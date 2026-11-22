#!/usr/bin/env bash
# Install or update PrivaTube on one Debian 12/13 server (Ubuntu 22.04+ should work too). As root, from the
# repository checkout:
#   sudo APP_HOST=videos.example.com ./deploy/install.sh
# Optional:
#   HTTPS_PORT=443      port for the site (default 443)
#   CLOUDFLARE=1        the site's DNS record is proxied by Cloudflare: log real visitor IPs.
#                       With Cloudflare, the proxied HTTPS ports are 443, 2053, 2083, 2087, 2096
#                       and 8443; port 80 must reach this server for the certificate.
# Safe to re-run: it updates code and config and keeps data, secrets and certificates.
# It only adds its own nginx files; other nginx sites on the server are left alone.
set -euo pipefail

: "${APP_HOST:?set APP_HOST, e.g. APP_HOST=videos.example.com}"
HTTPS_PORT="${HTTPS_PORT:-443}"
CLOUDFLARE="${CLOUDFLARE:-0}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA=/var/lib/privatube
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
. "$REPO/deploy/lib.sh"

echo "== packages (quiet, can take a few minutes)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    nginx python3-venv ffmpeg certbot curl unzip sqlite3 rsync openssl ca-certificates >/dev/null
# Signed video links need nginx's secure_link module: Debian's nginx has it, Ubuntu's
# needs nginx-extras.
if ! nginx -V 2>&1 | grep -q -- --with-http_secure_link_module; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx-extras >/dev/null
fi
auto_security_updates

echo "== swap (small servers: yt-dlp and ffmpeg need some headroom)"
if ! swapon --show | grep -q . && [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 2000000 ]; then
    fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== deno (JavaScript runtime yt-dlp needs for YouTube)"
if ! command -v deno >/dev/null; then
    case "$(uname -m)" in aarch64|arm64) arch=aarch64 ;; *) arch=x86_64 ;; esac
    curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-$arch-unknown-linux-gnu.zip"
    unzip -o -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip && chmod 755 /usr/local/bin/deno
fi

echo "== user and directories"
id privatube >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin privatube
# nginx (www-data) may read media and nothing else: the data dir is traverse-only, media is
# group www-data with setgid so new files inherit it, and the services run with UMask=0027.
install -d -o privatube -g privatube -m 711 "$DATA"
install -d -o privatube -g privatube -m 750 "$DATA/cache" "$DATA/backups"
install -d -o privatube -g www-data -m 2750 "$DATA/media"
chgrp -R www-data "$DATA/media"
find "$DATA/media" -type d -exec chmod 2750 {} +
find "$DATA/media" -type f -exec chmod 640 {} +
find "$DATA" -maxdepth 1 -type f -exec chmod o-rwx {} +
install -d -m 755 /opt/privatube
install -d -m 750 -g privatube /etc/privatube

echo "== application (pip install, a few minutes)"
rsync -a --delete --exclude .venv --exclude data --exclude .git --exclude '__pycache__' "$REPO/" /opt/privatube/app/
[ -x /opt/privatube/venv/bin/python ] || python3 -m venv /opt/privatube/venv
/opt/privatube/venv/bin/pip install -q -U pip
/opt/privatube/venv/bin/pip install -q -U /opt/privatube/app "yt-dlp[default]"

echo "== config"
if [ ! -f /etc/privatube/env ]; then
    sed -e "s|^PT_MEDIA_SECRET=.*|PT_MEDIA_SECRET=$(openssl rand -hex 24)|" \
        "$REPO/deploy/env.example" > /etc/privatube/env
    chown root:privatube /etc/privatube/env && chmod 640 /etc/privatube/env
    echo "   created /etc/privatube/env"
fi
port_suffix=""; [ "$HTTPS_PORT" = 443 ] || port_suffix=":$HTTPS_PORT"
[ "$CLOUDFLARE" = 1 ] && port_suffix=""      # Cloudflare's visitors use 443 (Origin Rule -> port)
set_env /etc/privatube/env PT_SITE_URL "https://$APP_HOST$port_suffix"
SECRET="$(sed -n 's/^PT_MEDIA_SECRET=//p' /etc/privatube/env)"

echo "== nginx + certificate"
install -m 644 "$REPO/deploy/nginx/privatube-proxy.conf" /etc/nginx/privatube-proxy.conf
realip="# not behind Cloudflare"
if [ "$CLOUDFLARE" = 1 ]; then
    {
        echo "# Cloudflare ranges, refreshed by install.sh"
        for v in 4 6; do curl -fsSL "https://www.cloudflare.com/ips-v$v" | sed 's/.*/set_real_ip_from &;/'; echo; done
        echo "real_ip_header CF-Connecting-IP;"
    } > /etc/nginx/privatube-cf-realip.conf
    realip="include /etc/nginx/privatube-cf-realip.conf;   # real visitor IP from CF-Connecting-IP"
fi
acme_setup "$APP_HOST"
issue_cert "$APP_HOST"
conf="$(mktemp)"
sed -e "s|__APP_HOST__|$APP_HOST|g" -e "s|__SECRET__|$SECRET|g" -e "s|__HTTPS_PORT__|$HTTPS_PORT|g" \
    -e "s|# __REALIP__|$realip|" "$REPO/deploy/nginx/site.conf.template" > "$conf"
# nginx before 1.25.1 (Debian 12, Ubuntu 22.04/24.04) has no "http2 on;": use "listen ... ssl http2".
nv="$(nginx -v 2>&1 | sed -n 's|.*nginx/\([0-9.]*\).*|\1|p')"
if [ "$(printf '%s\n' "$nv" 1.25.1 | sort -V | head -1)" != 1.25.1 ]; then
    sed -i -e '/^ *http2 on;/d' -e 's|^\( *listen .*\) ssl;|\1 ssl http2;|' "$conf"
fi
install_nginx_conf "$conf" /etc/nginx/sites-enabled/privatube.conf
rm -f "$conf"

echo "== services"
install -m 644 "$REPO"/deploy/systemd/privatube-* /etc/systemd/system/
systemctl daemon-reload
runuser -u privatube -- bash -c "set -a; . /etc/privatube/env; /opt/privatube/venv/bin/python -m privatube.cli init-db"
systemctl enable -q --now privatube-web privatube-worker privatube-ytdlp-update.timer
systemctl restart privatube-web privatube-worker

echo
echo "Done: https://$APP_HOST$port_suffix"
if ! sqlite3 "$DATA/privatube.db" "SELECT 1 FROM users WHERE role='admin' LIMIT 1" | grep -q 1; then
    echo "Create your admin account (the password is shown once):"
    echo "  sudo bash ./deploy/create-admin.sh YOURNAME"
fi
