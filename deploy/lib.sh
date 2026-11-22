# Shared helpers for install.sh (sourced, not executed).

# Install an nginx config file, test the whole nginx config, and roll back on failure,
# so other sites on the box (e.g. an existing stream relay) never see a broken config.
install_nginx_conf() {   # <rendered-file> <destination>
    local src="$1" dest="$2" backup=""
    if [ -f "$dest" ]; then backup="$(mktemp)"; cp -p "$dest" "$backup"; fi
    install -m 640 "$src" "$dest"
    if ! nginx -t -q 2>/tmp/privatube-nginx-test.log; then
        echo "!! nginx rejected $dest, restoring the previous version:" >&2
        cat /tmp/privatube-nginx-test.log >&2
        if [ -n "$backup" ]; then cp -p "$backup" "$dest"; else rm -f "$dest"; fi
        exit 1
    fi
    [ -n "$backup" ] && rm -f "$backup"
    systemctl reload-or-restart nginx
}

# Let's Encrypt via HTTP-01 on port 80 (webroot). No DNS API token is stored on the server.
# Needs: the hostname reaches this box on port 80 (for a Cloudflare-proxied name: plain
# HTTP must not be redirected to HTTPS, and the Origin Rule must only rewrite HTTPS).
acme_setup() {   # <hosts...>
    install -d -m 755 /var/www/letsencrypt
    local tmp; tmp="$(mktemp)"
    sed "s|__HOSTS__|$*|" "$REPO/deploy/nginx/acme.conf.template" > "$tmp"
    install_nginx_conf "$tmp" /etc/nginx/sites-enabled/privatube-acme.conf
    rm -f "$tmp"
}

issue_cert() {   # <host> [more names on the same certificate...]
    local host="$1" want have d args=()
    want="$(printf '%s\n' "$@" | sort -u | tr '\n' ' ')"
    if [ -f "/etc/letsencrypt/live/$host/cert.pem" ]; then
        have="$(openssl x509 -in "/etc/letsencrypt/live/$host/cert.pem" -noout -ext subjectAltName \
                | grep -o 'DNS:[^,]*' | sed 's/DNS://' | sort -u | tr '\n' ' ')"
        [ "$have" = "$want" ] && return 0
        echo "   certificate $host: names change to: $want"
    fi
    for d in "$@"; do args+=(-d "$d"); done
    certbot certonly -q -n --webroot -w /var/www/letsencrypt --cert-name "$host" "${args[@]}" \
        --agree-tos --register-unsafely-without-email --deploy-hook "systemctl reload nginx"
}

# set_env FILE KEY VALUE: replace "KEY=..." (or "#KEY=...") in FILE, or append it.
set_env() {
    if grep -qE "^#?$2=" "$1"; then sed -i -e "s|^#\?$2=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi
}

# Debian security updates install themselves (daily; no reboots, no other upgrades).
auto_security_updates() {
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades >/dev/null
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        > /etc/apt/apt.conf.d/20auto-upgrades
}
